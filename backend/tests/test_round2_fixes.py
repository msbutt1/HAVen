"""Regression tests for the defects found in the second review round. SIMULATED data, offline."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from conftest import ScriptedAdapter, sim_frames, wait_until
from fastapi.testclient import TestClient

from vibeguard_backend.app import create_app
from vibeguard_backend.cli import main as cli_main
from vibeguard_backend.history import seed_history
from vibeguard_backend.runtime import COUNTED_EVENTS
from vibeguard_backend.service import Backend, BadRequest, processing_config_for
from vibeguard_backend.status_page import CSP, STATUS_PAGE

CFG = processing_config_for("sim")


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend), base_url="http://localhost") as c:
        yield c


def _rewrite_device(frames: list[bytes], device_id: str) -> list[bytes]:
    out = []
    for f in frames:
        r = json.loads(f)
        r["device_id"] = device_id
        r["boot_id"] = r["boot_id"].replace("sim-", "other-")
        out.append((json.dumps(r, separators=(",", ":")) + "\n").encode())
    return out


# -------------------------------------------------------------------------- device state

def test_connected_but_silent_device_is_not_presented_as_current(backend):
    frames = sim_frames(duration_s=10)
    rt = backend.add_runtime("wearable", ScriptedAdapter([*frames[:300], ("pause", 1.5), *frames[300:400]],
                                                         clock="host", hold_open=True), CFG)
    assert wait_until(lambda: rt.counters["records"] == 300, 10)
    assert wait_until(lambda: rt.silent, 3)
    st, snap = rt.status(), rt.snapshot()
    assert st["connection"] == "connected" and st["data_fresh"] is False
    assert snap["latest"]["score"]["status"] == "unavailable" and snap["latest"]["score"]["reason"] == "device_silent"
    assert all(snap["latest"][k]["status"] == "unavailable" for k in ("emg", "imu"))
    assert wait_until(lambda: rt.counters["records"] > 300 and not rt.silent, 5)  # data again: fresh again
    assert rt.counters["silent_episodes"] == 1


def test_a_different_device_on_the_same_link_is_switched_to_not_ignored_forever(backend):
    a = sim_frames(duration_s=5, run_id="a")
    b = _rewrite_device(sim_frames(duration_s=5, run_id="b"), "sim-wearable-02")
    gate = threading.Event()
    rt = backend.add_runtime("wearable", ScriptedAdapter([*a[:60], ("gate", gate), *b[:200]], clock="device",
                                                         hold_open=True), CFG)
    assert wait_until(lambda: rt.counters["records"] == 60)
    s = backend.start_session("wearable", "on device A")
    gate.set()
    assert wait_until(lambda: rt.device_id == "sim-wearable-02", 10), rt.status()
    st = rt.status()
    assert st["device_id"] == "sim-wearable-02" and rt.counters["device_changes"] == 1
    assert rt.events["device_changed"] == 1 and rt.session is None
    row = backend.session(s["session_id"])
    assert row["state"] == "interrupted" and row["end_reason"] == "device_changed"
    assert wait_until(lambda: rt.latest["emg"] is not None and rt.latest["emg"]["device_id"] == "sim-wearable-02", 10)


def test_error_state_runtime_does_not_present_old_values_as_current(backend):
    rt = backend.add_runtime("wearable", ScriptedAdapter(sim_frames(duration_s=3)[:80], clock="device",
                                                         hold_open=True), CFG)
    assert wait_until(lambda: rt.latest["emg"] is not None)
    rt.error = "RuntimeError: simulated crash"
    snap = rt.snapshot()
    assert snap["latest"]["emg"]["status"] == "unavailable" and snap["latest"]["emg"]["reason"] == "runtime_error"


def test_calibration_command_anchor_is_not_rewound_by_a_late_duplicate_frame(backend):
    frames = sim_frames(duration_s=5)
    late = frames[10]  # an old record arrives again, just before the calibrate command
    g1, g2 = threading.Event(), threading.Event()
    script = [*frames[:30], ("gate", g1), *frames[30:36], late, ("gate", g2), *frames[36:40]]
    rt = backend.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True), CFG)
    assert wait_until(lambda: rt.counters["records"] == 30)
    s = backend.start_session("wearable", "anchor")
    g1.set()
    assert wait_until(lambda: rt.counters["records"] == 37)  # 6 new frames and the duplicate
    backend.calibrate("wearable", "relaxed")
    g2.set()
    assert wait_until(lambda: rt.counters["records"] == 41)
    row = backend.stop_session(s["session_id"])
    cmd = json.loads((Path(row["dir"]) / "commands.ndjson").read_text().splitlines()[0])
    assert cmd["after_seq"] == json.loads(frames[35])["seq"], cmd  # the newest record, not the duplicate


def test_counted_events_include_the_data_quality_events():
    for ev in ("sample_overlap", "buffer_overflow", "channels_changed", "rate_changed", "score_accumulation_reset"):
        assert ev in COUNTED_EVENTS


# -------------------------------------------------------------------------- web safety

def test_status_page_escapes_everything_it_renders_and_has_a_csp(client):
    for expr in ("esc(s.label)", "esc(k)", "esc(x)", "esc(String(t).toUpperCase())"):
        assert expr in STATUS_PAGE, expr
    assert "'<td>'+(s.label||'')" not in STATUS_PAGE
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-security-policy"] == CSP
    assert "script-src 'unsafe-inline'" in CSP and "connect-src 'self'" in CSP and "frame-ancestors 'none'" in CSP


def test_hostile_web_pages_cannot_drive_the_local_api(client):
    # cross-site request (CSRF): a browser always sends the page's Origin
    r = client.post("/api/station/sessions", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/station/sessions", json={}, headers={"Origin": "http://localhost:5173"}).status_code == 201
    assert client.post("/api/station/sessions", json={}).status_code == 201  # curl/scripts: no Origin
    # DNS rebinding: the Host header names an attacker's domain
    assert client.get("/api/health", headers={"Host": "evil.example:8765"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    # body size
    big = client.post("/api/station/sessions", content=b"{" + b" " * 1_100_000 + b"}",
                      headers={"content-type": "application/json"})
    assert big.status_code == 413


def test_websocket_from_a_foreign_origin_is_refused(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/live", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    with client.websocket_connect("/ws/live", headers={"origin": "http://127.0.0.1:3000"}) as ws:
        assert ws.receive_json()["channel"] == "snapshot"


def test_replay_streams_only_go_to_clients_that_ask_for_them(backend, client):
    rt = backend.add_runtime("wearable", ScriptedAdapter(sim_frames(duration_s=3)[:60], clock="device",
                                                         hold_open=True), CFG)
    assert wait_until(lambda: rt.counters["records"] == 60)
    s = backend.start_session("wearable", "r"); row = backend.stop_session(s["session_id"])
    with client.websocket_connect("/ws/live?channels=derived,runtime") as ws:
        ws.receive_json()
        name = backend.start_replay(row["session_id"], fast=True)["runtime"]
        assert wait_until(lambda: backend.runtime(name).finished, 10)
        backend.hub.publish("runtime", "wearable", {"event": "marker"})
        seen = []
        while True:
            m = ws.receive_json()
            seen.append(m["runtime"])
            if m["payload"].get("event") == "marker":
                break
    assert name not in seen  # nothing from the replay reached the default client


# -------------------------------------------------------------------------- input hardening

@pytest.mark.parametrize("raw", [
    b'{"protocol": {"a": "\\ud800"}}',                    # lone surrogate would brick every later read
    b'{"protocol": {"k": Infinity}}',
    json.dumps({"protocol": {"deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}}}).encode(),
    b'{"protocol": {"big": "' + b"x" * 20_000 + b'"}}',
    b'{"label": {"not": "a string"}}',
])
def test_station_session_input_is_validated(client, raw):
    r = client.post("/api/station/sessions", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 400, r.text
    assert client.get("/api/history").status_code == 200  # still healthy afterwards


def test_trial_input_limits_and_huge_numbers_are_400_not_500(client):
    sid = client.post("/api/station/sessions", json={}).json()["station_session_id"]
    stim = {"motor_setting": 1, "units": "pwm"}
    for bad in ({"trial_index": 2 ** 63}, {"trial_index": 10 ** 30}, {"trial_index": 2_000_000},
                {"response_latency_ms": 10 ** 400}, {"stimulus": {"big": "x" * 20_000}}, {"is_catch": "false"}):
        r = client.post(f"/api/station/sessions/{sid}/trials", json={"stimulus": stim, "response": "felt", **bad})
        assert r.status_code == 400, (bad, r.status_code, r.text[:100])
    raw = b'{"response": "felt", "stimulus": {"\\ud800": 1}}'  # lone surrogate in a key
    assert client.post(f"/api/station/sessions/{sid}/trials", content=raw,
                       headers={"content-type": "application/json"}).status_code == 400
    assert client.post(f"/api/station/sessions/{sid}/trials", json={"stimulus": stim, "response": "felt",
                                                                    "trial_index": 1_000_000}).status_code == 201
    big = b'{"speed": 1' + b"0" * 400 + b'}'  # an integer too large for a float
    assert client.post("/api/sessions/none/replay", content=big,
                       headers={"content-type": "application/json"}).status_code == 400


def test_parallel_auto_indexed_trials_all_succeed_with_distinct_indices(backend):
    sid = backend.start_station_session("live")["station_session_id"]
    errors, out = [], []

    def post():
        try:
            out.append(backend.add_trial(sid, {"motor_setting": 1}, "felt")["trial_index"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=post) for _ in range(16)]
    [t.start() for t in ts]; [t.join(10) for t in ts]
    assert not errors and sorted(out) == list(range(16))


def test_no_trial_can_slip_in_after_the_session_is_finished(backend):
    sid = backend.start_station_session("live")["station_session_id"]
    stop = threading.Event()
    results = []

    def hammer():
        while not stop.is_set():
            try:
                backend.add_trial(sid, {"motor_setting": 1}, "felt")
            except Exception as exc:  # noqa: BLE001 - RuntimeConflict once closed
                results.append(type(exc).__name__); return
    t = threading.Thread(target=hammer); t.start()
    time.sleep(0.05)
    done = backend.finish_station_session(sid, "completed")
    stop.set(); t.join(5)
    counted = sum(done["summary"]["stimulus_trials"].values())
    assert counted == len(backend.store.station_trials(sid)), "a trial was stored after the summary was frozen"


def test_request_id_locks_are_released_and_old_ids_pruned(backend):
    for i in range(50):
        backend.start_station_session("live", request_id=f"id-{i}")
    assert backend._key_locks == {}
    db = backend.store._db
    db.execute("UPDATE idempotency SET created_at='2020-01-01T00:00:00.000Z' WHERE request_id < 'id-25'")
    pruned = backend.store.prune_idempotency(48 * 3600)
    assert pruned > 0 and backend.store._one("SELECT COUNT(*) AS n FROM idempotency")["n"] == 50 - pruned


# -------------------------------------------------------------------------- stored data and CLI

def test_seeding_twice_adds_nothing_and_saves_the_calibrations(backend):
    first = seed_history(backend, days=3, duration_s=30)
    again = seed_history(backend, days=3, duration_s=30)
    assert len(first["wearable_sessions"]) == 3 and again["wearable_sessions"] == [] and again["skipped"] == 3
    assert len(backend.store.wearable_sessions()) == 3
    ids = {c["calibration_id"] for c in backend.store.calibrations()}
    for sid in first["wearable_sessions"]:
        assert backend.session(sid)["calibration_id"] in ids


def test_relative_data_dir_is_stored_absolute_and_survives_a_different_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = Backend("rel-data")
    assert b.data_dir.is_absolute()
    s = seed_history(b, days=1, duration_s=10)["wearable_sessions"][0]
    b.shutdown()
    other = tmp_path / "elsewhere"; other.mkdir(); monkeypatch.chdir(other)
    b2 = Backend(tmp_path / "rel-data")
    try:
        row = b2.session(s)
        assert Path(row["dir"]).is_absolute() and (Path(row["dir"]) / "telemetry.ndjson").is_file()
        assert b2.start_replay(s, fast=True)["role"] == "replay"
    finally:
        b2.shutdown()


def test_stop_during_runtime_shutdown_returns_the_session_not_an_error(backend):
    rt = backend.add_runtime("replay-x", ScriptedAdapter(sim_frames(duration_s=3)[:60], clock="device",
                                                         hold_open=True), CFG, role="replay")
    assert wait_until(lambda: rt.counters["records"] == 60)
    s = backend.start_session("replay-x", "dying")
    rt.request_stop()
    row = backend.stop_session(s["session_id"])
    assert row["state"] in ("completed", "interrupted")


def test_data_dir_works_after_the_subcommand_and_before_it(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    assert cli_main(["seed-history", "--days", "1", "--data-dir", str(a)]) == 0
    assert cli_main(["--data-dir", str(b), "seed-history", "--days", "1"]) == 0
    assert (a / "vibeguard.sqlite3").is_file() and (b / "vibeguard.sqlite3").is_file()
    assert cli_main(["seed-history", "--days", "1", "--data-dir", str(a)]) == 0
    assert "already present, skipped" in capsys.readouterr().out


def test_cli_backup_commands_end_to_end(tmp_path, capsys):
    data = tmp_path / "data"
    b = Backend(data, recover=False)
    sid = seed_history(b, days=1, duration_s=10)["wearable_sessions"][0]
    b.shutdown()
    dest = tmp_path / "bk"
    assert cli_main(["--data-dir", str(data), "export-backup", sid, str(dest)]) == 0
    assert cli_main(["verify-backup", str(dest)]) == 0
    assert "SIMULATED recording" in capsys.readouterr().out
    (dest / "telemetry.ndjson").write_bytes((dest / "telemetry.ndjson").read_bytes()[:-9])
    assert cli_main(["verify-backup", str(dest)]) == 1
    assert cli_main(["--data-dir", str(data), "export-backup", sid, str(dest)]) == 1  # refuses to overwrite
    assert cli_main(["serve", "--backup", str(dest), "--data-dir", str(tmp_path / "d2")]) == 2  # damaged backup


# -------------------------------------------------------------------------- station engine over HTTP

def test_station_http_flow_stimulus_respond_timeout_abort(client, backend):
    sid = client.post("/api/station/sessions", json={"label": "http flow"}).json()["station_session_id"]
    base = f"/api/station/sessions/{sid}"
    stim = {"stimulus": {"motor_setting": 120, "units": "pwm_duty_0_255"}, "response_timeout_s": 5}
    r = client.post(base + "/stimulus", json={**stim, "request_id": "s1"})
    assert r.status_code == 201 and r.json()["trial_index"] == 0
    assert client.post(base + "/stimulus", json={**stim, "request_id": "s1"}).json()["trial_id"] == r.json()["trial_id"]
    assert client.post(base + "/stimulus", json=stim).status_code == 409           # one open trial at a time
    assert client.get(base + "/trial").json()["open_trial"]["stimulus"]["motor_setting"] == 120
    assert client.post(base + "/complete", json={}).status_code == 409             # a trial is still waiting
    assert client.post(base + "/respond", json={"response": "timeout"}).status_code == 400
    t = client.post(base + "/respond", json={"response": "felt"})
    assert t.status_code == 200 and t.json()["response"] == "felt" and t.json()["measured_amplitude"] is None
    assert client.post(base + "/respond", json={"response": "felt"}).status_code == 409   # nothing waiting
    client.post(base + "/stimulus", json={**stim, "response_timeout_s": 0.2})
    assert wait_until(lambda: client.get(base).json()["trials"][-1]["response"] == "missing", 3)
    client.post(base + "/stimulus", json=stim)
    ab = client.post(base + "/abort", json={"reason": "operator"})
    assert ab.status_code == 200 and ab.json()["state"] == "aborted"
    trials = client.get(base).json()["trials"]
    assert [t["response"] for t in trials] == ["felt", "missing", "missing"]
    assert client.post(base + "/stimulus", json=stim).status_code == 409           # session closed
    assert client.post("/api/station/sessions/nope/stimulus", json=stim).status_code == 404
