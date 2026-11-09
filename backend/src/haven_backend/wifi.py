"""The wearable over Wi-Fi: the laptop side of Salaar's transport (firmware/INTEGRATION_FOR_ABDULJABBAR.md).

    wearable --UDP broadcast "VGDISCOVER 1 <device_id>" to 8766--> laptop answers "VGBRIDGE 1 <tcp_port>"
    wearable --TCP connect to <tcp_port> (8765), contract v1 NDJSON--> this adapter --> the runtime's Processor

The laptop's hotspot address can change, so the wearable finds it by broadcast and never needs it configured.
Lines are handed to the runtime exactly as received; the runtime validates every one (contract.parse_line) and
never repairs an invalid line. One wearable at a time: a new connection (the wearable reconnecting after a reboot,
or after Wi-Fi dropped) replaces the old one, whose socket may be half-open.
"""

from __future__ import annotations

import select
import socket
import threading
import time

from haven_signals import contract

from .adapters import AdapterDisconnected, DeviceAdapter

DISCOVERY_PREFIX = b"VGDISCOVER 1 "
MAX_LINE = contract.MAX_LINE_BYTES


class WifiAdapter(DeviceAdapter):
    kind = "wifi"
    clock = "host"

    def __init__(self, host: str = "0.0.0.0", port: int = 8765, discovery_port: int | None = 8766,
                 wait_s: float = 1.0):
        if not 0 < port < 65536 or (discovery_port is not None and not 0 < discovery_port < 65536):
            raise ValueError("ports must be between 1 and 65535")
        self.host, self.port, self.discovery_port = host, port, discovery_port
        self.wait_s = wait_s
        self._server: socket.socket | None = None
        self._udp: socket.socket | None = None
        self._udp_thread: threading.Thread | None = None
        self._client: socket.socket | None = None
        # new connections wait here until their first line proves they are a wearable (a contract record);
        # anything else (a browser or app pointed at the wrong port, a port scanner) is closed and never
        # replaces the wearable's connection
        self._candidates: dict[socket.socket, list] = {}   # socket -> [buffer, accepted_at, peer]
        self._pending: socket.socket | None = None         # a proven wearable connection, adopted at the next read
        self._pending_buf = bytearray()
        self.rejected_connections = 0
        self._buf = bytearray()
        self._discarding = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.peer: str | None = None
        self.connections = 0
        self.discoveries = 0
        self.last_discovery_from: str | None = None
        self.oversize_resyncs = 0

    # ---------------------------------------------------------------- sockets

    def _listen(self) -> None:
        if self._server is not None:
            return
        # SO_REUSEADDR lets a wildcard listener share a port with a specific-address one (macOS), so check first:
        # if anything already answers on this port, the wearable's connections could go to the wrong program
        try:
            probe = socket.create_connection(("127.0.0.1", self.port), timeout=0.3)
        except OSError:
            pass
        else:
            probe.close()
            raise AdapterDisconnected(f"TCP {self.port} is already in use by another program (vg-bridge, another "
                                      "backend, or an old server still shutting down): the wearable cannot reach this one")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
        except OSError as exc:
            srv.close()
            raise AdapterDisconnected(f"cannot listen on TCP {self.port} for the wearable: {exc} "
                                      "(is vg-bridge or another backend already running?)") from exc
        srv.listen(4)
        srv.setblocking(False)
        self._server = srv
        if self.discovery_port is not None and self._udp is None:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            try:
                udp.bind((self.host, self.discovery_port))
            except OSError as exc:
                udp.close()
                srv.close()
                self._server = None
                raise AdapterDisconnected(f"cannot listen for the wearable's discovery on UDP {self.discovery_port}: "
                                          f"{exc}") from exc
            udp.settimeout(0.5)
            self._udp = udp
            self._udp_thread = threading.Thread(target=self._answer_discovery, name="wifi-discovery", daemon=True)
            self._udp_thread.start()

    def _answer_discovery(self) -> None:
        """Answer "VGDISCOVER 1 <device_id>" with "VGBRIDGE 1 <tcp port>" (the wearable then connects)."""
        udp = self._udp
        while udp is not None and not self._stop.is_set():
            try:
                data, addr = udp.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                return
            if data.startswith(DISCOVERY_PREFIX):
                try:
                    udp.sendto(f"VGBRIDGE 1 {self.port}".encode(), addr)
                    self.discoveries += 1
                    self.last_discovery_from = addr[0]
                except OSError:
                    pass

    def _accept_pending(self) -> None:
        """Take the newest waiting connection; it replaces the current one at the next read."""
        srv = self._server
        if srv is None:
            return
        while True:
            try:
                conn, addr = srv.accept()
            except (BlockingIOError, OSError):
                break
            conn.setblocking(False)
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self._candidates[conn] = [bytearray(), time.monotonic(), addr[0]]
        self._check_candidates()

    CANDIDATE_TIMEOUT_S = 5.0

    def _check_candidates(self) -> None:
        """Read what new connections sent; promote the first one whose first line is a telemetry record."""
        now = time.monotonic()
        for conn, (buf, since, peer) in list(self._candidates.items()):
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise ConnectionError("closed")
                    buf.extend(chunk)
                    if b"\n" in buf or len(buf) > MAX_LINE:
                        break
            except BlockingIOError:
                pass
            except OSError:
                self._reject(conn)
                continue
            nl = buf.find(b"\n")
            if nl < 0:
                if len(buf) > MAX_LINE or now - since > self.CANDIDATE_TIMEOUT_S:
                    self._reject(conn)
                continue
            rec, _issues = contract.parse_line(bytes(buf[:nl + 1]))
            if rec is None:   # not the contract (an HTTP request, garbage): not a wearable
                self._reject(conn)
                continue
            del self._candidates[conn]
            with self._lock:
                if self._pending is not None:
                    self._pending.close()
                self._pending = conn
                self._pending_peer = peer
                self._pending_buf = bytearray(buf)   # the first line is delivered too

    def _reject(self, conn: socket.socket) -> None:
        self._candidates.pop(conn, None)
        self.rejected_connections += 1
        try:
            conn.close()
        except OSError:
            pass

    # ---------------------------------------------------------------- adapter API

    def open(self) -> None:
        """Listen, then wait (up to ``wait_s``) for the wearable to connect. No connection yet is a disconnect,
        so the runtime shows "waiting for the wearable" and tries again."""
        self._stop.clear()
        self._listen()
        end = time.monotonic() + self.wait_s
        while time.monotonic() < end and not self._stop.is_set():
            self._accept_pending()
            if self._adopt_pending():
                return
            try:
                select.select([self._server, *self._candidates], [], [], 0.1)
            except (OSError, ValueError):
                break
        raise AdapterDisconnected(f"waiting for the wearable to connect (TCP {self.port}"
                                  + (f", discovery UDP {self.discovery_port})" if self.discovery_port else ")"))

    def _adopt_pending(self) -> bool:
        with self._lock:
            conn, self._pending = self._pending, None
            peer = getattr(self, "_pending_peer", None)
            first = self._pending_buf
            self._pending_buf = bytearray()
        if conn is None:
            return False
        if self._client is not None:
            self._client.close()
        self._client = conn
        self.peer = peer
        self.connections += 1
        self._buf = first
        self._discarding = False
        return True

    def read_line(self, timeout: float) -> bytes | None:
        self._accept_pending()
        if self._pending is not None:
            # the wearable reconnected (a reboot, or Wi-Fi came back): the old socket may be half-open
            self._adopt_pending()
        conn = self._client
        if conn is None:
            raise AdapterDisconnected("the wearable is not connected")
        end = time.monotonic() + timeout
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl + 1])
                del self._buf[:nl + 1]
                if self._discarding:
                    self._discarding = False
                    continue
                return line
            if len(self._buf) > MAX_LINE:   # no newline in sight: hand over the start (rejected), skip the rest
                head = bytes(self._buf[:MAX_LINE + 1])
                self._buf.clear()
                self.oversize_resyncs += 1
                if not self._discarding:
                    self._discarding = True
                    return head
                continue
            left = end - time.monotonic()
            if left <= 0 or self._stop.is_set():
                return None
            try:
                ready, _, _ = select.select([conn], [], [], min(left, 0.1))
            except (OSError, ValueError) as exc:
                self._drop("the connection failed")
                raise AdapterDisconnected(f"the wearable's connection failed: {exc}") from exc
            if not ready:
                self._accept_pending()
                if self._pending is not None:
                    return None  # adopt it at the next call
                continue
            try:
                chunk = conn.recv(65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                self._drop("the connection failed")
                raise AdapterDisconnected(f"the wearable's connection failed: {exc}") from exc
            if not chunk:
                self._drop("closed")
                raise AdapterDisconnected("the wearable disconnected")
            self._buf.extend(chunk)

    def _drop(self, why: str) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except OSError:
                pass
        self._client = None
        self._buf.clear()
        self._discarding = False

    def close(self) -> None:
        """The runtime gave up on this connection; keep listening so the wearable can reconnect."""
        self._drop("closed by the runtime")

    def interrupt(self) -> None:
        """The runtime is stopping: stop listening altogether."""
        self._stop.set()
        self._drop("stopping")
        for c in list(self._candidates):
            self._reject(c)
        for s in (self._server, self._udp, self._pending):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._server = self._udp = self._pending = None

    def describe(self) -> dict:
        return {**super().describe(), "tcp_port": self.port, "discovery_port": self.discovery_port,
                "listen_host": self.host, "peer": self.peer, "connections": self.connections,
                "discoveries_answered": self.discoveries, "last_discovery_from": self.last_discovery_from,
                "rejected_connections": self.rejected_connections,
                "oversize_resyncs": self.oversize_resyncs}
