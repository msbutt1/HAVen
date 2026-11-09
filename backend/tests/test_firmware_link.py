"""Serial adapter selection by device_id, and the stream adapter (vg-fake-device output). SIMULATED data only.

Two wearable boards report the same USB serial number, so the OS cannot tell them apart: the firmware's
device_id in every record is what identifies one (see the firmware README: vg-w-<base MAC>).
"""

from __future__ import annotations

import json
import os
import pty
import threading
import time
import tty
from pathlib import Path

import pytest
from conftest import sim_frames, wait_until

from haven_backend.adapters import AdapterDisconnected
from haven_backend.runtime import DeviceRuntime
from haven_backend.serial_select import DeviceIdSerialAdapter
from haven_backend.service import processing_config_for
from haven_backend.stream import StreamAdapter

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs pseudo-terminals")
BAUD = 115200  # any rate a pty accepts; the real boards use 921600


def with_device(frames: list[bytes], device_id: str) -> list[bytes]:
    out = []
    for f in frames:
        r = json.loads(f)
        r["device_id"] = device_id
        r["boot_id"] = "b-" + device_id[-8:].ljust(8, "0") + "00000000"
        out.append((json.dumps(r, separators=(",", ":")) + "\n").encode())
    return out


class PtyBoard:
    """A pseudo-terminal 'board': says nothing until a host opens the port, then streams its frames."""

    def __init__(self, device_id: str | None, link: Path, silent: bool = False, dt: float = 0.01):
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        os.set_blocking(self.master, False)
        self.path = os.ttyname(slave)
        os.close(slave)  # only the host holds the slave, so a write succeeding means a host is connected
        link.symlink_to(self.path)
        self.frames = [] if silent or device_id is None else with_device(sim_frames(duration_s=8, run_id="fw"), device_id)
        self.silent, self.dt = silent, dt
        self.opens = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                os.write(self.master, b"")  # succeeds only while a host has the port open
            except OSError:
                self._stop.wait(0.05)
                continue
            self.opens += 1
            self._stop.wait(0.3)  # a board needs time to come out of reset; the host also configures the tty by then
            for f in self.frames:
                if self._stop.is_set():
                    return
                view, gone = memoryview(f), False
                while view and not self._stop.is_set():
                    try:
                        view = view[os.write(self.master, view):]  # a non-blocking write can be partial
                    except BlockingIOError:
                        self._stop.wait(0.002)
                    except OSError:
                        gone = True  # the host closed the port: wait for the next open (a fresh boot)
                        break
                if gone:
                    break
                self._stop.wait(self.dt)
            else:
                self._stop.wait(0.5)

    def close(self):
        self._stop.set()
        self._t.join(2)
        try:
            os.close(self.master)
        except OSError:
            pass


@pytest.fixture
def boards(tmp_path):
    made = []

    def make(spec: dict[str, str | None], **kw):
        out = {}
        for name, dev in spec.items():
            b = PtyBoard(dev, tmp_path / name, **kw)
            made.append(b)
            out[name] = b
        return out
    yield make
    for b in made:
        b.close()


def test_the_board_with_the_expected_device_id_is_chosen_among_several_ports(boards, tmp_path):
    boards({"ttyUSB0": "vg-w-aaaaaaaaaaaa", "ttyUSB1": "vg-w-2805a5075554", "ttyUSB10": "vg-w-cccccccccccc"})
    ad = DeviceIdSerialAdapter(str(tmp_path / "ttyUSB*"), BAUD, "vg-w-2805a5075554", dtr=False, rts=False, probe_s=3)
    ad.open()
    try:
        assert ad.port.endswith("ttyUSB1") or os.path.realpath(ad.port) == os.path.realpath(tmp_path / "ttyUSB1")
        assert ad.seen[str(tmp_path / "ttyUSB0")] == "vg-w-aaaaaaaaaaaa"   # natural order: 0, 1, 10
        assert ad.candidates[-1].endswith("ttyUSB10")
        first = json.loads(ad.read_line(1.0))
        assert first["device_id"] == "vg-w-2805a5075554"                  # what was read while identifying is kept
        assert first["type"] == "hello"
    finally:
        ad.close()
    d = ad.describe()
    assert d["expect_device_id"] == "vg-w-2805a5075554" and d["dtr"] is False and d["rts"] is False


def test_nothing_is_lost_while_identifying_the_board(boards, tmp_path):
    boards({"ttyUSB0": "vg-w-2805a5075554"})
    ad = DeviceIdSerialAdapter(str(tmp_path / "ttyUSB0"), BAUD, "vg-w-2805a5075554", probe_s=3)
    ad.open()
    try:
        recs = []
        end = time.monotonic() + 2.5
        while time.monotonic() < end and len(recs) < 60:
            line = ad.read_line(0.2)
            if line:
                recs.append(json.loads(line))
        seqs = [r["seq"] for r in recs]
        assert seqs == list(range(len(seqs))) and len(seqs) >= 40   # contiguous from the hello: no gap at hand-over
    finally:
        ad.close()


def test_no_port_with_the_expected_id_is_a_visible_disconnect_naming_what_was_found(boards, tmp_path):
    boards({"ttyUSB0": "vg-w-aaaaaaaaaaaa", "ttyUSB1": None})
    ad = DeviceIdSerialAdapter(str(tmp_path / "ttyUSB*"), BAUD, "vg-w-2805a5075554", probe_s=1.0)
    with pytest.raises(AdapterDisconnected) as exc:
        ad.open()
    msg = str(exc.value)
    assert "vg-w-2805a5075554" in msg and "vg-w-aaaaaaaaaaaa" in msg and "no valid data" in msg
    with pytest.raises(AdapterDisconnected, match="no port matches"):
        DeviceIdSerialAdapter(str(tmp_path / "nothing*"), BAUD, "vg-w-x", probe_s=1).open()


def test_the_runtime_follows_the_right_board_and_reconnects_to_it(boards, tmp_path, backend):
    bs = boards({"ttyUSB0": "vg-w-aaaaaaaaaaaa", "ttyUSB1": "vg-w-2805a5075554"})
    ad = DeviceIdSerialAdapter(str(tmp_path / "ttyUSB*"), BAUD, "vg-w-2805a5075554", dtr=False, rts=False, probe_s=3)
    rt = backend.add_runtime("wearable", ad, processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] > 60, 20)
    assert rt.device_id == "vg-w-2805a5075554" and rt.counters.get("foreign_frames", 0) == 0
    assert rt.provenance["source"] == "simulated"          # the firmware labels its fake acquisition simulated
    assert rt.status()["adapter"]["port"] and ad._last_port.endswith("ttyUSB1")


# ------------------------------------------------------------------------------ stream adapter

def test_a_stream_file_is_read_as_written_and_keeps_its_provenance(backend, tmp_path):
    f = tmp_path / "fake.ndjson"
    frames = with_device(sim_frames(duration_s=5, run_id="fw"), "vg-w-2805a5075554")
    f.write_bytes(b"".join(frames))
    rt = backend.add_runtime("wearable", StreamAdapter(str(f)), processing_config_for("sim"))
    assert wait_until(lambda: rt.finished, 20)
    assert rt.counters["records"] == len(frames) and rt.error is None
    assert rt.provenance == {"source": "simulated", "recording_id": json.loads(frames[0])["provenance"]["recording_id"]}
    assert rt.adapter.describe()["note"].startswith("records keep the provenance")


def test_boot_banner_noise_in_a_stream_is_counted_and_skipped(backend, tmp_path):
    f = tmp_path / "noisy.ndjson"
    frames = with_device(sim_frames(duration_s=3, run_id="fw"), "vg-w-2805a5075554")
    f.write_bytes(b"\xe0\x9c\x00ets Jun  8 2016 00:22:57\r\nrst:0x1 (POWERON_RESET)\n" + b"".join(frames))
    rt = backend.add_runtime("wearable", StreamAdapter(str(f)), processing_config_for("sim"))
    assert wait_until(lambda: rt.finished, 20)
    assert rt.counters["invalid_frames"] == 2 and rt.counters["records"] == len(frames)


def test_a_paced_stream_plays_at_device_speed(tmp_path):
    f = tmp_path / "p.ndjson"
    frames = with_device(sim_frames(duration_s=3, run_id="fw"), "vg-w-2805a5075554")
    f.write_bytes(b"".join(frames))
    ad = StreamAdapter(str(f), paced=True, speed=10.0)
    ad.open()
    t0, n = time.monotonic(), 0
    while True:
        try:
            line = ad.read_line(0.5)
        except Exception:  # EndOfStream
            break
        n += line is not None
    took = time.monotonic() - t0
    assert n == len(frames) and 0.15 < took < 1.5     # ~3 s of device time at 10x
    ad.close()


def test_a_missing_stream_file_is_a_clean_disconnect(tmp_path):
    with pytest.raises(AdapterDisconnected, match="not found"):
        StreamAdapter(str(tmp_path / "nope.ndjson")).open()


# ------------------------------------------------------------------------------ serial-check reads the firmware's own facts

import socket
import subprocess
import sys
import urllib.request

from haven_backend.hardware import ProfileError, load_profile, serial_check
from haven_backend.preflight import FAIL, PASS, WARN


class _Lines:
    def __init__(self, lines):
        self.lines, self.i, self.port = list(lines), 0, "crafted"

    def open(self):
        pass

    def read_line(self, timeout):
        if self.i < len(self.lines):
            self.i += 1
            return self.lines[self.i - 1]
        time.sleep(min(timeout, 0.02))
        return None

    def close(self):
        pass


def _firmware_stream(device_id="vg-w-2805a5075554", counters=None, hardware=None):
    out = []
    for f in with_device(sim_frames(duration_s=6, run_id="fw"), device_id):
        r = json.loads(f)
        if r["type"] == "hello":
            r["hardware"] = hardware or {"board": "esp32-devkit", "acquisition": "FAKE sources (SIMULATED)",
                                         "config_problems": "none", "link": "uart0 921600 8N1"}
        if r["type"] == "status" and counters is not None:
            r["counters"] = counters
        out.append((json.dumps(r, separators=(",", ":")) + "\n").encode())
    return out


def _check(rep, name):
    found = [c for c in rep.checks if c.name == name]
    assert found, (name, [c.name for c in rep.checks])
    return found[0]


def test_serial_check_says_when_the_firmware_is_producing_fake_samples():
    res = serial_check("x", 921600, 2, adapter_factory=lambda p, b: _Lines(_firmware_stream()))
    c = _check(res.report, "acquisition is real")
    assert c.level == WARN and "FAKE" in c.detail and "synthetic" in c.fix
    assert _check(res.report, "data labelled live").level == WARN          # provenance simulated, as the README says


def test_serial_check_reports_the_firmwares_own_drop_counters_with_their_meaning():
    stream = _firmware_stream(counters={"emg_dropped_samples": 0, "tx_dropped_records": 12, "tx_dropped_bytes": 9000,
                                         "encode_errors": 0})
    res = serial_check("x", 921600, 2, adapter_factory=lambda p, b: _Lines(stream))
    c = _check(res.report, "device counters")
    assert c.level == WARN and "tx_dropped_records=12" in c.detail and "UART" in c.detail
    ok = serial_check("x", 921600, 2, adapter_factory=lambda p, b: _Lines(
        _firmware_stream(counters={"emg_dropped_samples": 0, "tx_dropped_records": 0})))
    assert _check(ok.report, "device counters").level == PASS


def test_serial_check_flags_config_problems_and_the_wrong_board():
    bad = _firmware_stream(hardware={"board": "esp32", "acquisition": "REAL", "config_problems": "imu range unset"})
    res = serial_check("x", 921600, 2, adapter_factory=lambda p, b: _Lines(bad))
    assert _check(res.report, "firmware configuration").level == WARN
    res2 = serial_check("x", 921600, 2, adapter_factory=lambda p, b: _Lines(_firmware_stream("vg-w-aaaaaaaaaaaa")),
                        expect_device_id="vg-w-2805a5075554")
    c = _check(res2.report, "device id")
    assert c.level == FAIL and "vg-w-aaaaaaaaaaaa" in c.detail
    res3 = serial_check("x", 921600, 2, adapter_factory=lambda p, b: _Lines(_firmware_stream()),
                        expect_device_id="vg-w-2805a5075554")
    assert _check(res3.report, "device id").level == PASS


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, 5, "vg/w"])
def test_profile_device_id_must_be_a_contract_id(tmp_path, bad):
    f = tmp_path / "hw.json"
    f.write_text(json.dumps({"name": "w", "baud": 921600, "emg": {"output": "raw"}, "device_id": bad}))
    with pytest.raises(ProfileError, match="device_id"):
        load_profile(f)


# ------------------------------------------------------------------------------ serve --device stream (vg-fake-device output)

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_reads_the_fake_device_stream_and_keeps_it_labelled_simulated(tmp_path):
    f = tmp_path / "fake.ndjson"
    f.write_bytes(b"".join(_firmware_stream(counters={"tx_dropped_records": 0, "emg_dropped_samples": 0})))
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "haven_backend.cli", "--data-dir", str(tmp_path / "data"), "serve",
                             "--device", "stream", "--stream", str(f), "--http-port", str(port), "--log-level", "warning"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        def get(path):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
                return json.loads(r.read())

        def done():
            try:
                return get("/api/runtimes/wearable")["state"] == "completed"
            except Exception:  # noqa: BLE001
                return False
        assert wait_until(done, 30)
        rt = get("/api/runtimes/wearable")
        assert rt["adapter"]["kind"] == "stream" and rt["provenance"]["source"] == "simulated"
        assert rt["device_id"] == "vg-w-2805a5075554" and rt["hello"]["hardware"]["acquisition"].startswith("FAKE")
        assert rt["data_fresh"] is False                         # a finished stream is not current data
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "facts_acq" in page and "DEVICE-REPORTED LOSS" in page
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_stream_needs_a_source_and_rejects_a_profile(tmp_path):
    from haven_backend.cli import main
    assert main(["serve", "--device", "stream", "--data-dir", str(tmp_path / "d")]) == 2
    prof = tmp_path / "hw.json"
    prof.write_text(json.dumps({"name": "w", "baud": 921600, "emg": {"output": "raw"}}))
    assert main(["serve", "--device", "stream", "--stream", str(tmp_path / "x"), "--profile", str(prof),
                 "--data-dir", str(tmp_path / "d")]) == 2
    assert main(["serve", "--device-id", "vg-w-x", "--data-dir", str(tmp_path / "d")]) == 2   # sim device: no id to match
    assert not (tmp_path / "d").exists()
