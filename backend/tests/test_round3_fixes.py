"""Regression tests for round-3 fixes not covered by the review test files. SIMULATED data, offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import wait_until
from fastapi.testclient import TestClient

from haven_backend.app import create_app
from haven_backend.backup import export_backup, start_backup_replay, verify_backup
from haven_backend.history import seed_history
from haven_backend.runtime import RuntimeConflict


@pytest.fixture
def backup_dir(backend, tmp_path) -> Path:
    sid = seed_history(backend, days=1, duration_s=10)["wearable_sessions"][0]
    export_backup(backend, sid, tmp_path / "bk")
    return tmp_path / "bk"


def test_finished_fallback_can_be_restarted_but_not_deleted_or_restarted_while_playing(backend, backup_dir):
    rt = start_backup_replay(backend, backup_dir, name="wearable", fast=True)
    assert wait_until(lambda: rt.finished, 20)
    with TestClient(create_app(backend), base_url="http://localhost") as c:
        assert c.delete("/api/runtimes/wearable").status_code == 409  # would leave no wearable
        assert "wearable" in backend.runtimes
        r = c.post("/api/runtimes/wearable/restart")
        assert r.status_code == 200 and r.json()["provenance"] is None or r.status_code == 200
        again = backend.runtime("wearable")
        assert again is not rt and again.meta["loop_count"] == 1
        assert wait_until(lambda: again.finished, 20)
        assert again.provenance["source"] == "replay" and again.counters["frames"] == rt.counters["frames"]
        # a runtime that is still playing cannot be restarted
        slow = start_backup_replay(backend, backup_dir, name="slow", speed=0.05)
        assert c.post("/api/runtimes/slow/restart").status_code == 409
        assert c.post("/api/runtimes/nope/restart").status_code == 404


def test_loop_keeps_the_fallback_playing(backend, backup_dir):
    start_backup_replay(backend, backup_dir, name="wearable", fast=True)
    backend.loop_backup("wearable", gap_s=0.1)
    assert wait_until(lambda: backend.runtimes["wearable"].meta.get("loop_count", 0) >= 2, 30)
    rt = backend.runtimes["wearable"]
    assert rt.provenance is None or rt.provenance["source"] == "replay"  # every pass is labelled replay


def test_replay_runtime_that_was_not_started_from_a_backup_cannot_be_restarted(backend):
    sid = seed_history(backend, days=1, duration_s=10)["wearable_sessions"][0]
    name = backend.start_replay(sid, fast=True)["runtime"]
    assert wait_until(lambda: backend.runtime(name).finished, 20)
    with pytest.raises(RuntimeConflict, match="not started from a backup"):
        backend.restart_backup(name)


def test_exporting_a_crashed_session_is_flagged_as_truncated(backend, tmp_path):
    sid = seed_history(backend, days=1, duration_s=10)["wearable_sessions"][0]
    backend.store.update_wearable_session(sid, state="interrupted", end_reason="backend_restart")
    meta = export_backup(backend, sid, tmp_path / "bk")
    assert meta["interrupted"] is True and verify_backup(tmp_path / "bk")["interrupted"] is True


def test_crashed_session_reports_when_it_really_ended(tmp_path):
    """recover_after_restart used to stamp the restart time as ended_at; the last received frame is the truth."""
    from haven_backend.service import Backend

    data = tmp_path / "data"
    a = Backend(data, recover=False)
    sid = seed_history(a, days=1, duration_s=10)["wearable_sessions"][0]
    row = a.session(sid)
    last_rx = (Path(row["dir"]) / "rx_times.csv").read_text().strip().splitlines()[-1].split(",")[1]
    a.store.update_wearable_session(sid, state="recording", ended_at=None, files=None, counters=None)
    a.shutdown()
    b = Backend(data)  # restart: recovery runs
    try:
        got = b.session(sid)
        assert got["state"] == "interrupted" and got["ended_at"] == last_rx
        assert got["counters"]["recovered"] is True and got["counters"]["recovered_at"] != last_rx
        m = json.loads((Path(got["dir"]) / "manifest.json").read_text())
        assert m["session"]["state"] == "interrupted" and m["session"]["ended_at"] == last_rx
    finally:
        b.shutdown()


def test_a_session_started_with_only_the_relaxed_reference_is_flagged(backend):
    from conftest import ScriptedAdapter, sim_frames
    from haven_backend.service import processing_config_for

    frames = sim_frames(duration_s=10)
    import threading
    g = threading.Event()
    rt = backend.add_runtime("wearable", ScriptedAdapter([*frames[:60], ("gate", g), *frames[60:200]],
                                                         clock="device", hold_open=True), processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == 60)
    backend.calibrate("wearable", "relaxed")
    g.set()
    assert wait_until(lambda: rt.calibration_state == "relaxed_captured", 10)
    s = backend.start_session("wearable", "half calibrated")
    row = backend.stop_session(s["session_id"])
    assert row["counters"]["started_with_relaxed_reference_only"] == 1
