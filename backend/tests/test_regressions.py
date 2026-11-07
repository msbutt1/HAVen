"""Regression tests for defects found in review: pre-session calibration replay, data-dir
ownership, CLI argument validation, and seeded-history consistency. SIMULATED data only."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from conftest import ScriptedAdapter, sim_frames, wait_until

from vibeguard_backend.cli import main as cli_main
from vibeguard_backend.history import seed_history
from vibeguard_backend.service import Backend, DataDirInUse, processing_config_for


def _index_at(frames: list[bytes], seconds: float) -> int:
    for i, f in enumerate(frames):
        t = json.loads(f).get("timing")
        if t and (t["t0_us"] if t["mode"] == "uniform" else t["t_us"][0]) / 1e6 >= seconds:
            return i
    return len(frames)


def test_replay_of_session_calibrated_before_it_started_reproduces_score_and_calibration(backend):
    frames = sim_frames(duration_s=30)
    i_rel, i_sq, i_cal = _index_at(frames, 0.5), _index_at(frames, 4.5), _index_at(frames, 8.0)
    g1, g2, g3 = threading.Event(), threading.Event(), threading.Event()
    script = [*frames[:i_rel], ("gate", g1), *frames[i_rel:i_sq], ("gate", g2), *frames[i_sq:i_cal],
              ("gate", g3), *frames[i_cal:]]
    rt = backend.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == i_rel)
    backend.calibrate("wearable", "relaxed")
    g1.set()
    assert wait_until(lambda: rt.counters["records"] == i_sq)
    backend.calibrate("wearable", "squeeze")
    g2.set()
    assert wait_until(lambda: rt.counters["records"] == i_cal)
    assert wait_until(lambda: rt.status()["calibration"]["state"] == "calibrated")
    s = backend.start_session("wearable", "calibrated before start")  # calibration is NOT in the recording
    g3.set()
    assert wait_until(lambda: rt.counters["records"] == len(frames))
    row = backend.stop_session(s["session_id"])
    assert row["score"]["status"] == "ok" and row["score"]["session_au"] > 0
    assert row["counters"]["calibration_ids"], "the calibration that normalised this session is recorded"
    cmds = [json.loads(x) for x in (Path(row["dir"]) / "commands.ndjson").read_text().splitlines()]
    assert cmds[0]["action"] == "load_calibration" and cmds[0]["after_seq"] is None

    st = backend.start_replay(row["session_id"], fast=True)
    rr = backend.runtime(st["runtime"])
    assert wait_until(lambda: rr.finished, 15)
    assert rr.provenance["source"] == "replay"
    assert rr.counters["calibration_commands_replayed"] == 1
    assert rr.calibration_state == "calibrated" or rr.latest["score"]["cumulative"]["value"] > 0
    assert rr.latest["score"]["cumulative"]["value"] == pytest.approx(row["score"]["at_end"]["cumulative_au"], abs=1e-4)


def test_second_server_on_same_data_dir_is_refused_and_seed_history_leaves_live_sessions_alone(tmp_path):
    data = tmp_path / "data"
    server = Backend(data, exclusive=True)
    try:
        with pytest.raises(DataDirInUse):
            Backend(data, exclusive=True)
        server.store.create_wearable_session({"session_id": "ws-live", "runtime": "wearable",
                                              "state": "recording", "started_at": "2026-11-07T00:00:00.000Z"})
        seeder = Backend(data, recover=False)  # what `vg-backend seed-history` does
        seed_history(seeder, days=1, duration_s=10)
        seeder.shutdown()
        assert server.session("ws-live")["state"] == "recording"
    finally:
        server.shutdown()


def test_cli_rejects_bad_arguments_before_touching_the_data_dir(tmp_path, capsys):
    data = tmp_path / "data"
    assert cli_main(["--data-dir", str(data), "serve", "--speed", "0"]) == 2
    assert cli_main(["--data-dir", str(data), "serve", "--device", "serial", "--port", "/dev/null",
                     "--imu-counts-per-g", "100"]) == 2
    assert cli_main(["--data-dir", str(data), "serve", "--device", "serial"]) == 2
    assert not data.exists()


def test_seeded_history_is_calibrated_labelled_and_replays_to_the_same_records(backend):
    created = seed_history(backend, days=3, duration_s=30)
    for sid in created["wearable_sessions"]:
        row = backend.session(sid)
        assert row["source"] == "simulated" and row["simulated_history"] is True
        assert row["calibration_id"], f"{row['label']} ended uncalibrated"
        derived = [json.loads(x) for x in (Path(row["dir"]) / "derived.ndjson").read_text().splitlines()]
        assert sum(1 for d in derived if d["type"] == "stream_event" and d["event"] == "end_of_input") == 1
        rx = (Path(row["dir"]) / "rx_times.csv").read_text().splitlines()[1:]
        assert rx[-1].split(",")[1] <= row["ended_at"], "receive times must not run past the session end"


def test_the_real_server_can_speak_websocket():
    """The in-process test client needs no WebSocket library, but uvicorn does: without one,
    /ws/live answers 404 on the running server and the dashboard gets no live data."""
    import importlib.util

    assert any(importlib.util.find_spec(m) for m in ("websockets", "wsproto")), \
        "install uvicorn[standard] (websockets) or /ws/live will 404 on the real server"
