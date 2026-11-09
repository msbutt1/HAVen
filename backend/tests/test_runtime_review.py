"""Review round 3: runtime.py defects (silent-device detection, device switching, session start,
periodic work, replay of pre-session calibration). Every test is a proven bug and is
xfail(strict=True): when the defect is fixed the test XPASSes and strict mode fails the run, so
the marker must then be removed. SIMULATED data, offline, deterministic."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from conftest import ScriptedAdapter, sim_frames, wait_until
from test_provenance_and_replay import calibration_points

from haven_signals import contract
from haven_backend import runtime as runtime_mod
from haven_backend.service import processing_config_for

CFG = processing_config_for("sim")


def _rewrite_device(frames: list[bytes], device_id: str) -> list[bytes]:
    out = []
    for f in frames:
        r = json.loads(f)
        r["device_id"] = device_id
        r["boot_id"] = r["boot_id"].replace("sim-", "other-")
        out.append((json.dumps(r, separators=(",", ":")) + "\n").encode())
    return out


# ------------------------------------------------------------------ data_fresh / silent detection

def test_connected_link_that_never_delivers_anything_is_not_fresh(backend):
    rt = backend.add_runtime("wearable", ScriptedAdapter([], clock="host", hold_open=True), CFG)
    assert wait_until(lambda: rt.connection == "connected")
    time.sleep(1.2)  # > SILENT_S (0.5 s) of nothing at all
    assert rt.status()["data_fresh"] is False


def test_a_stream_of_only_invalid_frames_is_not_fresh(backend):
    frames = sim_frames(duration_s=10)
    garbage = [x for _ in range(80) for x in (b'{"not":"a record"}\n', ("pause", 0.03))]
    rt = backend.add_runtime("wearable", ScriptedAdapter([*frames[:300], *garbage], clock="host",
                                                         hold_open=True), CFG)
    assert wait_until(lambda: rt.counters["invalid_frames"] >= 40, 10)  # ~1.2 s after the last valid frame
    assert rt.counters["invalid_frames"] < 80  # garbage is still flowing
    assert rt.status()["data_fresh"] is False


def test_errored_runtime_is_not_fresh(backend):
    rt = backend.add_runtime("wearable", ScriptedAdapter(sim_frames(duration_s=3)[:80], clock="device",
                                                         hold_open=True), CFG)
    assert wait_until(lambda: rt.latest["emg"] is not None)
    rt.error = "RuntimeError: simulated crash"
    st = rt.status()
    assert st["state"] == "error" and st["data_fresh"] is False


def test_finished_source_is_not_fresh(backend):
    rt = backend.add_runtime("wearable", ScriptedAdapter(sim_frames(duration_s=3)[:100], clock="device"), CFG)
    assert wait_until(lambda: rt.finished, 10)
    assert rt.status()["data_fresh"] is False


# ------------------------------------------------------------------ state from rejected records

def test_a_retired_boot_record_does_not_change_the_reported_boot(backend):
    r1 = sim_frames(duration_s=3, run_id="r1")
    r2 = sim_frames(duration_s=3, run_id="r2")
    rt = backend.add_runtime("wearable", ScriptedAdapter([*r1[:50], *r2[:50], r1[51]], clock="device",
                                                         hold_open=True), CFG)
    assert wait_until(lambda: rt.events["retired_boot_record"] == 1, 10)
    assert rt.proc.boot_id == json.loads(r2[0])["boot_id"]
    assert rt.status()["boot_id"] == rt.proc.boot_id


# ------------------------------------------------------------------ device switching

def test_switching_device_keeps_the_new_devices_hello(backend):
    a = sim_frames(duration_s=5, run_id="a")
    b = _rewrite_device(sim_frames(duration_s=5, run_id="b"), "sim-wearable-02")
    assert json.loads(b[0])["type"] == "hello"
    rt = backend.add_runtime("wearable", ScriptedAdapter([*a[:60], *b[:200]], clock="device", hold_open=True), CFG)
    assert wait_until(lambda: rt.counters["records"] == 260, 10)
    assert wait_until(lambda: rt.device_id == "sim-wearable-02", 10)
    assert rt.hello is not None and rt.hello["device_id"] == "sim-wearable-02"


# ------------------------------------------------------------------ session start / periodic work

def test_failed_session_start_leaves_no_half_started_session(backend, monkeypatch):
    rt = backend.add_runtime("wearable", ScriptedAdapter(sim_frames(duration_s=5)[:100], clock="device",
                                                         hold_open=True), CFG)
    assert wait_until(lambda: rt.counters["records"] == 100)
    real = Path.write_text

    def full_disk(self, *a, **kw):
        if self.name == "manifest.json.tmp":
            raise OSError(28, "No space left on device")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", full_disk)
    raised = False
    try:
        backend.start_session("wearable", "x")
    except OSError:
        raised = True
    monkeypatch.setattr(Path, "write_text", real)
    active = rt.session is not None
    recording_rows = [r for r in backend.store.wearable_sessions() if r["state"] == "recording"]
    assert raised == (not active)  # an error means nothing started; no error means it is running
    assert len(recording_rows) == (1 if active else 0)


def test_a_failed_periodic_flush_does_not_stop_acquisition(backend, monkeypatch):
    frames = sim_frames(duration_s=10)
    rt = backend.add_runtime("wearable", ScriptedAdapter([*frames[:100], ("pause", 1.5), *frames[100:200]],
                                                         clock="host", hold_open=True), CFG)
    assert wait_until(lambda: rt.counters["records"] == 100)
    backend.start_session("wearable", "flush")
    real = runtime_mod.SessionRecorder.flush
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real(self)

    monkeypatch.setattr(runtime_mod.SessionRecorder, "flush", flaky)
    wait_until(lambda: rt.counters["records"] == 200 or rt.error is not None, 6)
    assert rt.error is None and rt.counters["records"] == 200


# ------------------------------------------------------------------ replay of pre-session calibration

@pytest.mark.xfail(strict=True, reason="only an ACTIVE calibration is saved as a pre-session command; a "
                   "relaxed reference captured before the session is not, so the in-session squeeze "
                   "calibrates live but is rejected in the replay (replay stays uncalibrated, unflagged)")
def test_replay_reproduces_a_calibration_whose_relaxed_half_predates_the_session(backend):
    frames = sim_frames(duration_s=30.0, run_id="half")
    (i1, a1), (i2, a2) = calibration_points(frames)
    g1, g2 = threading.Event(), threading.Event()
    script = [*frames[:i1 + 1], ("gate", g1), *frames[i1 + 1:i2 + 1], ("gate", g2), *frames[i2 + 1:]]
    rt = backend.add_runtime("wearable", ScriptedAdapter(script, clock="device"), CFG)

    def n(i: int) -> int:
        return sum(1 for f in frames[:i + 1] if contract.parse_line(f)[1] == [])

    assert wait_until(lambda: rt.counters["records"] == n(i1), 20)
    backend.calibrate("wearable", a1)
    g1.set()
    assert wait_until(lambda: rt.counters["records"] == n(i2), 20)
    assert rt.calibration_state == "relaxed_captured"
    row = backend.start_session("wearable", "half-calibrated")  # relaxed done before, squeeze inside
    backend.calibrate("wearable", a2)
    g2.set()
    assert wait_until(lambda: rt.finished and backend.session(row["session_id"])["state"] != "recording", 30)
    live = backend.session(row["session_id"])
    assert live["calibration_id"] and live["score"]["status"] == "ok"  # the live session was calibrated

    st = backend.start_replay(row["session_id"], fast=True)
    rep = backend.runtime(st["runtime"])
    assert wait_until(lambda: rep.finished, 30)
    assert rep.calibration_state == "calibrated"
