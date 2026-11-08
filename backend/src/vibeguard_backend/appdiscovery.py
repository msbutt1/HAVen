"""Lets the VibeGuard phone app find this laptop on the Wi-Fi network.

The app broadcasts ``VGFIND 1`` (padded with spaces to at least 64 bytes) to UDP port 8767. This answers the
sender only, with ``VGAPI 1 <http port> <password 0|1> <name>``, at most 64 bytes, so a reply is never larger
than the query that asked for it. The app takes this laptop's address from the reply's source address and
opens ``http://<address>:<http port>/dashboard/``.

It answers only senders on a private, link-local or loopback address, and ``serve`` starts it only when the web
API listens beyond this computer (``--host 0.0.0.0`` or a LAN address). Nothing here contacts another host on
its own: it only replies to a query it received.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading

QUERY = b"VGFIND 1"
MIN_QUERY = 64          # the query is padded to this, so the reply (<= MAX_REPLY) never amplifies it
MAX_REPLY = 64
MAX_QUERY = 512
DEFAULT_PORT = 8767


def clean_name(name: str) -> str:
    """A short, plain label for the app's list: letters, digits and dashes, at most 24 characters."""
    name = name.split(".")[0]
    name = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-")
    return (name or "vibeguard")[:24].strip("-") or "vibeguard"


def reply_for(data: bytes, sender: str, http_port: int, password: bool, name: str) -> bytes | None:
    """The reply to one datagram, or None when it is not a query this should answer."""
    if not MIN_QUERY <= len(data) <= MAX_QUERY:
        return None
    if not data.startswith(QUERY) or data[len(QUERY):len(QUERY) + 1] not in (b"", b" ", b"\x00"):
        return None
    try:
        ip = ipaddress.ip_address(sender.split("%")[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not (ip.is_private or ip.is_loopback or ip.is_link_local):
        return None
    return f"VGAPI 1 {int(http_port)} {1 if password else 0} {clean_name(name)}".encode("ascii")[:MAX_REPLY]


class AppDiscovery:
    """A daemon thread answering the app's discovery broadcasts on UDP ``port``."""

    def __init__(self, http_port: int, password: bool, name: str | None = None,
                 host: str = "0.0.0.0", port: int = DEFAULT_PORT):
        self.http_port = http_port
        self.password = password
        self.name = clean_name(name or socket.gethostname())
        self.host = host
        self.port = port
        self.answered = 0
        self.last_from: str | None = None
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind and start answering. Raises OSError when the port is taken."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # no SO_REUSEADDR: a second backend is told the port is taken
        try:
            s.bind((self.host, self.port))
        except OSError:
            s.close()
            raise
        s.settimeout(0.5)
        self.port = s.getsockname()[1]   # the real port when 0 was asked (tests)
        self._sock = s
        self._thread = threading.Thread(target=self._run, name="app-discovery", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        s = self._sock
        while s is not None and not self._stop.is_set():
            try:
                data, addr = s.recvfrom(MAX_QUERY + 1)
            except socket.timeout:
                continue
            except OSError:
                return
            out = reply_for(data, addr[0], self.http_port, self.password, self.name)
            if out is None:
                continue
            try:
                s.sendto(out, addr)
                self.answered += 1
                self.last_from = addr[0]
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
