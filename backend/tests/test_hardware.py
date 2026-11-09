"""Hardware bring-up tools: ports, hardware profile, serial-check, fake device. SIMULATED firmware only."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from conftest import ScriptedAdapter, sim_frames, wait_until

from haven_backend.cli import main as cli_main
from haven_backend.hardware import (FakeDevice, ProfileError, find_baud, list_ports, load_profile,
                                        render_ports, serial_check)
from haven_backend.preflight import FAIL, PASS, WARN

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs pseudo-terminals")

BAUD = 230400  # a pty only accepts some rates on macOS (460800 and 921600 fail with ENOTTY); a real port takes any


def check(report, name):
    found = [c for c in report.checks if c.name == name]
    assert found, f"no check named {name!r}: {[c.name for c in report.checks]}"
    return found[0]


@pytest.fixture
def device():
    made = []

    def make(**kw):
        d = FakeDevice(**kw).start()  # a pty does not enforce a baud rate; the check reports the bandwidth needed
        made.append(d)
        return d
    yield make
    for d in made:
        d.stop()


# ------------------------------------------------------------------------------ ports

def test_ports_listing_is_well_formed_and_renders_when_empty():
    ports = list_ports()
    assert isinstance(ports, list) and all({"device", "description", "vid", "likely_board"} <= set(p) for p in ports)
    assert "no serial ports found" in render_ports([])
    shown = render_ports([{"device": "/dev/cu.usbmodem1", "description": "Arduino Uno", "vid": "0x2341",
                           "pid": "0x0043", "likely_board": "Arduino", "is_usb": True}])
    assert "/dev/cu.usbmodem1" in shown and "Arduino" in shown


# ------------------------------------------------------------------------------ profile

def test_the_example_profile_loads_but_invents_no_sensor_facts():
    p = load_profile(Path(__file__).resolve().parents[1] / "hardware.example.json")
    assert p.emg_output == "raw" and p.name == "wearable-W" and p.device_id == "vg-w-2805a5075554"
    assert p.baud == 921600 and p.dtr is False and p.rts is False  # the ESP32 board: auto-reset lines left off
    assert p.imu_counts_per_g is None and p.imu_conversion_source is None  # nothing is a made-up datasheet value


@pytest.mark.parametrize("patch,message", [
    ({"emg": {"output": "envelope"}}, "ENVELOPE"),
    ({"emg": {"output": "weird"}}, "emg.output"),
    ({"imu": {"counts_per_g": 4096}}, "conversion_source"),
    ({"imu": {"counts_per_g": 4096, "conversion_source": "n/a"}}, "conversion_source"),
    ({"imu": {"counts_per_g": -1, "conversion_source": "datasheet table 1"}}, "positive"),
    ({"imu": {"counts_per_g": True, "conversion_source": "datasheet table 1"}}, "positive"),
    ({"baud": 12}, "baud"),
    ({"baud": "fast"}, "baud"),
    ({"emg": {"mains_hz": 55}}, "mains_hz"),
    ({"buad": 9600}, "unknown field"),
    ({"name": ""}, "name"),
    ({"emg": []}, "must be objects"),
])
def test_bad_profiles_are_refused_with_a_reason(tmp_path, patch, message):
    data = {"name": "bench", **patch}
    f = tmp_path / "hw.json"
    f.write_text(json.dumps(data))
    with pytest.raises(ProfileError, match=message):
        load_profile(f)


def test_unreadable_or_non_object_profile_files_are_clean_errors(tmp_path):
    with pytest.raises(ProfileError, match="cannot read"):
        load_profile(tmp_path / "missing.json")
    (tmp_path / "a.json").write_text("{not json")
    with pytest.raises(ProfileError, match="not valid JSON"):
        load_profile(tmp_path / "a.json")
    (tmp_path / "b.json").write_text("[1]")
    with pytest.raises(ProfileError, match="JSON object"):
        load_profile(tmp_path / "b.json")


def test_serve_refuses_an_envelope_profile_before_touching_data(tmp_path):
    f = tmp_path / "hw.json"
    f.write_text(json.dumps({"name": "bench", "emg": {"output": "envelope"}}))
    assert cli_main(["serve", "--profile", str(f), "--data-dir", str(tmp_path / "d")]) == 2
    assert not (tmp_path / "d").exists()


# ------------------------------------------------------------------------------ serial-check on a fake device

def test_a_healthy_link_passes_every_signal_check(device):
    dev = device()
    res = serial_check(dev.path, BAUD, seconds=5)
    r = res.report
    for name in ("port opens", "data arrives", "contract frames", "hardware facts known", "emg sample rate",
                 "imu sample rate", "emg is raw", "no records lost"):
        assert check(r, name).level == PASS, (name, check(r, name))
    assert res.stats["emg"]["measured_hz"] == pytest.approx(1000.0, rel=0.01)
    assert res.stats["imu"]["measured_hz"] == pytest.approx(400.0, rel=0.01)
    assert res.stats["invalid"] == {} and res.stats["seq_missing"] == 0
    # a pty is not throttled, so the check reports the bandwidth this stream NEEDS: ~31 kB/s is more than a
    # 230400-baud (23 kB/s) link can carry, which is the HANDOFF section 2.9 finding
    assert res.stats["link_utilisation"] > 1.0 and check(r, "link bandwidth").level == FAIL


def test_saved_capture_is_the_raw_stream_and_validates(device, tmp_path):
    dev = device()
    out = tmp_path / "cap.ndjson"
    res = serial_check(dev.path, BAUD, seconds=3, save=out)
    lines = out.read_bytes().splitlines()
    assert len(lines) == res.stats["lines"] > 20
    from haven_signals import contract
    assert all(contract.parse_line(x)[1] == [] for x in lines[1:])  # the first line may be a tail


def test_wrong_baud_symptoms_are_named(device):
    dev = device(mode="garbage")
    res = serial_check(dev.path, BAUD, seconds=3)
    c = check(res.report, "contract frames")
    assert c.level == FAIL and "WRONG BAUD" in c.fix and "--try-bauds" in c.fix
    assert res.report.failed


def test_a_silent_link_says_the_firmware_is_not_sending(device):
    dev = device(mode="silent")
    res = serial_check(dev.path, BAUD, seconds=2)
    c = check(res.report, "data arrives")
    assert c.level == FAIL and "no bytes" in c.detail and "TX" in c.fix


def test_a_missing_hello_is_a_warning(device):
    dev = device(drop_hello=True)
    res = serial_check(dev.path, BAUD, seconds=3)
    assert check(res.report, "hello received").level == WARN


@pytest.mark.parametrize("error,level", [(0.03, WARN), (0.08, FAIL)])
def test_a_wrong_sample_rate_is_measured_from_the_timestamps(device, error, level):
    dev = device(rate_error=error)
    res = serial_check(dev.path, BAUD, seconds=5)
    c = check(res.report, "emg sample rate")
    assert c.level == level, c
    assert res.stats["emg"]["measured_hz"] < 1000 * (1 - error * 0.7)


def test_a_device_that_hangs_mid_check_is_reported(device):
    dev = device(hang_after_s=2.0)
    res = serial_check(dev.path, BAUD, seconds=7)
    c = check(res.report, "data keeps flowing")
    assert c.level == FAIL and res.stats["stalled_for_s"] > 3


def test_an_unplug_mid_check_is_reported(device):
    dev = device(unplug_after_s=2.0)
    res = serial_check(dev.path, BAUD, seconds=8)
    assert check(res.report, "link stays up").level == FAIL


def test_a_port_that_does_not_exist_is_a_clean_failure(tmp_path):
    res = serial_check(str(tmp_path / "ttyNOPE"), BAUD, seconds=1)
    c = check(res.report, "port opens")
    assert c.level == FAIL and "ports" in c.fix and res.report.failed


def test_find_baud_scores_each_rate(device):
    dev = device()
    results = find_baud(dev.path, [115200, 230400], probe_s=1.5)
    assert [r["baud"] for r in results] == [115200, 230400] and all(r["valid"] > 0 for r in results)


# ------------------------------------------------------------------------------ signal sanity from crafted frames

class _Replayer:
    """An adapter that plays crafted frames (stands in for SerialAdapter)."""

    def __init__(self, frames):
        self.frames, self.i, self.port = list(frames), 0, "crafted"

    def open(self):
        pass

    def read_line(self, timeout):
        if self.i < len(self.frames):
            self.i += 1
            return self.frames[self.i - 1]
        time.sleep(min(timeout, 0.05))
        return None

    def close(self):
        pass


def _craft(emg_fn):
    out = []
    for f in sim_frames(duration_s=8, run_id="craft"):
        r = json.loads(f)
        if r["type"] == "emg_batch":
            t0 = r["first_sample_index"]
            r["samples"] = [[int(emg_fn((t0 + i) / 1000.0))] for i in range(len(r["samples"]))]
            r["quality"] = {"flags": []}
        out.append((json.dumps(r, separators=(",", ":")) + "\n").encode())
    return out


def test_a_smooth_one_sided_emg_is_flagged_as_an_envelope():
    frames = _craft(lambda t: 600 + 400 * math.sin(2 * math.pi * 1.5 * t))
    res = serial_check("x", BAUD, seconds=2, adapter_factory=lambda p, b: _Replayer(frames))
    c = check(res.report, "emg is raw")
    assert c.level == FAIL and "ENVELOPE" in c.detail + c.fix


def test_mains_hum_is_detected_and_attributed_to_the_frequency():
    frames = _craft(lambda t: 2048 + 500 * math.sin(2 * math.pi * 60 * t) + 40 * math.sin(97 * t * 7))
    res = serial_check("x", BAUD, seconds=2, adapter_factory=lambda p, b: _Replayer(frames), mains_hz=60)
    c = check(res.report, "mains hum")
    assert c.level == WARN and "60 Hz" in c.detail
    clean = _craft(lambda t: 2048 + 90 * math.sin(2 * math.pi * 31 * t) + 70 * math.sin(2 * math.pi * 133 * t))
    res2 = serial_check("x", BAUD, seconds=2, adapter_factory=lambda p, b: _Replayer(clean), mains_hz=60)
    assert check(res2.report, "mains hum").level == PASS  # always reported: a missing line is not a PASS


def test_clipping_at_the_declared_adc_limits_is_reported():
    heavy = _craft(lambda t: 4095 if int(t * 1000) % 5 == 0 else 2048 + (int(t * 1000) % 7))  # 20% railed
    res = serial_check("x", BAUD, seconds=2, adapter_factory=lambda p, b: _Replayer(heavy))
    c = check(res.report, "emg clipping")
    assert c.level == FAIL and res.stats["emg"]["clipped_samples"] > 100  # saturated input: not just a warning
    light = _craft(lambda t: 4095 if int(t * 1000) % 400 == 0 else 2048 + 300 * math.sin(97 * t) + (int(t * 1000) % 7))
    res2 = serial_check("x", BAUD, seconds=2, adapter_factory=lambda p, b: _Replayer(light))
    assert check(res2.report, "emg clipping").level == WARN


# ------------------------------------------------------------------------------ end to end: serve --profile on a fake device

def test_serve_with_a_profile_reads_a_serial_device_and_cites_the_conversion(device, tmp_path):
    dev = device()
    profile = tmp_path / "hw.json"
    profile.write_text(json.dumps({
        "name": "bench-rig", "port": dev.path, "baud": BAUD,
        "emg": {"output": "raw", "mains_hz": 60},
        "imu": {"model": "simulated", "counts_per_g": 4096,
                "conversion_source": "simulator synthetic scale 4096 counts per g (SIMULATED)"}}))
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "haven_backend.cli", "--data-dir", str(tmp_path / "data"),
                             "serve", "--profile", str(profile), "--http-port", str(port), "--log-level", "warning"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urllib.request.urlopen(base + path, timeout=2) as r:
                return json.loads(r.read())
        assert wait_until(lambda: _try(get, "/api/runtimes/wearable"), 30)
        assert wait_until(lambda: get("/api/runtimes/wearable")["counters"].get("records", 0) > 50, 15)
        def warmed():
            r = get("/api/runtimes/wearable")
            imu = (r["latest"].get("imu") or {}).get("features") or {}
            return ((imu.get("vibration") or {}).get("physical") or {}).get("status") == "ok"
        assert wait_until(warmed, 15)
        rt = get("/api/runtimes/wearable")
        assert rt["adapter"]["kind"] == "serial" and rt["adapter"]["port"] == dev.path
        assert rt["meta"]["hardware_profile_name"] == "bench-rig"
        assert rt["data_fresh"] is True and rt["hello"]["firmware"]["name"] == "vibeguard-sim"
        phys = rt["latest"]["imu"]["features"]["vibration"]["physical"]
        assert phys["units"] == "m/s^2" and "SIMULATED" in phys["conversion_source"]  # cited, never uncited
        assert rt["latest"]["emg"]["window"]["measured_rate_hz"] == pytest.approx(1000, rel=0.02)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception:  # noqa: BLE001
        return None


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
