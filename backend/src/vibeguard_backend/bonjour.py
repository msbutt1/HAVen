"""Announces this backend on the local network with Bonjour (DNS-SD), so the iPhone app can find it.

iOS lets an app browse Bonjour services (with the user's local-network permission) but not send the UDP broadcast
the Android app uses (appdiscovery.py, port 8767) without a special Apple entitlement. So, next to that responder,
``serve`` also announces ``_vibeguard._tcp`` with the web port and a TXT record:

    v=1            protocol version
    password=0|1   whether VG_BASIC_AUTH protects the API
    path=/dashboard/

The announcement goes out by multicast DNS on this computer's own network(s) only (224.0.0.251, port 5353). Like the
UDP responder, it runs only when the web API listens beyond this computer, and nothing here contacts another host.
"""
from __future__ import annotations

import ipaddress
import socket
import threading

from .appdiscovery import clean_name

SERVICE_TYPE = "_vibeguard._tcp.local."


def lan_ipv4_addresses() -> list[str]:
    """This computer's private, non-loopback IPv4 addresses (the ones a phone on the same Wi-Fi can reach)."""
    import ifaddr

    out: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue                                   # IPv6 entries are tuples
            try:
                a = ipaddress.IPv4Address(ip.ip)
            except ValueError:
                continue
            if (a.is_private or a.is_link_local) and not a.is_loopback and str(a) not in out:
                out.append(str(a))
    return out


def service_fields(http_port: int, password: bool, name: str, addresses: list[str]) -> dict:
    """What the announcement says, as plain data (tested without a network)."""
    label = clean_name(name)
    return {
        "type_": SERVICE_TYPE,
        "name": f"{label}.{SERVICE_TYPE}",
        "server": f"{label}.local.",
        "port": int(http_port),
        "properties": {"v": "1", "password": "1" if password else "0", "path": "/dashboard/"},
        "addresses": [socket.inet_aton(a) for a in addresses],
    }


class Announcer:
    """Registers the service in a background thread; ``stop`` withdraws it (a goodbye packet) and closes."""

    def __init__(self, http_port: int, password: bool, name: str | None = None,
                 addresses: list[str] | None = None):
        self.http_port = http_port
        self.password = password
        self.name = clean_name(name or socket.gethostname())
        self.addresses = addresses if addresses is not None else lan_ipv4_addresses()
        self.error: str | None = None
        self._zc = None
        self._info = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Start announcing. False (and ``error`` says why) when there is no LAN address to announce."""
        if not self.addresses:
            self.error = "no Wi-Fi or LAN address on this computer"
            return False
        self._thread = threading.Thread(target=self._register, name="bonjour", daemon=True)
        self._thread.start()
        return True

    def _register(self) -> None:
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf

            self._info = ServiceInfo(**service_fields(self.http_port, self.password, self.name, self.addresses))
            self._zc = Zeroconf(ip_version=IPVersion.V4Only)
            self._zc.register_service(self._info, allow_name_change=True)
        except Exception as e:                       # never take the backend down for an announcement
            self.error = f"{type(e).__name__}: {e}"

    def stop(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if self._zc is not None and self._info is not None:
                self._zc.unregister_service(self._info)
        except Exception:
            pass
        finally:
            if self._zc is not None:
                self._zc.close()
            self._zc = None
