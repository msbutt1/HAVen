"""The Wi-Fi wearable (firmware/INTEGRATION_FOR_ABDULJABBAR.md): discovery, TCP intake, tool use, new shift."""
import socket
import time

import pytest
from fastapi.testclient import TestClient

from conftest import sim_frames, wait_until
from haven_backend.adapters import AdapterDisconnected
from haven_backend.app import create_app
from haven_backend.fake_wifi import FakeWifiWearable
from haven_backend.service import Backend, processing_config_for
from haven_backend.wifi import WifiAdapter


def free_port(kind=socket.SOCK_STREAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def ports():
    return free_port(), free_port(socket.SOCK_DGRAM)


def test_the_wifi_config_is_salaars_live_config_with_a_cited_conversion():
    cfg = processing_config_for("wifi")
    assert cfg.clock == "host" and cfg.stale_after_s == 1.0 and cfg.imu.window_s == 0.5
    scale, offset = cfg.imu.conversion["ax"]
    assert abs(scale - 9.80665 / 4096) < 1e-12 and offset == 0
    assert "4096" in cfg.imu.conversion_source and "MPU-6500" in cfg.imu.conversion_source
    with pytest.raises(ValueError):
        processing_config_for("wifi", 0)


def test_discovery_is_answered_with_the_tcp_port(ports):
    tcp, udp = ports
    ad = WifiAdapter("127.0.0.1", tcp, udp, wait_s=0.2)
    with pytest.raises(AdapterDisconnected, match="waiting for the wearable"):
        ad.open()                                   # listening, nobody connected yet
    try:
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(2)
        c.sendto(b"VGDISCOVER 1 vg-w-2805a5075554", ("127.0.0.1", udp))
        data, _ = c.recvfrom(64)
        assert data == f"VGBRIDGE 1 {tcp}".encode()
        c.sendto(b"HELLO", ("127.0.0.1", udp))      # anything else gets no answer
        with pytest.raises(socket.timeout):
            c.settimeout(0.4)
            c.recvfrom(64)
        c.close()
        assert ad.discoveries == 1
    finally:
        ad.interrupt()


def test_lines_arrive_whole_and_a_non_wearable_connection_never_hijacks_the_stream(ports):
    tcp, udp = ports
    ad = WifiAdapter("127.0.0.1", tcp, udp, wait_s=2)
    frames = sim_frames("happy_path", duration_s=3.0)
    ad._listen()   # the runtime's first open() does this; here the clients connect before it
    try:
        wearable = socket.create_connection(("127.0.0.1", tcp))
        wearable.sendall(frames[0])
        ad.open()
        assert ad.read_line(1) == frames[0]          # the first line, which proved it a wearable, is delivered
        intruder = socket.create_connection(("127.0.0.1", tcp))
        intruder.sendall(b"GET /ws/live HTTP/1.1\r\nHost: localhost\r\n\r\n")
        wearable.sendall(b"".join(frames[1:40]))
        got = []
        end = time.monotonic() + 5
        while len(got) < 39 and time.monotonic() < end:
            line = ad.read_line(0.2)
            if line:
                got.append(line)
        assert got == frames[1:40]                   # nothing from the intruder, nothing lost
        assert wait_until(lambda: (ad.read_line(0.05), ad.rejected_connections)[1] >= 1, 3)
        assert ad.connections == 1
        intruder.close()
        wearable.close()
    finally:
        ad.interrupt()


def test_a_reconnecting_wearable_replaces_the_old_connection(ports):
    tcp, udp = ports
    ad = WifiAdapter("127.0.0.1", tcp, udp, wait_s=2)
    frames = sim_frames("happy_path", duration_s=3.0)
    ad._listen()
    try:
        old = socket.create_connection(("127.0.0.1", tcp))
        old.sendall(frames[0])
        ad.open()
        assert ad.read_line(1) == frames[0]
        new = socket.create_connection(("127.0.0.1", tcp))     # e.g. after a reboot; the old one may be half-open
        new.sendall(frames[1] + frames[2])
        got = [ad.read_line(0.5) for _ in range(4)]
        assert frames[1] in got and frames[2] in got and ad.connections == 2
        new.close()
        with pytest.raises(AdapterDisconnected, match="disconnected"):
            for _ in range(20):
                ad.read_line(0.1)
        old.close()
    finally:
        ad.interrupt()


def test_a_port_already_in_use_is_refused_with_a_reason(ports):
    tcp, udp = ports
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", tcp))
    squatter.listen(1)
    try:
        ad = WifiAdapter("0.0.0.0", tcp, udp, wait_s=0.2)
        with pytest.raises(AdapterDisconnected, match="already in use"):
            ad.open()
    finally:
        squatter.close()


def test_end_to_end_with_the_simulated_wifi_wearable(tmp_path, ports):
    """Discovery, connection, processing: tool use is detected on the tool_vs_arm scenario, and a new shift
    resets the tool time. Everything is labelled simulated."""
    tcp, udp = ports
    b = Backend(tmp_path / "data")
    fw = None
    try:
        rt = b.add_runtime("wearable", WifiAdapter("127.0.0.1", tcp, udp), processing_config_for("wifi"))
        fw = FakeWifiWearable("tool_vs_arm", duration_s=20.0, speed=4.0, discovery_port=udp).start()
        assert wait_until(lambda: rt.connection == "connected" and rt.counters["records"] > 50, 15)
        assert rt.provenance["source"] == "simulated"
        assert wait_until(lambda: (rt.latest.get("tool_use") or {}).get("state") == "tool", 15)
        tool = rt.latest["tool_use"]
        assert tool["status"] == "ok" and tool["dominant_hz"] > 50
        assert rt.counters.get("invalid_frames", 0) == 0
        assert wait_until(lambda: (rt.latest["tool_use"] or {}).get("session_tool_s", 0) > 0.5, 10)
        with TestClient(create_app(b), base_url="http://localhost") as c:
            r = c.post("/api/runtimes/wearable/tool-use/new-shift", json={"request_id": "shift-1"})
            assert r.status_code == 200 and r.json()["event"] == "tool_session_reset"
            assert r.json()["previous"]["session_tool_s"] > 0
            again = c.post("/api/runtimes/wearable/tool-use/new-shift", json={"request_id": "shift-1"})
            assert again.json() == r.json()                       # the same request id does not reset twice
            assert c.get("/api/runtimes/wearable/snapshot").status_code in (200, 404)
        assert wait_until(lambda: rt.latest["tool_use"] is not None and rt.latest["tool_use"]["session_tool_s"] < 1.0, 5)
        assert any((tmp_path / "data" / "tool_use").glob("*.json"))   # last known totals survive a restart
    finally:
        if fw:
            fw.stop()
        b.shutdown()


def test_a_recording_stores_its_tool_time(tmp_path, ports):
    tcp, udp = ports
    b = Backend(tmp_path / "data")
    fw = None
    try:
        rt = b.add_runtime("wearable", WifiAdapter("127.0.0.1", tcp, udp), processing_config_for("wifi"))
        fw = FakeWifiWearable("tool_vs_arm", duration_s=20.0, speed=4.0, discovery_port=udp).start()
        assert wait_until(lambda: rt.counters["records"] > 20, 15)
        sess = b.start_session("wearable")
        assert wait_until(lambda: (rt.latest.get("tool_use") or {}).get("session_tool_s", 0) > 1.0, 20)
        time.sleep(0.5)
        row = b.stop_session(sess["session_id"])
        r = row["counters"]["readings"]
        assert r["tool_time_s"] is not None and r["tool_time_s"] > 0
        assert r["peak_vibration_ms2"] is not None                 # m/s^2: the conversion is configured
    finally:
        if fw:
            fw.stop()
        b.shutdown()


def test_saved_tool_totals_are_shown_after_a_restart(tmp_path, ports):
    tcp, udp = ports
    d = tmp_path / "data" / "tool_use"
    d.mkdir(parents=True)
    (d / "vg-w-x.json").write_text('{"device_id": "vg-w-x", "saved_at": "2026-11-08T10:00:00Z", '
                                   '"record": {"type": "tool_use", "session_tool_s": 42.5, "status": "ok"}}')
    b = Backend(tmp_path / "data")
    try:
        rt = b.add_runtime("wearable", WifiAdapter("127.0.0.1", tcp, udp, wait_s=0.1), processing_config_for("wifi"))
        st = rt.status()
        assert st["tool_use_saved"]["record"]["session_tool_s"] == 42.5
        assert st["tool_use_saved"]["device_id"] == "vg-w-x"
    finally:
        b.shutdown()
