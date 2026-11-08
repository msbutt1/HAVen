"""Review: the whole backend driven by a FAITHFUL emulator of the ESP32 wearable firmware (tests/firmware_emulator.py).

The emulator is built from the firmware README, behind a pseudo-terminal (real pyserial, no mocks): every open of the
port resets the 'board' (ROM banner, new boot_id, hello seq 0), hello is re-sent every 5 s and status every 1 s with
the NEXT seq, whole frames are dropped when the frame queue is full (seq gap + unflagged sample-index jump), the
sample ring overruns (dropped_samples_before + counters), sensors disconnect, the board resets mid-session. All data is
SIMULATED. Plain tests verify what works; ``xfail(strict=True)`` tests reproduce a defect (when it is fixed the test
flips to XPASS and strict makes it fail loudly, so the marker must then be removed).

macOS note: a pty only accepts some baud rates (115200, 230400); the emulator models the 921600 wire in its TX pacing.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from conftest import wait_until
from firmware_emulator import DEVICE_ID_W, FirmwareEmulator
from vibeguard_signals import contract
from vibeguard_backend import runtime as runtime_mod
from vibeguard_backend.adapters import AdapterDisconnected, ReplayAdapter, SerialAdapter
from vibeguard_backend.hardware import find_baud, serial_check
from vibeguard_backend.preflight import FAIL, PASS, WARN
from vibeguard_backend.serial_select import DeviceIdSerialAdapter
from vibeguard_backend.service import Backend, processing_config_for
from vibeguard_backend.stream import StreamAdapter

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs pseudo-terminals")
BAUD = 115200  # a pty accepts this; the emulator paces its TX like a 921600 wire (92 kB/s)


# ------------------------------------------------------------------------------------------------ helpers

@pytest.fixture
def emu(tmp_path):
    made: list[FirmwareEmulator] = []

    def make(link: str | None = None, device_id: str = DEVICE_ID_W, **kw) -> FirmwareEmulator:
        e = FirmwareEmulator(tmp_path / link if link else None, device_id, **kw).start()
        made.append(e)
        return e
    yield make
    for e in made:
        e.close()


@pytest.fixture
def backend(tmp_path):
    b = Backend(tmp_path / "data")
    yield b
    b.shutdown(timeout=12)


@pytest.fixture
def events(monkeypatch):
    """Record every stream_event a DeviceRuntime emits (with its detail), per runtime."""
    log: dict[int, list[dict]] = {}
    orig = runtime_mod.DeviceRuntime._emit

    def spy(self, outs):
        for o in outs or []:
            if o.get("type") == "stream_event":
                log.setdefault(id(self), []).append(o)
        return orig(self, outs)
    monkeypatch.setattr(runtime_mod.DeviceRuntime, "_emit", spy)
    return lambda rt: log.get(id(rt), [])


def serial_runtime(backend, e: FirmwareEmulator, **kw):
    ad = SerialAdapter(e.path, BAUD, dtr=False, rts=False)
    return backend.add_runtime("wearable", ad, processing_config_for("serial"), **kw)


def check(rep, name):
    found = [c for c in rep.checks if c.name == name]
    assert found, (name, [c.name for c in rep.checks])
    return found[0]


class RecordingAdapter(SerialAdapter):
    """The real SerialAdapter, remembering every line it hands over (for offline re-use)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.lines: list[bytes] = []

    def read_line(self, timeout):
        line = super().read_line(timeout)
        if line is not None:
            self.lines.append(line)
        return line


# ---------------------------------------------------------------- one long, faulty run, shared by the offline tests

RUN_S = 11.0


def _fw_run_job(d: Path, out: dict) -> None:
    """serial_check (the real code path) against an emulator for RUN_S seconds, with faults injected on a schedule."""
    import threading
    e = FirmwareEmulator(d / "ttyUSB0").start()
    adapters: list[RecordingAdapter] = []

    def factory(p, b):
        adapters.append(RecordingAdapter(p, b, dtr=False, rts=False))
        return adapters[-1]

    def faults():
        time.sleep(1.2); e.drop_frames(3)            # 3 whole frames lost in the TX queue
        time.sleep(1.5); e.stall_serializer(1.5)     # the sample rings overrun
        time.sleep(3.0); e.disconnect_sensor("emg"); time.sleep(1.0); e.disconnect_sensor("emg", False)
        time.sleep(0.5); e.fail_encode(1)            # one batch cannot be encoded: no seq, an index jump
    t = threading.Thread(target=faults, daemon=True)
    t.start()
    try:
        res = serial_check(str(d / "ttyUSB0"), BAUD, RUN_S, adapter_factory=factory, expect_device_id=DEVICE_ID_W)
        t.join(5)
        time.sleep(0.2)
        out.update(res=res, lines=list(adapters[0].lines), dropped=list(e.dropped), counters=dict(e.counters),
                   emitted=list(e.emitted), contract_errors=list(e.contract_errors), adapter=adapters[0].describe())
    except BaseException as exc:  # noqa: BLE001 - re-raised in the fixture
        out["error"] = exc
    finally:
        e.close()


@pytest.fixture(scope="module", autouse=True)
def _fw_run_started(tmp_path_factory):
    """Start the long faulty run at the beginning of the module so it overlaps the other (independent) tests."""
    import threading
    out: dict = {}
    th = threading.Thread(target=_fw_run_job, args=(tmp_path_factory.mktemp("fw_run"), out), daemon=True)
    th.start()
    yield th, out
    th.join(30)


@pytest.fixture(scope="module")
def fw_run(_fw_run_started):
    th, out = _fw_run_started
    th.join(60)
    if "error" in out:
        raise out["error"]
    assert "res" in out
    return out


# ------------------------------------------------------------------------------------------------ the emulator itself

def test_the_emulator_behaves_like_the_readme_board(emu):
    e = emu(hello_period_s=1.0, status_period_s=0.5)
    ad = SerialAdapter(e.path, BAUD, dtr=False, rts=False)
    ad.open()
    t0, text = time.monotonic(), 0
    try:
        while time.monotonic() - t0 < 3.2:
            line = ad.read_line(0.2)
            text += bool(line) and not line.startswith(b"{")
    finally:
        ad.close()
    assert e.contract_errors == [] and text >= 5                       # every record valid; the ROM banner was there
    recs = [r for r in e.emitted]
    assert [r["seq"] for r in recs] == list(range(len(recs)))          # one counter per boot, shared by all types
    assert recs[0]["type"] == "hello" and recs[0]["seq"] == 0 and re.fullmatch(r"b-[0-9a-f]{16}", recs[0]["boot_id"])
    hellos = [r for r in recs if r["type"] == "hello"]
    assert len(hellos) >= 3 and all(h["seq"] > 0 for h in hellos[1:])  # re-sent with the NEXT seq
    assert {r["provenance"]["source"] for r in recs} == {"simulated"} and {r["device_id"] for r in recs} == {DEVICE_ID_W}
    hw = hellos[0]["hardware"]
    assert hw["acquisition"].startswith("FAKE") and hw["acquisition"].endswith("(SIMULATED)")
    assert {"board", "acquisition", "config_problems", "link"} <= set(hw)
    for name in ("emg", "imu"):                                        # first_sample_index counts attempts: contiguous here
        nxt = 0
        for r in (x for x in recs if x["type"] == f"{name}_batch"):
            assert r["first_sample_index"] == nxt
            nxt += len(r["samples"])
    assert 18_000 < e.bytes_written / 3.2 < 45_000                      # README: ~26-37 kB/s for EMG 1 kHz + IMU


# ------------------------------------------------------------------------------------------------ SerialAdapter + runtime

def test_banner_is_counted_and_skipped_and_a_resent_hello_is_not_a_duplicate_or_a_reboot(emu, backend, events):
    e = emu(hello_period_s=0.7)
    rt = serial_runtime(backend, e)
    assert wait_until(lambda: rt.hello is not None and rt.hello["seq"] > 0, 8)
    time.sleep(1.5)                                                     # at least two re-sent hellos have been processed
    evs = [o["event"] for o in events(rt)]
    assert evs.count("invalid_record") == rt.counters["invalid_frames"] >= 6   # banner lines: counted, skipped
    for bad in ("duplicate_record", "late_record", "foreign_device", "retired_boot_record", "seq_gap", "sample_gap"):
        assert bad not in evs                                           # a re-sent hello (next seq) is an ordinary record
    assert evs.count("boot_change") == 1 and rt.events.get("boot_change", 0) == 0   # only the very first boot, not counted
    assert rt.adapter.partial_discarded == 1                            # the garbage-glued first line is dropped, and said so
    st = rt.status()
    assert st["device_id"] == DEVICE_ID_W and st["provenance"] == {"source": "simulated"}
    snap = rt.snapshot()
    assert snap["device_status"]["counters"] == {"emg_dropped_samples": 0, "imu_dropped_samples": 0,
                                                 "tx_dropped_records": 0, "tx_dropped_bytes": 0, "encode_errors": 0}
    assert snap["hello"]["hardware"]["acquisition"].startswith("FAKE") and snap["hello"]["hardware"]["link"].startswith("uart0")


def test_connecting_in_the_middle_of_a_frame_drops_the_tail_and_counts_it(emu, backend):
    e = emu(reset_on_open=False, midline_on_connect=True, hello_period_s=60)
    rt = serial_runtime(backend, e)
    assert wait_until(lambda: rt.counters["records"] > 60, 8)
    assert rt.adapter.partial_discarded == 1 and rt.counters.get("invalid_frames", 0) == 0   # no false invalid frame
    assert rt.events.get("seq_gap", 0) == 0 and e.boot_n == 1                                  # not a reboot either


def test_serial_check_sees_the_firmwares_facts_and_its_faults(fw_run):
    rep, stats = fw_run["res"].report, fw_run["res"].stats
    assert fw_run["contract_errors"] == []
    assert check(rep, "device id").level == PASS
    assert check(rep, "acquisition is real").level == WARN and "FAKE" in check(rep, "acquisition is real").detail
    assert check(rep, "data labelled live").level == WARN
    assert 4.0 <= stats["hello_interval_s"] <= 6.0                                  # hello cadence: every 5 s
    dc = check(rep, "device counters")
    truth = fw_run["counters"]
    assert dc.level == WARN and stats["device_counters"] == truth
    assert truth["tx_dropped_records"] == 3 and truth["encode_errors"] == 1 and truth["emg_dropped_samples"] > 100
    assert stats["seq_missing"] == 3                                                # the encode error consumed no seq
    assert stats["dropped_samples"] == truth["emg_dropped_samples"] + truth["imu_dropped_samples"]
    assert stats["reboots"] == 0 and stats["unannounced_reboots"] == 0 and stats["seq_duplicates"] == 0   # re-sent hellos
    assert fw_run["adapter"]["partial_discarded"] == 1
    assert check(rep, "emg sample rate").level == PASS and check(rep, "imu sample rate").level == PASS


def test_a_captured_emulator_stream_through_the_runtime_classifies_the_gaps_it_can(fw_run, tmp_path, backend, events):
    f = tmp_path / "capture.ndjson"
    f.write_bytes(b"".join(l if l.endswith(b"\n") else l + b"\n" for l in fw_run["lines"]))
    rt = backend.add_runtime("wearable", StreamAdapter(str(f)), processing_config_for("serial"))
    assert wait_until(lambda: rt.finished, 30)
    ev = events(rt)
    gaps = [o for o in ev if o["event"] == "sample_gap"]
    overrun = [g for g in gaps if g["detail"]["cause"] == "device_overrun"]
    assert overrun and all(g["detail"]["missing_samples"] == g["detail"]["device_reported_dropped"] for g in overrun)
    assert sum(g["detail"]["missing_samples"] for g in overrun) == \
        fw_run["counters"]["emg_dropped_samples"] + fw_run["counters"]["imu_dropped_samples"]
    assert rt.events["seq_gap"] == 3                                                # 3 whole frames lost, counted by size
    assert rt.events["device_sensor_state"] >= 2                                    # emg disconnected, then ok again
    assert rt.provenance == {"source": "simulated"} and rt.error is None


# ------------------------------------------------------------------------------------------------ calibration, reset, sessions, replay

def test_reset_mid_session_is_a_boot_change_that_invalidates_calibration_and_the_session_replays(emu, backend, events):
    e = emu(hello_period_s=60)
    rt = serial_runtime(backend, e)
    assert wait_until(lambda: rt.counters["records"] > 100, 8)
    rt.calibrate("relaxed")
    assert wait_until(lambda: rt.calibration_state == "relaxed_captured", 10)
    e.emg_sigma = 400.0
    rt.calibrate("squeeze")
    assert wait_until(lambda: rt.calibration_state == "calibrated", 10)
    e.emg_sigma = 30.0
    sess = rt.start_session("emulator")
    time.sleep(1.5)
    boot_before = rt.boot_id
    e.reset(mid_frame=True)                                                         # a watchdog reset in the middle of a frame
    assert wait_until(lambda: rt.boot_id not in (None, boot_before), 8)
    assert wait_until(lambda: rt.calibration_state == "invalidated", 5)
    assert rt.calibration_reason == "boot_change" and rt.events["boot_change"] == 1
    time.sleep(1.0)
    row = rt.stop_session(sess["session_id"])
    assert row["state"] == "completed" and len(row["boot_ids"]) == 2 and row["counters"]["events"]["boot_change"] == 1
    assert row["counters"]["invalid_frames"] >= 7                                   # the second banner is in the recording
    tel = Path(row["dir"]) / "telemetry.ndjson"
    recorded = tel.read_bytes().splitlines()
    valid = [l for l in recorded if contract.parse_line(l)[1] == [] and contract.parse_line(l)[0] is not None]
    assert len(recorded) == row["counters"]["invalid_frames"] + len(valid)         # every line is in the file, none invented
    rr = backend.start_replay(row["session_id"], fast=True)
    rep_rt = backend.runtime(rr["runtime"])
    assert wait_until(lambda: rep_rt.finished, 30)
    assert rep_rt.counters["records"] == len(valid) and rep_rt.events.get("boot_change") == 1   # the replay reproduces the reset
    assert rep_rt.calibration_state == "invalidated" and rep_rt.provenance["source"] == "replay"
    assert len(rep_rt.adapter.skipped) == row["counters"]["invalid_frames"]        # banner lines skipped (and counted) in replay
    assert rep_rt.provenance["original_source"] == "simulated"


# ------------------------------------------------------------------------------------------------ two boards, one USB serial number

def test_two_boards_are_told_apart_by_device_id_and_the_runtime_follows_a_re_enumerated_board(emu, backend, tmp_path, events):
    emu("ttyUSB0", "vg-w-aaaaaaaaaaaa")
    w = emu("ttyUSB1")
    ad = DeviceIdSerialAdapter(str(tmp_path / "ttyUSB*"), BAUD, DEVICE_ID_W, dtr=False, rts=False)
    rt = backend.add_runtime("wearable", ad, processing_config_for("serial"))
    assert wait_until(lambda: rt.counters["records"] > 60, 10)
    assert rt.device_id == DEVICE_ID_W and rt.counters.get("foreign_frames", 0) == 0
    assert ad.seen[str(tmp_path / "ttyUSB0")] == "vg-w-aaaaaaaaaaaa" and ad.port.endswith("ttyUSB1")
    boot1 = rt.boot_id
    w.close()                                                                       # unplug; it comes back as ttyUSB2
    (tmp_path / "ttyUSB1").unlink()
    emu("ttyUSB2")
    assert wait_until(lambda: rt.boot_id not in (None, boot1) and rt.connection == "connected" and rt.counters["records"] > 150, 15)
    assert ad.port.endswith("ttyUSB2") and rt.counters["connection_connected"] == 2
    assert rt.events.get("boot_change") == 1 and rt.counters.get("foreign_frames", 0) == 0


def test_find_baud_scores_the_emulated_board_per_rate(emu):
    e = emu()
    res = find_baud(e.path, [115200, 230400], probe_s=1.2, dtr=False, rts=False)
    assert [r["baud"] for r in res] == [115200, 230400] and all(r["valid"] >= 30 and r["share"] > 0.8 for r in res)
    assert e.boot_n >= 2                                                            # every open reset the board


# ------------------------------------------------------------------------------------------------ DEFECTS (xfail strict)

def test_hello_seq0_behind_the_rom_banner_is_not_lost_on_connect(emu, backend):
    e = emu(hello_period_s=60)                          # no re-send can mask the loss
    rt = serial_runtime(backend, e)
    assert wait_until(lambda: rt.counters["records"] > 30, 8)
    assert rt.hello is not None and rt.hello["seq"] == 0


def test_after_a_reset_the_hello_shown_belongs_to_the_current_boot(emu, backend):
    e = emu(hello_period_s=2.0)
    rt = serial_runtime(backend, e)
    assert wait_until(lambda: rt.hello is not None and rt.counters["records"] > 60, 8)   # a re-sent hello arrived
    old = rt.boot_id
    e.reset()
    assert wait_until(lambda: rt.boot_id not in (None, old), 5)
    assert rt.hello is not None and rt.hello["boot_id"] == rt.boot_id                   # still the old boot's hello


@pytest.mark.xfail(strict=True, reason="tx-queue drops are device-side (tx_dropped_records) but the sample_gap cause says "
                   "'transport_or_unreported', and the encode_errors case (no seq gap at all) gets the same label")
def test_firmware_side_drops_are_not_labelled_transport(fw_run, tmp_path, backend, events):
    f = tmp_path / "capture.ndjson"
    f.write_bytes(b"".join(l if l.endswith(b"\n") else l + b"\n" for l in fw_run["lines"]))
    rt = backend.add_runtime("wearable", StreamAdapter(str(f)), processing_config_for("serial"))
    assert wait_until(lambda: rt.finished, 30)
    assert fw_run["counters"]["tx_dropped_records"] == 3 and fw_run["counters"]["encode_errors"] == 1
    gaps = [o for o in events(rt) if o["event"] == "sample_gap"]
    assert gaps and all(g["detail"]["cause"] != "transport_or_unreported" for g in gaps)


def test_serial_check_does_not_call_flagged_overruns_unflagged_holes(fw_run):
    lost = {r["seq"] for r in fw_run["dropped"]}
    nxt: dict[str, int] = {}
    unflagged = 0                                                  # truth from the wire: index gap minus what the device flagged
    for r in fw_run["emitted"]:
        if r["seq"] in lost or not r["type"].endswith("_batch"):
            continue
        if r["type"] in nxt:
            unflagged += r["first_sample_index"] - nxt[r["type"]] - r["quality"].get("dropped_samples_before", 0)
        nxt[r["type"]] = r["first_sample_index"] + len(r["samples"])
    assert unflagged > 0
    detail = check(fw_run["res"].report, "no records lost").detail
    m = re.search(r"(\d+) sample\(s\) missing from the sample index \(not flagged", detail)
    assert m and int(m.group(1)) == unflagged


def test_serial_check_with_device_id_does_not_call_the_port_ambiguous(emu, tmp_path):
    emu("ttyUSB0", "vg-w-aaaaaaaaaaaa")
    emu("ttyUSB1")
    res = serial_check(str(tmp_path / "ttyUSB*"), BAUD, 2, dtr=False, rts=False, expect_device_id=DEVICE_ID_W,
                       adapter_factory=lambda p, b: DeviceIdSerialAdapter(p, b, DEVICE_ID_W, dtr=False, rts=False))
    assert check(res.report, "device id").level == PASS
    amb = [c for c in res.report.checks if c.name == "port is ambiguous"]
    assert not amb or str(tmp_path / "ttyUSB1") not in amb[0].detail.split("others:")[-1]


def test_probing_silent_ports_does_not_make_the_runtime_unstoppable(tmp_path, backend):
    import pty
    import tty
    keep = []
    for n in ("ttyUSB0", "ttyUSB1", "ttyUSB2"):
        m, s = pty.openpty()
        tty.setraw(s)
        (tmp_path / n).symlink_to(os.ttyname(s))
        keep.append((m, s))
    try:
        ad = DeviceIdSerialAdapter(str(tmp_path / "ttyUSB*"), BAUD, DEVICE_ID_W, dtr=False, rts=False, probe_s=1.5)
        rt = backend.add_runtime("wearable", ad, processing_config_for("serial"))
        time.sleep(0.5)
        t0 = time.monotonic()
        stopped = rt.stop(1.5)                                  # three silent ports: the thread is inside open() for ~4.5 s
        assert stopped and time.monotonic() - t0 < 2.0
    finally:
        for m, s in keep:
            os.close(m)
            os.close(s)


def test_serial_check_device_id_adapter_uses_the_profiles_dtr_rts(tmp_path, monkeypatch, capsys):
    import vibeguard_backend.serial_select as sel
    from vibeguard_backend.cli import main
    seen = {}

    class Spy(sel.DeviceIdSerialAdapter):
        def __init__(self, *a, **k):
            seen.update(k)
            super().__init__(*a, **k)
    monkeypatch.setattr(sel, "DeviceIdSerialAdapter", Spy)
    prof = tmp_path / "hw.json"
    prof.write_text(json.dumps({"name": "w", "baud": 921600, "emg": {"output": "raw"}, "device_id": DEVICE_ID_W,
                                "serial": {"dtr": False, "rts": False}}))
    main(["serial-check", "--port", str(tmp_path / "nothing*"), "--profile", str(prof), "--seconds", "1"])
    assert seen.get("dtr") is False and seen.get("rts") is False


def test_try_bauds_honours_device_id_and_the_profile(tmp_path, monkeypatch, capsys):
    import vibeguard_backend.hardware as hw
    from vibeguard_backend.cli import main
    seen = {}

    def fake_find(port, bauds, probe_s=3.0, probe_max_s=10.0, adapter_factory=None, progress=None, dtr=None, rts=None):
        seen.update(factory=adapter_factory, dtr=dtr, rts=rts)
        return [{"baud": bauds[0], "lines": 10, "valid": 10, "share": 1.0}]
    monkeypatch.setattr(hw, "find_baud", fake_find)
    monkeypatch.setattr(hw, "serial_check", lambda *a, **k: hw.SerialCheckResult("p", 1, 1, hw.Report(), {}))
    prof = tmp_path / "hw.json"
    prof.write_text(json.dumps({"name": "w", "baud": 921600, "emg": {"output": "raw"}, "device_id": DEVICE_ID_W,
                                "serial": {"dtr": False, "rts": False}}))
    main(["serial-check", "--port", str(tmp_path / "ttyUSB*"), "--profile", str(prof), "--try-bauds", "921600,460800"])
    assert seen["factory"] is not None and seen["dtr"] is False and seen["rts"] is False


def test_stream_adapter_caps_a_newline_free_source(tmp_path):
    f = tmp_path / "noise.bin"
    f.write_bytes(b"x" * 3_000_000)
    ad = StreamAdapter(str(f))
    ad.open()
    try:
        line = ad.read_line(1.0)
        assert line is not None and len(line) <= contract.MAX_LINE_BYTES + 1
    finally:
        ad.close()
