"""Sessions survive restart, offline.

Acceptance dimension: "Sessions survive restart and the local demo works without
cloud services" (abduljabbar_05_testing_and_acceptance.md), plus "a disconnect
produces visible status, not silently stale data" and "simulated history is labelled".

A real crash is produced by running a backend in a child process and SIGKILLing it,
so nothing in the normal stop path runs. A second Backend on the same data_dir must
then account for everything the first one left behind.
"""

from __future__ import annotations

import ast
import functools
import json
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from conftest import ScriptedAdapter, sim_frames, wait_until
from haven_signals import contract
from haven_signals.simulator import SimConfig, Simulation
from haven_backend.runtime import RuntimeConflict
from haven_backend.service import Backend, processing_config_for

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_SRC = TESTS_DIR.parent / "src"
SIGNALS_SRC = TESTS_DIR.parents[1] / "signals" / "src"

# ----------------------------------------------------------------------------- helpers


@functools.lru_cache(maxsize=4)
def _demo30(run_id: str) -> tuple[bytes, ...]:
    return tuple(sim_frames(duration_s=30, run_id=run_id))


def demo30(run_id: str = "t1") -> list[bytes]:
    """The 30 s demo scenario (its phase times scale with duration, so it must be 30 s)."""
    return list(_demo30(run_id))


def first_sample_s(frame: bytes) -> float | None:
    t = json.loads(frame).get("timing")
    if not t:
        return None
    return (t["t0_us"] if t["mode"] == "uniform" else t["t_us"][0]) / 1e6


def index_at(frames: list[bytes], t_s: float) -> int:
    """Index of the first batch whose first sample is at or after ``t_s`` (device time)."""
    for i, f in enumerate(frames):
        t = first_sample_s(f)
        if t is not None and t >= t_s:
            return i
    raise AssertionError(f"no frame at or after {t_s} s")


def complete_lines(path: Path) -> tuple[list[bytes], bytes]:
    """(newline-terminated lines, trailing partial bytes) of a file as it is on disk."""
    data = path.read_bytes()
    *lines, tail = data.split(b"\n")
    return [ln + b"\n" for ln in lines], tail


def assert_all_valid(lines: list[bytes]) -> None:
    for i, ln in enumerate(lines):
        rec, issues = contract.parse_line(ln)
        assert rec is not None, f"line {i + 1} on disk is not a valid contract record: {issues}"


def session_dir(row: dict) -> Path:
    return Path(row["dir"])


def wait_row(backend: Backend, sid: str, state: str, timeout: float = 10.0) -> dict:
    assert wait_until(lambda: backend.session(sid)["state"] == state, timeout), backend.session(sid)
    return backend.session(sid)


CHILD = r'''
import json, sys, threading, time
sys.path.insert(0, sys.argv[1])
from conftest import ScriptedAdapter, sim_frames, wait_until
from haven_backend.adapters import SimAdapter
from haven_backend.service import Backend, processing_config_for

data_dir, scenario = sys.argv[2], sys.argv[3]
b = Backend(data_dir)
cfg = processing_config_for("sim")
out = {"pid_ok": True}

def check(ok, what):
    if not ok:
        print("CHILD_FAILED " + what, file=sys.stderr, flush=True)
        sys.exit(3)

def ready():
    print(json.dumps(out), flush=True)
    while True:          # wait to be killed; nothing after this point runs
        time.sleep(60)

if scenario == "recover":
    frames = sim_frames(duration_s=5)
    g1, g2 = threading.Event(), threading.Event()
    script = [frames[0], ("gate", g1), *frames[1:11], ("gate", g2), *frames[11:31],
              ("pause", 1.2), frames[31]]
    rt = b.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True), cfg)
    check(wait_until(lambda: rt.counters["records"] == 1), "first record")
    done = b.start_session("wearable", "finished before crash", request_id="req-start-done")
    g1.set()
    check(wait_until(lambda: rt.counters["records"] == 11), "11 records")
    b.stop_session(done["session_id"], request_id="req-stop-done")
    cut = b.start_session("wearable", "cut off by crash", request_id="req-start-cut")
    g2.set()
    # frames[31] arrives >= 1.2 s after the recorder opened, so the recorder flushed
    check(wait_until(lambda: rt.session is not None and rt.session.rec.lines == 21), "21 lines")
    st_done = b.start_station_session("simulated", "station finished before crash")
    for i, resp in enumerate(("felt", "not_felt")):
        b.add_trial(st_done["station_session_id"], {"motor_setting": 100 + i, "units": "pwm_duty_0_255"}, resp)
    b.finish_station_session(st_done["station_session_id"], "completed")
    st_open = b.start_station_session("simulated", "station cut off by crash",
                                      wearable_session_id=cut["session_id"])
    b.add_trial(st_open["station_session_id"], {"motor_setting": 90, "units": "pwm_duty_0_255"}, "felt",
                request_id="req-trial-1")
    out.update(done=done["session_id"], cut=cut["session_id"], st_done=st_done["station_session_id"],
               st_open=st_open["station_session_id"])
    ready()

elif scenario == "silent_tail":
    frames = sim_frames(duration_s=5)
    g = threading.Event()
    script = [frames[0], ("gate", g), *frames[1:6], "disconnect"]
    rt = b.add_runtime("wearable", ScriptedAdapter(script, clock="device", auto_reconnect=False), cfg)
    check(wait_until(lambda: rt.counters["records"] == 1), "first record")
    s = b.start_session("wearable", "device unplugged, then backend crashed")
    g.set()
    check(wait_until(lambda: rt.connection == "disconnected" and rt.session is not None
                     and rt.session.rec.lines == 5), "5 lines then disconnect")
    time.sleep(1.5)      # the scenario under test: 1.5 s of silence before the crash
    out.update(session=s["session_id"], lines=5)
    ready()

elif scenario == "paced":
    ad = SimAdapter(scenario="demo", seed=1, duration_s=30, loop=False, run_id="paced")
    rt = b.add_runtime("wearable", ad, cfg)
    check(wait_until(lambda: rt.counters["records"] >= 2), "first records")
    s = b.start_session("wearable", "paced crash")
    check(wait_until(lambda: rt.session is not None and rt.session.rec.lines >= 100, 15), "100 lines")
    out.update(session=s["session_id"], lines=rt.session.rec.lines)
    ready()
'''


def crash_child(tmp_path: Path, scenario: str, timeout: float = 25.0) -> tuple[Path, dict]:
    """Run a backend in a child process, wait for its ready line, then SIGKILL it."""
    script = tmp_path / "crash_child.py"
    script.write_text(CHILD)
    data = tmp_path / "data"
    proc = subprocess.Popen([sys.executable, str(script), str(TESTS_DIR), str(data), scenario],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        line = proc.stdout.readline() if ready else b""  # type: ignore[union-attr]
        if not line:
            proc.kill()
            _, err = proc.communicate(timeout=5)
            pytest.fail(f"crash child ({scenario}) never became ready: {err.decode()[-2000:]}")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        assert proc.returncode == -signal.SIGKILL
        return data, json.loads(line)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()  # type: ignore[union-attr]
        proc.stderr.close()  # type: ignore[union-attr]


@pytest.fixture(scope="module")
def crashed_recover(tmp_path_factory) -> tuple[Path, dict]:
    """One killed 'recover' child per module; nothing has opened its data dir since the kill."""
    return crash_child(tmp_path_factory.mktemp("crash_recover"), "recover")


def recovered_copy(crashed: tuple[Path, dict], tmp_path: Path) -> tuple[Path, dict]:
    """Private copy of the crashed data dir (database + session files, WAL included), with
    the session ``dir`` columns repointed at the copy so each test restarts from the
    exact post-kill state."""
    src, ids = crashed
    dst = tmp_path / "data"
    shutil.copytree(src, dst)
    db = sqlite3.connect(dst / "vibeguard.sqlite3")
    try:
        db.execute("UPDATE wearable_sessions SET dir = replace(dir, ?, ?)", (str(src), str(dst)))
        db.commit()
    finally:
        db.close()
    return dst, ids


def calibrate_demo(backend: Backend, frames: list[bytes], end_s: float = 9.0):
    """Feed the demo scenario and issue relaxed/squeeze at the demo schedule (0.5 s, 4.5 s)."""
    i_rel, i_sq, i_end = index_at(frames, 0.5), index_at(frames, 4.5), index_at(frames, end_s)
    g1, g2 = threading.Event(), threading.Event()
    script = [*frames[:i_rel], ("gate", g1), *frames[i_rel:i_sq], ("gate", g2), *frames[i_sq:i_end]]
    rt = backend.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == i_rel)
    backend.calibrate("wearable", "relaxed")
    g1.set()
    assert wait_until(lambda: rt.counters["records"] == i_sq)
    backend.calibrate("wearable", "squeeze")
    g2.set()
    assert wait_until(lambda: rt.counters["records"] == i_end)
    assert wait_until(lambda: rt.status()["calibration"]["state"] == "calibrated"), rt.status()["calibration"]
    return rt


def emg_features(derived_path: Path) -> list[dict]:
    out = []
    for ln in derived_path.read_text().splitlines():
        rec = json.loads(ln)
        if rec["type"] == "stream_features" and rec["stream"] == "emg":
            out.append(rec)
    return out


# ----------------------------------------------------------------------------- (1) crash recovery


def test_kill_mid_session_is_recovered_as_interrupted_with_everything_else_intact(crashed_recover, tmp_path):
    data, ids = recovered_copy(crashed_recover, tmp_path)
    frames = sim_frames(duration_s=5)

    b = Backend(data)
    try:
        assert b.recovered == {"wearable_sessions_interrupted": 1, "station_sessions_interrupted": 1}

        cut = b.session(ids["cut"])
        assert cut["state"] == "interrupted" and cut["end_reason"] == "backend_restart"
        assert cut["ended_at"] is not None and cut["ended_at"] >= cut["started_at"]
        # provenance and identity survive the crash
        assert cut["label"] == "cut off by crash"
        assert cut["source"] == "simulated" and cut["recording_id"] == "sim-t1"
        assert cut["device_id"] == "sim-wearable-01" and cut["boot_ids"] == ["sim-t1-b0"]
        assert cut["simulated_history"] is False

        # telemetry that the recorder had flushed is on disk, byte for byte as received
        lines, tail = complete_lines(session_dir(cut) / "telemetry.ndjson")
        # Guarantee: everything older than the flush interval (1 s) survives a kill. The 21st
        # frame arrived 0.2 s after the last periodic flush, so it may legitimately be lost.
        assert len(lines) >= 20, f"only {len(lines)} of the 20 older lines survived the crash"
        assert lines == [f if f.endswith(b"\n") else f + b"\n" for f in frames[11:11 + len(lines)]]
        assert_all_valid(lines)
        # no clean close happened, but the manifest written at session start survives: it carries
        # the processing config a replay needs
        m = json.loads((session_dir(cut) / "manifest.json").read_text())
        # recovery rewrote its session block (state interrupted, ended when the last frame arrived)
        assert m["session"]["state"] == "interrupted" and m["processing_config"]["imu"]["conversion"]
        assert m["session"]["ended_at"] is not None

        # the session completed before the crash is untouched
        done = b.session(ids["done"])
        assert done["state"] == "completed" and done["end_reason"] == "stopped_by_user"
        assert done["counters"]["frames"] == 10
        for name, meta in done["files"].items():
            assert Path(meta["path"]).stat().st_size == meta["bytes"], name
        assert (session_dir(done) / "manifest.json").is_file()

        # station records: finished one intact, open one interrupted with its trial kept
        st_done = b.station_session(ids["st_done"])
        assert st_done["state"] == "completed" and [t["response"] for t in st_done["trials"]] == ["felt", "not_felt"]
        assert st_done["summary"]["trials"] == 2
        st_open = b.station_session(ids["st_open"])
        assert st_open["state"] == "interrupted" and st_open["end_reason"] == "backend_restart"
        assert st_open["wearable_session_id"] == ids["cut"]
        assert [t["trial_index"] for t in st_open["trials"]] == [0]
        with pytest.raises(RuntimeConflict):
            b.add_trial(ids["st_open"], {"motor_setting": 80, "units": "pwm_duty_0_255"}, "felt")
        # a retried trial (same request_id) after restart returns the stored trial, adds nothing
        again = b.add_trial(ids["st_open"], {"motor_setting": 90, "units": "pwm_duty_0_255"}, "felt",
                            request_id="req-trial-1")
        assert again["trial_index"] == 0 and len(b.station_session(ids["st_open"])["trials"]) == 1

        # stopping an interrupted session is a no-op, not an error and not a state change
        assert b.stop_session(ids["cut"])["state"] == "interrupted"
        assert b.session(ids["cut"])["end_reason"] == "backend_restart"

        # history after restart shows all four records with their provenance
        hist = b.history()
        kinds = sorted((i["kind"], i.get("session_id") or i.get("station_session_id")) for i in hist["items"])
        assert kinds == sorted([("wearable", ids["cut"]), ("wearable", ids["done"]),
                                ("station", ids["st_done"]), ("station", ids["st_open"])])
        assert all(i["source"] == "simulated" for i in hist["items"])
    finally:
        b.shutdown()

    # a second restart has nothing left to recover and changes nothing
    c = Backend(data)
    try:
        assert c.recovered == {"wearable_sessions_interrupted": 0, "station_sessions_interrupted": 0}
        assert c.session(ids["cut"])["end_reason"] == "backend_restart"
        assert c.station_session(ids["st_done"])["state"] == "completed"
    finally:
        c.shutdown()


def test_retried_start_after_crash_does_not_duplicate_the_session(crashed_recover, tmp_path):
    data, ids = recovered_copy(crashed_recover, tmp_path)
    b = Backend(data)
    try:
        b.add_runtime("wearable", ScriptedAdapter([], hold_open=True), processing_config_for("sim"))
        assert wait_until(lambda: b.runtime("wearable").connection == "connected")
        before = {r["session_id"] for r in b.store.wearable_sessions()}
        again = b.start_session("wearable", "cut off by crash", request_id="req-start-cut")
        assert again["session_id"] == ids["cut"]
        assert {r["session_id"] for r in b.store.wearable_sessions()} == before
        assert b.runtime("wearable").session is None  # nothing started recording
        # a retried stop of the completed session is equally harmless
        assert b.stop_session(ids["done"], request_id="req-stop-done")["session_id"] == ids["done"]
        assert b.session(ids["done"])["state"] == "completed"
    finally:
        b.shutdown()


def test_interrupted_session_replays_with_replay_provenance_after_restart(crashed_recover, tmp_path):
    data, ids = recovered_copy(crashed_recover, tmp_path)
    b = Backend(data)
    try:
        st = b.start_replay(ids["cut"], fast=True)
        rt = b.runtime(st["runtime"])
        assert wait_until(lambda: not rt.alive, 15)
        assert rt.error is None and rt.finished
        assert rt.counters["records"] >= 20  # only what was flushed before the kill
        assert rt.provenance["source"] == "replay"
        assert rt.provenance["original_source"] == "simulated"
        assert rt.provenance["recording_id"] == "sim-t1"
        # a replay never borrows a stored calibration
        assert rt.counters["calibration_restored"] == 0
        # replaying did not touch the interrupted original
        assert b.session(ids["cut"])["state"] == "interrupted"
    finally:
        b.shutdown()


def test_health_reports_what_was_recovered_on_start(crashed_recover, tmp_path):
    from fastapi.testclient import TestClient
    from haven_backend.app import create_app

    data, ids = recovered_copy(crashed_recover, tmp_path)
    b = Backend(data)
    try:
        with TestClient(create_app(b), base_url="http://localhost") as client:
            health = client.get("/api/health").json()
            assert health["recovered_on_start"] == {"wearable_sessions_interrupted": 1,
                                                    "station_sessions_interrupted": 1}
            row = client.get(f"/api/sessions/{ids['cut']}").json()
            assert row["state"] == "interrupted" and row["end_reason"] == "backend_restart"
            r = client.get(f"/api/sessions/{ids['cut']}/files/telemetry.ndjson")
            assert r.status_code == 200 and r.content.count(b"\n") >= 20
            assert client.get(f"/api/sessions/{ids['cut']}/files/manifest.json").status_code == 200
    finally:
        b.shutdown()


def test_paced_stream_loses_at_most_about_one_second_on_kill(tmp_path):
    """Continuous 20 Hz-per-stream simulated device: the 1 s flush bounds the loss."""
    data, info = crash_child(tmp_path, "paced")
    b = Backend(data)
    try:
        assert b.recovered["wearable_sessions_interrupted"] == 1
        row = b.session(info["session"])
        assert row["state"] == "interrupted" and row["source"] == "simulated"
        lines, _tail = complete_lines(session_dir(row) / "telemetry.ndjson")
        assert_all_valid(lines)
        # demo sim emits ~40 frames/s (emg + imu batches at 20 Hz each, plus status);
        # anything older than ~1 s before the kill must be on disk.
        assert len(lines) >= info["lines"] - 50, (len(lines), info["lines"])
        seqs = [json.loads(ln)["seq"] for ln in lines]
        assert seqs == sorted(seqs)
    finally:
        b.shutdown()


def test_frames_recorded_before_an_unplug_survive_a_later_crash(tmp_path):
    """SessionRecorder documents 'bounded data loss on a crash' (1 s). Frames received
    1.5 s before the kill, followed by a device unplug, must therefore be on disk."""
    data, info = crash_child(tmp_path, "silent_tail")
    b = Backend(data)
    try:
        row = b.session(info["session"])
        assert row["state"] == "interrupted"
        lines, tail = complete_lines(session_dir(row) / "telemetry.ndjson")
        assert len(lines) == info["lines"], (
            f"{len(lines)} of {info['lines']} frames received 1.5 s before the crash are on disk "
            f"({len(tail)} partial bytes)")
    finally:
        b.shutdown()


def test_seed_history_cli_while_serving_does_not_interrupt_the_live_session(tmp_path, sim_config):
    from haven_backend.cli import main as cli_main

    data = tmp_path / "data"
    frames = sim_frames(duration_s=3)
    a = Backend(data)
    try:
        g = threading.Event()
        rt = a.add_runtime("wearable", ScriptedAdapter([frames[0], ("gate", g), *frames[1:20]],
                                                       clock="device", hold_open=True), sim_config)
        assert wait_until(lambda: rt.counters["records"] == 1)
        s = a.start_session("wearable", "recording while history is seeded")
        g.set()
        assert wait_until(lambda: rt.session is not None and rt.session.rec.lines == 19)

        # documented CLI usage, run against the same data dir while the server is up
        assert cli_main(["--data-dir", str(data), "seed-history", "--days", "1"]) == 0

        assert rt.session is not None, "the runtime is still recording"
        row = a.session(s["session_id"])
        assert (row["state"], row["end_reason"]) == ("recording", None), (
            f"live session row now says {row['state']}/{row['end_reason']} while still recording")
        stopped = a.stop_session(s["session_id"])
        assert stopped["state"] == "completed" and rt.session is None
    finally:
        a.shutdown()


# ----------------------------------------------------------------------------- (2) persistence


def test_completed_records_and_calibrations_persist_across_backend_instances(tmp_path):
    from haven_backend.history import seed_history

    data = tmp_path / "data"
    frames = demo30()
    a = Backend(data)
    rt = calibrate_demo(a, frames, end_s=9.0)
    cal_id = rt.status()["calibration"]["calibration_id"]
    assert cal_id
    s = a.start_session("wearable", "after calibration")
    assert wait_until(lambda: rt.session is not None)
    a.stop_session(s["session_id"])
    st = a.start_station_session("live", "operator test", protocol={"kind": "manual"})
    a.add_trial(st["station_session_id"], {"motor_setting": 120, "units": "pwm_duty_0_255"}, "felt",
                response_latency_ms=420.0, responded_at="2026-11-07T10:00:01.000Z",
                presented_at="2026-11-07T10:00:00.580Z")
    a.add_trial(st["station_session_id"], {"motor_setting": 0, "units": "pwm_duty_0_255"}, "missing",
                is_catch=True)
    a.finish_station_session(st["station_session_id"], "aborted", "wearer left")
    hist = seed_history(a, days=1, duration_s=6)
    before = {
        "wearable": {r["session_id"]: r for r in a.store.wearable_sessions()},
        "station": {r["station_session_id"]: a.station_session(r["station_session_id"])
                    for r in a.store.station_sessions()},
        "cal": a.store.calibrations(),
        "devices": a.store.devices(),
    }
    a.shutdown()

    b = Backend(data)
    try:
        assert b.recovered == {"wearable_sessions_interrupted": 0, "station_sessions_interrupted": 0}
        assert {r["session_id"]: r for r in b.store.wearable_sessions()} == before["wearable"]
        assert {r["station_session_id"]: b.station_session(r["station_session_id"])
                for r in b.store.station_sessions()} == before["station"]
        assert b.store.calibrations() == before["cal"]
        assert [c["calibration_id"] for c in b.store.calibrations()].count(cal_id) == 1
        saved = b.store.latest_calibration("sim-wearable-01", "simulated")
        assert saved["calibration_id"] == cal_id and saved["record"]["calibration_id"] == cal_id
        assert b.store.devices() == before["devices"]

        row = b.session(s["session_id"])
        assert row["state"] == "completed" and row["calibration_id"] == cal_id
        assert row["source"] == "simulated" and row["simulated_history"] is False

        sta = b.station_session(st["station_session_id"])
        assert sta["state"] == "aborted" and sta["end_reason"] == "wearer left" and sta["source"] == "live"
        assert [(t["response"], t["is_catch"], t["response_latency_ms"]) for t in sta["trials"]] == \
            [("felt", False, 420.0), ("missing", True, None)]

        # simulated history stays labelled as simulated after restart
        items = {i.get("session_id") or i.get("station_session_id"): i for i in b.history()["items"]}
        for sid in hist["wearable_sessions"] + hist["station_sessions"]:
            assert items[sid]["source"] == "simulated" and items[sid]["simulated_history"] is True
        assert items[s["session_id"]]["simulated_history"] is False
        assert items[st["station_session_id"]]["simulated_history"] is False
        assert b.history()["labels"]["simulated"].startswith("SIMULATED")
        # filtered history keeps live and simulated apart
        assert {i["source"] for i in b.history(source="live")["items"]} == {"live"}
    finally:
        b.shutdown()


# ----------------------------------------------------------------------------- (3) calibration restore


def test_calibration_is_restored_and_verified_for_the_same_boot(tmp_path):
    data = tmp_path / "data"
    frames = demo30()
    a = Backend(data)
    cal_id = calibrate_demo(a, frames, end_s=9.0).status()["calibration"]["calibration_id"]
    a.shutdown()

    b = Backend(data)
    try:
        later = frames[index_at(frames, 10.0):index_at(frames, 15.0)]
        g = threading.Event()
        rt = b.add_runtime("wearable", ScriptedAdapter([("gate", g), *later], clock="device", hold_open=True),
                           processing_config_for("sim"))
        assert wait_until(lambda: rt.connection == "connected")
        s = b.start_session("wearable", "after restart")
        g.set()
        assert wait_until(lambda: rt.counters["records"] == len(later))
        assert rt.counters["calibration_restored"] == 1
        cal = rt.status()["calibration"]
        assert cal["state"] == "calibrated" and cal["calibration_id"] == cal_id, cal
        norm = rt.latest["emg"]["features"]["emg_activation"]["normalized"]
        assert norm["calibration_id"] == cal_id
        assert "calibration_unverified" not in norm["reasons"]
        assert norm["value"] is not None, norm
        b.stop_session(s["session_id"])
        row = b.session(s["session_id"])
        assert row["calibration_id"] == cal_id
        # restoring did not create a second copy of the stored calibration
        assert [c["calibration_id"] for c in b.store.calibrations()] == [cal_id]
    finally:
        b.shutdown()


def _reboot_frames(after_s: float, until_s: float) -> list[bytes]:
    """Frames of the SAME recording (sim-t1) after a device reboot: boot sim-t1-b1."""
    sim = Simulation(SimConfig(scenario="reboot", duration_s=30, seed=1), run_id="t1")
    recs = [r.record for r in sim.records() if not r.lost]
    boots = sorted({r["boot_id"] for r in recs})
    assert len(boots) >= 2 and recs[0]["provenance"]["recording_id"] == "sim-t1"
    second = [contract.serialize_record(r).encode() for r in recs if r["boot_id"] == boots[1]]
    return second[index_at(second, after_s):index_at(second, until_s)]


@pytest.mark.parametrize("other", ["new_run", "device_rebooted"])
def test_calibration_from_another_boot_is_invalidated_never_applied(tmp_path, other):
    data = tmp_path / "data"
    a = Backend(data)
    cal_id = calibrate_demo(a, demo30(), end_s=9.0).status()["calibration"]["calibration_id"]
    a.shutdown()

    if other == "new_run":
        fr = demo30("t2")
        frames, expected = fr[index_at(fr, 10.0):index_at(fr, 15.0)], {"provenance_mismatch", "boot_mismatch"}
    else:
        frames, expected = _reboot_frames(1.0, 6.0), {"boot_mismatch"}
    assert json.loads(frames[0])["device_id"] == "sim-wearable-01"

    b = Backend(data)
    try:
        g = threading.Event()
        rt = b.add_runtime("wearable", ScriptedAdapter([("gate", g), *frames], clock="device", hold_open=True),
                           processing_config_for("sim"))
        assert wait_until(lambda: rt.connection == "connected")
        s = b.start_session("wearable", "other boot")
        g.set()
        assert wait_until(lambda: rt.counters["records"] == len(frames))
        b.stop_session(s["session_id"])

        assert rt.counters["calibration_restored"] == 1
        cal = rt.status()["calibration"]
        assert cal["state"] == "invalidated" and cal["reason"] in expected, cal
        assert cal["calibration_id"] is None
        feats = emg_features(session_dir(b.session(s["session_id"])) / "derived.ndjson")
        assert feats, "no EMG features recorded"
        assert all(f["features"]["emg_activation"]["normalized"]["value"] is None for f in feats)
        row = b.session(s["session_id"])
        assert row["calibration_id"] is None
        # the stored calibration itself is kept for the record, not deleted or altered
        assert [c["calibration_id"] for c in b.store.calibrations()] == [cal_id]
    finally:
        b.shutdown()


# ----------------------------------------------------------------------------- (4) graceful shutdown


def test_graceful_shutdown_closes_the_recording_session_as_interrupted(tmp_path):
    data = tmp_path / "data"
    frames = sim_frames(duration_s=5)
    a = Backend(data)
    g = threading.Event()
    rt = a.add_runtime("wearable", ScriptedAdapter([frames[0], ("gate", g), *frames[1:16]], clock="device",
                                                   hold_open=True), processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == 1)
    s = a.start_session("wearable", "stopped by shutdown")
    g.set()
    assert wait_until(lambda: rt.session is not None and rt.session.rec.lines == 15)
    a.shutdown()
    assert not rt.alive

    b = Backend(data)
    try:
        assert b.recovered == {"wearable_sessions_interrupted": 0, "station_sessions_interrupted": 0}
        row = b.session(s["session_id"])
        assert row["state"] == "interrupted" and row["end_reason"] == "runtime_stopped"
        assert row["counters"]["frames"] == 15 and row["source"] == "simulated"
        lines, tail = complete_lines(session_dir(row) / "telemetry.ndjson")
        assert tail == b"" and lines == [f + b"\n" if not f.endswith(b"\n") else f for f in frames[1:16]]
        import hashlib
        for name, meta in row["files"].items():
            assert hashlib.sha256(Path(meta["path"]).read_bytes()).hexdigest() == meta["sha256"], name
        manifest = json.loads((session_dir(row) / "manifest.json").read_text())
        assert manifest["session"]["state"] == "interrupted"
        rx = (session_dir(row) / "rx_times.csv").read_text().splitlines()
        assert len(rx) == 1 + 15
        # and it replays
        rep = b.runtime(b.start_replay(s["session_id"], fast=True)["runtime"])
        assert wait_until(lambda: not rep.alive, 15)
        assert rep.counters["records"] == 15 and rep.provenance["source"] == "replay"
    finally:
        b.shutdown()


def test_shutdown_while_device_unplugged_keeps_the_disconnect_visible(tmp_path):
    data = tmp_path / "data"
    frames = sim_frames(duration_s=5)
    a = Backend(data)
    g = threading.Event()
    rt = a.add_runtime("wearable", ScriptedAdapter([frames[0], ("gate", g), *frames[1:6], "disconnect"],
                                                   clock="device", auto_reconnect=False),
                       processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == 1)
    s = a.start_session("wearable", "unplugged")
    g.set()
    assert wait_until(lambda: rt.connection == "disconnected")
    assert rt.status()["state"] == "disconnected"
    a.shutdown()

    b = Backend(data)
    try:
        row = b.session(s["session_id"])
        assert row["state"] == "interrupted" and row["end_reason"] == "runtime_stopped"
        assert row["counters"]["connection_disconnected"] == 1
        assert row["counters"]["frames"] == 5
        lines, _ = complete_lines(session_dir(row) / "telemetry.ndjson")
        assert len(lines) == 5
    finally:
        b.shutdown()


def test_session_ended_by_end_of_input_is_completed_and_survives_restart(tmp_path, sim_config):
    data = tmp_path / "data"
    frames = sim_frames(duration_s=3)
    a = Backend(data)
    g = threading.Event()
    rt = a.add_runtime("wearable", ScriptedAdapter([frames[0], ("gate", g), *frames[1:]],
                                                   clock="device"), sim_config)
    assert wait_until(lambda: rt.counters["records"] == 1)
    s = a.start_session("wearable", "finite")
    g.set()
    row = wait_row(a, s["session_id"], "completed")
    assert wait_until(lambda: not rt.alive)
    a.shutdown()

    b = Backend(data)
    try:
        assert b.recovered["wearable_sessions_interrupted"] == 0
        assert b.session(s["session_id"]) == row
        assert row["end_reason"] == "end_of_input"
        assert row["counters"]["frames"] == len(frames) - 1
    finally:
        b.shutdown()


# ----------------------------------------------------------------------------- (5) offline


LOCAL_SOCKET_FILES = {"wifi.py", "fake_wifi.py", "bridge.py", "live_cli.py", "web_ui.py", "appdiscovery.py", "bonjour.py"}
NETWORK_MODULES = {"requests", "httpx", "urllib.request", "urllib3", "http.client", "aiohttp", "socket",
                   "websockets", "smtplib", "ftplib", "boto3", "botocore", "google.cloud", "firebase_admin",
                   "supabase", "openai", "anthropic", "zeroconf"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


@pytest.mark.parametrize("root", [BACKEND_SRC, SIGNALS_SRC], ids=["backend", "signals"])
def test_source_has_no_network_client_imports_or_remote_urls(root):
    files = sorted(root.rglob("*.py"))
    assert files
    for f in files:
        if f.name == "preflight.py":
            # The demo-day checklist talks to the local backend only: its default URL is loopback
            # (checked below) and it has no other address in it.
            assert "http://127.0.0.1" in f.read_text() and not re.search(
                r"https?://(?!127\.0\.0\.1|localhost)[a-z0-9]", f.read_text())
            continue
        bad = {m for m in _imports(f) if any(m == n or m.startswith(n + ".") for n in NETWORK_MODULES)}
        # bonjour.py announces this backend by multicast DNS on the local network only (no remote host)
        allowed = {"socket", "zeroconf", "zeroconf.IPVersion", "zeroconf.ServiceInfo", "zeroconf.Zeroconf"} \
            if f.name == "bonjour.py" else {"socket"}
        if f.name in LOCAL_SOCKET_FILES and bad <= allowed:
            # The Wi-Fi wearable's link (firmware/INTEGRATION_FOR_ABDULJABBAR.md): the laptop LISTENS for the wearable
            # and answers its discovery broadcast; appdiscovery.py answers the phone app's. These may use sockets, but
            # only loopback, any-interface or broadcast addresses appear in them: no remote host is ever contacted.
            text = f.read_text()
            hosts = set(re.findall(r"[\"'](\d{1,3}(?:\.\d{1,3}){3})[\"']", text))
            assert hosts <= {"127.0.0.1", "0.0.0.0", "255.255.255.255"}, f"{f} names hosts {sorted(hosts)}"
            continue
        assert not bad, f"{f} imports network clients: {sorted(bad)}"
        if f.name == "phoneqr.py":
            # builds this computer's own address (http://<its LAN address>:<port>/dashboard/) for a QR code that
            # phones read; it imports no network module and contacts nothing
            assert re.findall(r"https?://[^\s\"'<>)]+", f.read_text()) == ["http://{ranked[0]}:{int(port"]   # the regex stops at ")"
            continue
        for url in re.findall(r"https?://[^\s\"'<>)]+", f.read_text()):
            host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
            assert host in ("localhost", "127.0.0.1", "{args.host}"), f"{f} references remote URL {url}"


def test_cli_defaults_bind_to_loopback_and_local_data_dir():
    from haven_backend.cli import build_parser

    args = build_parser().parse_args(["serve"])
    assert args.host == "127.0.0.1" and args.device == "sim"
    assert not str(args.data_dir).startswith(("http:", "https:", "s3:", "gs:"))


def test_full_local_demo_runs_with_outbound_network_blocked(tmp_path, monkeypatch):
    """Record, restart, replay, seed history and serve the API with every non-loopback
    connection attempt blocked and logged."""
    from fastapi.testclient import TestClient
    from haven_backend.app import create_app
    from haven_backend.history import seed_history

    attempts: list = []
    real_connect, real_create = socket.socket.connect, socket.create_connection

    def is_local(addr) -> bool:
        if isinstance(addr, (str, bytes)):  # AF_UNIX
            return True
        host = str(addr[0])
        return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")

    def guarded_connect(self, addr):
        if not is_local(addr):
            attempts.append(addr)
            raise OSError("network blocked by test")
        return real_connect(self, addr)

    def guarded_create(addr, *a, **kw):
        if not is_local(addr):
            attempts.append(addr)
            raise OSError("network blocked by test")
        return real_create(addr, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create)

    data = tmp_path / "data"
    frames = sim_frames(duration_s=4)
    a = Backend(data)
    g = threading.Event()
    rt = a.add_runtime("wearable", ScriptedAdapter([frames[0], ("gate", g), *frames[1:]], clock="device",
                                                   hold_open=True), processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == 1)
    with TestClient(create_app(a), base_url="http://localhost") as client:
        sid = client.post("/api/sessions", json={"label": "offline", "request_id": "r1"}).json()["session_id"]
        g.set()
        assert wait_until(lambda: rt.counters["records"] == len(frames))
        assert client.post(f"/api/sessions/{sid}/stop", json={"request_id": "r2"}).json()["state"] == "completed"
        assert client.get("/api/health").json()["ok"] is True
    seed_history(a, days=1, duration_s=4)
    a.shutdown()

    b = Backend(data)
    try:
        with TestClient(create_app(b), base_url="http://localhost") as client:
            hist = client.get("/api/history").json()["items"]
            assert {i["source"] for i in hist} == {"simulated"}
            assert any(i.get("session_id") == sid for i in hist)
            rep = client.post(f"/api/sessions/{sid}/replay", json={"fast": True}).json()
            assert wait_until(lambda: not b.runtime(rep["runtime"]).alive, 15)
            assert b.runtime(rep["runtime"]).counters["records"] == len(frames) - 1
            assert client.get("/api/runtimes").status_code == 200
    finally:
        b.shutdown()
    assert attempts == [], f"outbound connections attempted: {attempts}"
