"""The phone app finds the laptop over Wi-Fi: the backend answers its UDP query (appdiscovery.py)."""
import socket

import pytest

from vibeguard_backend.appdiscovery import MAX_REPLY, MIN_QUERY, AppDiscovery, clean_name, reply_for
from vibeguard_backend.bonjour import Announcer as RealAnnouncer   # bound before conftest swaps in a quiet one
from vibeguard_backend.cli import build_parser

Q = b"VGFIND 1".ljust(MIN_QUERY, b" ")


def test_reply_format_and_who_gets_one():
    assert reply_for(Q, "192.168.1.40", 8000, False, "lab-laptop") == b"VGAPI 1 8000 0 lab-laptop"
    assert reply_for(Q, "10.0.0.7", 8010, True, "x") == b"VGAPI 1 8010 1 x"          # a password is required
    assert reply_for(Q, "127.0.0.1", 8000, False, "x") is not None                    # the emulator's host alias
    assert reply_for(Q, "169.254.3.4", 8000, False, "x") is not None                  # link-local (no DHCP)
    assert reply_for(Q, "fe80::1%en0", 8000, False, "x") is not None
    assert reply_for(Q, "8.8.8.8", 8000, False, "x") is None                          # never a public address
    assert reply_for(Q, "100.64.0.1", 8000, False, "x") is None                       # carrier-grade NAT is not a LAN
    assert reply_for(Q, "not-an-ip", 8000, False, "x") is None


def test_only_padded_queries_are_answered_and_replies_never_amplify():
    assert reply_for(b"VGFIND 1", "192.168.1.40", 8000, False, "x") is None           # unpadded: too short
    assert reply_for(b"VGFIND 2".ljust(MIN_QUERY), "192.168.1.40", 8000, False, "x") is None
    assert reply_for(b"VGFIND 10".ljust(MIN_QUERY), "192.168.1.40", 8000, False, "x") is None
    assert reply_for(b"VGDISCOVER 1 vg-w-1".ljust(MIN_QUERY), "192.168.1.40", 8000, False, "x") is None  # the wearable's
    assert reply_for(b"VGFIND 1".ljust(600), "192.168.1.40", 8000, False, "x") is None
    r = reply_for(Q, "192.168.1.40", 65535, True, "a" * 200)
    assert r is not None and len(r) <= MAX_REPLY <= MIN_QUERY


def test_names_are_short_and_plain():
    assert clean_name("Abdujabbors-MacBook-Pro.local") == "Abdujabbors-MacBook-Pro"
    assert clean_name("lab pc (2)") == "lab-pc-2"
    assert clean_name("...") == "vibeguard"
    assert len(clean_name("x" * 99)) == 24
    assert clean_name("Abdujabbors-MacBook-Air-2") == "Abdujabbors-MacBook-Air"   # cut at 24, no dangling dash


def test_round_trip_over_a_real_socket():
    d = AppDiscovery(8000, password=False, name="test-laptop", host="127.0.0.1", port=0)
    d.start()
    try:
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(2)
        c.sendto(b"hello", ("127.0.0.1", d.port))                 # ignored
        c.sendto(Q, ("127.0.0.1", d.port))
        data, addr = c.recvfrom(256)
        assert data == b"VGAPI 1 8000 0 test-laptop" and addr[0] == "127.0.0.1"
        assert d.answered == 1 and d.last_from == "127.0.0.1"
        c.close()
    finally:
        d.stop()


def test_a_taken_port_raises_so_serve_can_say_so():
    a = AppDiscovery(8000, password=False, host="127.0.0.1", port=0)
    a.start()
    try:
        with pytest.raises(OSError):
            AppDiscovery(8000, password=False, host="127.0.0.1", port=a.port).start()
    finally:
        a.stop()


def test_serve_defaults():
    args = build_parser().parse_args(["serve"])
    assert args.app_discovery_port == 8767 and args.host == "127.0.0.1"   # discovery runs only with a LAN --host


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _ask(port: int) -> bytes | None:
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(0.5)
    try:
        c.sendto(Q, ("127.0.0.1", port))
        return c.recvfrom(256)[0]
    except OSError:
        return None
    finally:
        c.close()


@pytest.mark.parametrize("host,answers", [("0.0.0.0", True), ("127.0.0.1", False)])
def test_serve_answers_the_app_only_when_it_listens_beyond_this_computer(monkeypatch, tmp_path, host, answers):
    import uvicorn

    from vibeguard_backend.cli import main as cli_main

    port = _free_udp_port()
    seen = {}

    def fake_run(app, **kw):        # stands in for the web server: ask while "serving", then return
        seen["reply"] = _ask(port)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    rc = cli_main(["serve", "--device", "none", "--host", host, "--http-port", "8123",
                   "--app-discovery-port", str(port), "--data-dir", str(tmp_path / "d")])
    assert rc == 0
    if answers:
        assert seen["reply"] is not None and seen["reply"].startswith(b"VGAPI 1 8123 0 ")
    else:
        assert seen["reply"] is None
    assert _ask(port) is None        # stopped with the server


# ---------------------------------------------------------------- Bonjour, for the iPhone app

def test_bonjour_announcement_says_port_password_and_path():
    from vibeguard_backend.bonjour import SERVICE_TYPE, service_fields

    f = service_fields(8000, True, "Abdujabbors-MacBook-Air.local", ["192.168.43.20"])
    assert SERVICE_TYPE == "_vibeguard._tcp.local."
    assert f["name"] == "Abdujabbors-MacBook-Air._vibeguard._tcp.local."
    assert f["server"] == "Abdujabbors-MacBook-Air.local."
    assert f["port"] == 8000
    assert f["properties"] == {"v": "1", "password": "1", "path": "/dashboard/"}
    assert f["addresses"] == [socket.inet_aton("192.168.43.20")]


def test_bonjour_only_announces_lan_addresses(monkeypatch):
    import types

    from vibeguard_backend import bonjour

    fake = [types.SimpleNamespace(ips=[types.SimpleNamespace(ip="127.0.0.1"), types.SimpleNamespace(ip="192.168.1.5"),
                                       types.SimpleNamespace(ip=("fe80::1", 0, 4)), types.SimpleNamespace(ip="8.8.8.8"),
                                       types.SimpleNamespace(ip="169.254.10.2")])]
    monkeypatch.setattr("ifaddr.get_adapters", lambda: fake)
    assert bonjour.lan_ipv4_addresses() == ["192.168.1.5", "169.254.10.2"]
    a = RealAnnouncer(8000, False, addresses=[])
    assert a.start() is False and "no Wi-Fi" in a.error             # nothing to announce: says so, does not crash


@pytest.mark.parametrize("host,flags,announced", [("0.0.0.0", [], True), ("127.0.0.1", [], False),
                                                  ("0.0.0.0", ["--no-bonjour"], False)])
def test_serve_announces_on_bonjour_only_beyond_this_computer(monkeypatch, tmp_path, host, flags, announced):
    import uvicorn

    from vibeguard_backend import bonjour
    from vibeguard_backend.cli import main as cli_main

    seen = []

    class FakeAnnouncer:                        # no multicast in tests
        def __init__(self, http_port, password, name=None, addresses=None):
            self.name, self.addresses, self.error = "test", ["192.168.1.5"], None
            seen.append(("init", http_port, password))

        def start(self):
            seen.append("start")
            return True

        def stop(self):
            seen.append("stop")

    monkeypatch.setattr(bonjour, "Announcer", FakeAnnouncer)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    rc = cli_main(["serve", "--device", "none", "--host", host, "--http-port", "8124", "--app-discovery-port", "0",
                   *flags, "--data-dir", str(tmp_path / "d")])
    assert rc == 0
    assert seen == ([("init", 8124, False), "start", "stop"] if announced else [])
