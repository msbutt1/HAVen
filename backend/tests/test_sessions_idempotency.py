"""Session and command idempotency: Backend service and HTTP API.

Acceptance check under test: "Repeated commands do not accidentally duplicate
completed sessions." A retried click / resent request (same ``request_id``)
must return the first result and change nothing; different commands must be
judged on the real state (409 when not allowed); invalid input is 400; unknown
ids are 404; a request_id reused for something else must never silently act on
(or report) the wrong record.

Everything runs on SIMULATED frames through a ScriptedAdapter, offline.
"""

from __future__ import annotations

import threading
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from conftest import ScriptedAdapter, sim_frames, wait_until
from vibeguard_backend.app import create_app
from vibeguard_backend.runtime import RuntimeConflict
from vibeguard_backend.service import Backend, BadRequest, NotFound, processing_config_for
from vibeguard_backend.store import IdempotencyConflict

STIM = {"motor_setting": 120, "units": "pwm_duty_0_255"}


@lru_cache(maxsize=1)
def _frames() -> tuple[bytes, ...]:
    return tuple(sim_frames(duration_s=2.0, seed=3))


def _connected_runtime(backend: Backend, name: str = "wearable"):
    """A runtime that delivered a few simulated frames and then stays connected (silent)."""
    rt = backend.add_runtime(name, ScriptedAdapter(list(_frames()[:40]), hold_open=True),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.connection == "connected" and rt.counters["records"] >= 40)
    return rt


@pytest.fixture
def rt(backend):
    return _connected_runtime(backend)


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend), raise_server_exceptions=False) as c:
        yield c


def _rows(backend: Backend) -> list[dict]:
    return backend.store.wearable_sessions()


def _ids(rows: list[dict], key: str = "session_id") -> list[str]:
    return [r[key] for r in rows]


def _assert_no_duplicates(backend: Backend) -> None:
    ids = _ids(_rows(backend))
    assert len(ids) == len(set(ids)), ids
    hist = backend.history()["items"]
    keys = [(h["kind"], h.get("session_id") or h.get("station_session_id")) for h in hist]
    assert len(keys) == len(set(keys)), keys


# =============================================================== wearable sessions: service

def test_start_twice_with_same_request_id_returns_same_session_and_one_row(backend, rt):
    a = backend.start_session("wearable", "first", request_id="req-start-1")
    b = backend.start_session("wearable", "first (retried)", request_id="req-start-1")
    assert a == b
    assert a["state"] == "recording" and a["source"] == "simulated"
    rows = _rows(backend)
    assert _ids(rows) == [a["session_id"]]
    assert rows[0]["label"] == "first"  # the retry's different label changed nothing
    assert rt.status()["active_session_id"] == a["session_id"]
    _assert_no_duplicates(backend)


def test_concurrent_starts_with_same_request_id_create_one_session(backend, rt):
    n = 8
    barrier = threading.Barrier(n)
    results: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            out = backend.start_session("wearable", "race", request_id="req-race")
            with lock:
                results.append(out)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors, errors
    assert len(results) == n
    assert len({r["session_id"] for r in results}) == 1
    assert _ids(_rows(backend)) == [results[0]["session_id"]]
    _assert_no_duplicates(backend)


def test_different_request_id_while_recording_conflicts_and_failure_is_not_cached(backend, rt):
    first = backend.start_session("wearable", request_id="req-a")
    with pytest.raises(RuntimeConflict):
        backend.start_session("wearable", request_id="req-b")
    with pytest.raises(RuntimeConflict):
        backend.start_session("wearable")  # no request_id at all
    assert _ids(_rows(backend)) == [first["session_id"]]

    backend.stop_session(first["session_id"])
    # the rejected request was not remembered: the same id can now really start a session
    second = backend.start_session("wearable", request_id="req-b")
    assert second["session_id"] != first["session_id"] and second["state"] == "recording"
    assert sorted(_ids(_rows(backend))) == sorted([first["session_id"], second["session_id"]])
    _assert_no_duplicates(backend)


def test_stop_twice_returns_same_completed_row_without_changing_it(backend, rt):
    s = backend.start_session("wearable", request_id="req-s")
    sid = s["session_id"]
    first = backend.stop_session(sid, request_id="req-stop")
    assert first["state"] == "completed" and first["end_reason"] == "stopped_by_user"
    assert first["ended_at"]

    again_same_id = backend.stop_session(sid, request_id="req-stop")
    again_no_id = backend.stop_session(sid)
    again_new_id = backend.stop_session(sid, request_id="req-stop-2")
    for out in (again_same_id, again_no_id, again_new_id):
        assert out["session_id"] == sid
        assert out["state"] == "completed"
        assert out["ended_at"] == first["ended_at"]
        assert out["counters"] == first["counters"]

    stored = backend.session(sid)
    assert stored["ended_at"] == first["ended_at"] and stored["state"] == "completed"
    assert _ids(_rows(backend)) == [sid]
    assert rt.status()["active_session_id"] is None and rt.host_state == "ready"
    _assert_no_duplicates(backend)


def test_concurrent_stops_without_request_id_all_see_one_completion(backend, rt):
    sid = backend.start_session("wearable")["session_id"]
    n = 8
    barrier = threading.Barrier(n)
    results: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            out = backend.stop_session(sid)
            with lock:
                results.append(out)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors, errors
    assert {r["state"] for r in results} == {"completed"}
    assert len({r["ended_at"] for r in results}) == 1
    assert _ids(_rows(backend)) == [sid]


def test_stop_unknown_session_is_not_found_and_not_remembered(backend, rt):
    with pytest.raises(NotFound):
        backend.stop_session("ws-doesnotexist", request_id="req-unknown")
    # the 404 was not cached under the id: reusing it for a real stop works
    sid = backend.start_session("wearable")["session_id"]
    out = backend.stop_session(sid, request_id="req-unknown")
    assert out["session_id"] == sid and out["state"] == "completed"


def test_request_id_reused_for_a_different_command_kind_is_rejected_and_changes_nothing(backend, rt):
    s = backend.start_session("wearable", request_id="req-shared")
    with pytest.raises(IdempotencyConflict):
        backend.stop_session(s["session_id"], request_id="req-shared")
    assert backend.session(s["session_id"])["state"] == "recording"
    assert rt.status()["active_session_id"] == s["session_id"]
    with pytest.raises(IdempotencyConflict):
        backend.calibrate("wearable", "relaxed", request_id="req-shared")
    with pytest.raises(IdempotencyConflict):
        backend.start_station_session("simulated", request_id="req-shared")
    assert backend.store.station_sessions() == []


def test_start_while_disconnected_conflicts_then_same_request_id_works_after_reconnect(backend, sim_config):
    frames = list(_frames()[:5])
    adapter = ScriptedAdapter([*frames, "disconnect"], auto_reconnect=False, hold_open=True)
    rt = backend.add_runtime("wearable", adapter, sim_config)
    assert wait_until(lambda: rt.counters["records"] == 5 and rt.connection == "disconnected")
    assert rt.host_state == "disconnected"

    with pytest.raises(RuntimeConflict):
        backend.start_session("wearable", request_id="req-dc")
    assert _rows(backend) == []

    adapter.reconnect()
    assert wait_until(lambda: rt.connection == "connected", timeout=10)
    s = backend.start_session("wearable", request_id="req-dc")
    assert s["state"] == "recording"
    assert _ids(_rows(backend)) == [s["session_id"]]


@pytest.mark.parametrize("action", ["", "start", "RELAXED", None, "squeeze "])
def test_calibration_bad_action_is_bad_request_and_touches_nothing(backend, rt, action):
    before = rt.status()["calibration"]
    with pytest.raises(BadRequest):
        backend.calibrate("wearable", action, request_id="req-cal")
    assert rt.status()["calibration"] == before
    # the bad request did not consume the request_id
    out = backend.calibrate("wearable", "relaxed", request_id="req-cal")
    assert out["action"] == "relaxed"


def test_sessions_and_retries_survive_restart_without_duplicates(tmp_path):
    data = tmp_path / "data"
    b1 = Backend(data)
    try:
        _connected_runtime(b1)
        done = b1.start_session("wearable", "done", request_id="req-start-done")
        stopped = b1.stop_session(done["session_id"], request_id="req-stop-done")
        cut = b1.start_session("wearable", "cut off", request_id="req-start-cut")
    finally:
        b1.shutdown()

    b2 = Backend(data)
    try:
        assert b2.session(cut["session_id"])["state"] == "interrupted"
        # retried stop after restart: same completed row, unchanged
        again = b2.stop_session(done["session_id"], request_id="req-stop-done")
        assert again["state"] == "completed" and again["ended_at"] == stopped["ended_at"]
        # stopping the interrupted one changes nothing either
        cut_row = b2.stop_session(cut["session_id"])
        assert cut_row["state"] == "interrupted"
        _connected_runtime(b2)
        # retried starts after restart never create new sessions
        b2.start_session("wearable", request_id="req-start-done")
        b2.start_session("wearable", request_id="req-start-cut")
        assert sorted(_ids(_rows(b2))) == sorted([done["session_id"], cut["session_id"]])
        assert b2.runtime("wearable").status()["active_session_id"] is None
        _assert_no_duplicates(b2)
    finally:
        b2.shutdown()


# ------------------------------------------------- request_id reused for a different target

def test_stop_request_id_reused_for_another_session_never_returns_the_wrong_session(backend, rt):
    a = backend.start_session("wearable")["session_id"]
    backend.stop_session(a, request_id="req-stop-x")
    b = backend.start_session("wearable")["session_id"]
    try:
        out = backend.stop_session(b, request_id="req-stop-x")
    except (IdempotencyConflict, RuntimeConflict):
        out = None  # rejected loudly: acceptable
    if out is not None:
        assert out["session_id"] == b, f"asked to stop {b}, got the row of {out['session_id']}"
        assert backend.session(b)["state"] == "completed"


def test_trial_request_id_reused_in_another_station_session_never_returns_foreign_trial(backend):
    s1 = backend.start_station_session("simulated")["station_session_id"]
    s2 = backend.start_station_session("simulated")["station_session_id"]
    backend.add_trial(s1, STIM, "felt", request_id="req-trial-x")
    try:
        out = backend.add_trial(s2, STIM, "not_felt", request_id="req-trial-x")
    except (IdempotencyConflict, RuntimeConflict):
        out = None
    if out is not None:
        assert out["station_session_id"] == s2, "got a trial belonging to another station session"
        assert len(backend.store.station_trials(s2)) == 1


def test_complete_request_id_reused_for_another_station_session_never_returns_wrong_session(backend):
    s1 = backend.start_station_session("simulated")["station_session_id"]
    s2 = backend.start_station_session("simulated")["station_session_id"]
    backend.finish_station_session(s1, "completed", request_id="req-done-x")
    try:
        out = backend.finish_station_session(s2, "completed", request_id="req-done-x")
    except (IdempotencyConflict, RuntimeConflict):
        out = None
    if out is not None:
        assert out["station_session_id"] == s2
        assert backend.station_session(s2)["state"] == "completed"


# =============================================================== wearable sessions: HTTP

def test_http_start_twice_same_request_id_one_session(backend, rt, client):
    r1 = client.post("/api/sessions", json={"runtime": "wearable", "label": "h", "request_id": "h-1"})
    r2 = client.post("/api/sessions", json={"runtime": "wearable", "label": "h", "request_id": "h-1"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()
    listed = client.get("/api/sessions").json()
    assert _ids(listed) == [r1.json()["session_id"]]
    hist = client.get("/api/history").json()["items"]
    assert [h["session_id"] for h in hist if h["kind"] == "wearable"] == [r1.json()["session_id"]]


def test_http_concurrent_starts_same_request_id_one_session(backend, rt, client):
    n = 8
    barrier = threading.Barrier(n)
    codes: list[int] = []
    bodies: list[dict] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        r = client.post("/api/sessions", json={"request_id": "h-race"})
        with lock:
            codes.append(r.status_code)
            bodies.append(r.json())

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert codes == [201] * n, bodies
    assert len({b["session_id"] for b in bodies}) == 1
    assert len(client.get("/api/sessions").json()) == 1


def test_http_conflicts_not_found_and_kind_reuse(backend, rt, client):
    s = client.post("/api/sessions", json={"request_id": "h-a"}).json()
    r = client.post("/api/sessions", json={"request_id": "h-b"})
    assert r.status_code == 409 and "already recording" in r.json()["detail"]

    assert client.post("/api/sessions/ws-nope/stop", json={}).status_code == 404
    assert client.post("/api/sessions/ws-nope/stop").status_code == 404
    assert client.get("/api/sessions/ws-nope").status_code == 404

    r = client.post(f"/api/sessions/{s['session_id']}/stop", json={"request_id": "h-a"})
    assert r.status_code == 409 and "already used" in r.json()["detail"]
    assert client.get(f"/api/sessions/{s['session_id']}").json()["state"] == "recording"

    r = client.post("/api/sessions", json={"runtime": "nope", "request_id": "h-c"})
    assert r.status_code == 404
    assert len(client.get("/api/sessions").json()) == 1


def test_http_stop_twice_same_completed_row(backend, rt, client):
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    r1 = client.post(f"/api/sessions/{sid}/stop", json={"request_id": "h-stop"})
    r2 = client.post(f"/api/sessions/{sid}/stop", json={"request_id": "h-stop"})
    r3 = client.post(f"/api/sessions/{sid}/stop")
    assert [r.status_code for r in (r1, r2, r3)] == [200, 200, 200]
    first = r1.json()
    assert first["state"] == "completed"
    for r in (r2, r3):
        assert r.json()["session_id"] == sid
        assert r.json()["ended_at"] == first["ended_at"]
    listed = client.get("/api/sessions", params={"state": "completed"}).json()
    assert _ids(listed) == [sid]
    assert client.get("/api/sessions", params={"state": "recording"}).json() == []


def test_http_start_while_disconnected_is_409(backend, sim_config, client):
    adapter = ScriptedAdapter([*_frames()[:3], "disconnect"], auto_reconnect=False, hold_open=True)
    rt = backend.add_runtime("wearable", adapter, sim_config)
    assert wait_until(lambda: rt.connection == "disconnected" and rt.counters["records"] == 3)
    r = client.post("/api/sessions", json={"request_id": "h-dc"})
    assert r.status_code == 409 and "not connected" in r.json()["detail"]
    assert client.get("/api/runtimes/wearable").json()["state"] == "disconnected"
    assert client.get("/api/sessions").json() == []


def test_http_calibration_bad_action_is_400(backend, rt, client):
    for body in ({"action": "explode"}, {}, {"action": None}, {"action": 1}):
        r = client.post("/api/runtimes/wearable/calibration", json=body)
        assert r.status_code == 400, (body, r.text)
    assert client.post("/api/runtimes/nope/calibration", json={"action": "relaxed"}).status_code == 404
    ok = client.post("/api/runtimes/wearable/calibration", json={"action": "cancel", "request_id": "h-cal"})
    assert ok.status_code == 200 and ok.json()["action"] == "cancel"


# =============================================================== station sessions

def _station(client: TestClient, **body) -> str:
    r = client.post("/api/station/sessions", json={"source": "simulated", **body})
    assert r.status_code == 201, r.text
    return r.json()["station_session_id"]


def test_station_start_same_request_id_one_session(backend, client):
    r1 = client.post("/api/station/sessions", json={"source": "simulated", "request_id": "st-1"})
    r2 = client.post("/api/station/sessions", json={"source": "simulated", "request_id": "st-1"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["station_session_id"] == r2.json()["station_session_id"]
    assert len(client.get("/api/station/sessions").json()) == 1
    bad = client.post("/api/station/sessions", json={"source": "fake"})
    assert bad.status_code == 400
    _assert_no_duplicates(backend)


def test_station_duplicate_trial_request_id_records_one_trial(backend, client):
    sid = _station(client)
    body = {"stimulus": STIM, "response": "felt", "request_id": "tr-1"}
    r1 = client.post(f"/api/station/sessions/{sid}/trials", json=body)
    r2 = client.post(f"/api/station/sessions/{sid}/trials", json=body)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["trial_id"] == r2.json()["trial_id"]
    assert r1.json()["trial_index"] == 0
    # service level retry too
    t3 = backend.add_trial(sid, STIM, "felt", request_id="tr-1")
    assert t3["trial_id"] == r1.json()["trial_id"]
    trials = client.get(f"/api/station/sessions/{sid}").json()["trials"]
    assert len(trials) == 1
    assert client.get("/api/station/sessions").json()[0]["trial_count"] == 1
    # the next real trial gets the next index
    r4 = client.post(f"/api/station/sessions/{sid}/trials", json={"stimulus": STIM, "response": "not_felt"})
    assert r4.json()["trial_index"] == 1


def test_station_explicit_duplicate_trial_index_is_409(backend, client):
    sid = _station(client)
    url = f"/api/station/sessions/{sid}/trials"
    assert client.post(url, json={"stimulus": STIM, "response": "felt", "trial_index": 3}).status_code == 201
    r = client.post(url, json={"stimulus": STIM, "response": "not_felt", "trial_index": 3})
    assert r.status_code == 409 and "already recorded" in r.json()["detail"]
    trials = backend.station_session(sid)["trials"]
    assert [(t["trial_index"], t["response"]) for t in trials] == [(3, "felt")]
    # auto index continues after the highest index
    assert client.post(url, json={"stimulus": STIM, "response": "missing"}).json()["trial_index"] == 4


def test_station_duplicate_trial_index_given_as_string_is_409_not_500(backend, client):
    sid = _station(client)
    url = f"/api/station/sessions/{sid}/trials"
    assert client.post(url, json={"stimulus": STIM, "response": "felt", "trial_index": 0}).status_code == 201
    r = client.post(url, json={"stimulus": STIM, "response": "felt", "trial_index": "0"})
    assert r.status_code in (400, 409), (r.status_code, r.text)


@pytest.mark.parametrize("finish", ["complete", "abort"])
def test_station_trial_after_finish_is_409(backend, client, finish):
    sid = _station(client)
    url = f"/api/station/sessions/{sid}/trials"
    assert client.post(url, json={"stimulus": STIM, "response": "felt", "request_id": "pre"}).status_code == 201
    assert client.post(f"/api/station/sessions/{sid}/{finish}", json={}).status_code == 200
    r = client.post(url, json={"stimulus": STIM, "response": "felt"})
    assert r.status_code == 409 and "trials are closed" in r.json()["detail"]
    with pytest.raises(RuntimeConflict):
        backend.add_trial(sid, STIM, "felt", trial_index=7)
    # a retry of the trial that was accepted before the finish still returns it, unchanged
    retry = client.post(url, json={"stimulus": STIM, "response": "felt", "request_id": "pre"})
    assert retry.status_code == 201 and retry.json()["trial_index"] == 0
    assert len(backend.station_session(sid)["trials"]) == 1


def test_station_complete_twice_and_abort_after_complete_change_nothing(backend, client):
    sid = _station(client)
    url = f"/api/station/sessions/{sid}/trials"
    client.post(url, json={"stimulus": STIM, "response": "felt"})
    r1 = client.post(f"/api/station/sessions/{sid}/complete", json={"request_id": "c-1"})
    r2 = client.post(f"/api/station/sessions/{sid}/complete", json={"request_id": "c-1"})
    r3 = client.post(f"/api/station/sessions/{sid}/complete", json={})
    r4 = client.post(f"/api/station/sessions/{sid}/abort", json={"reason": "late"})
    assert [r.status_code for r in (r1, r2, r3, r4)] == [200, 200, 200, 200]
    first = r1.json()
    assert first["state"] == "completed" and first["ended_at"]
    for r in (r2, r3, r4):
        j = r.json()
        assert j["state"] == "completed"
        assert j["ended_at"] == first["ended_at"]
        assert j["summary"] == first["summary"]
        assert j["end_reason"] == first["end_reason"]
    # complete's request_id cannot be reused for abort
    r5 = client.post(f"/api/station/sessions/{sid}/abort", json={"request_id": "c-1"})
    assert r5.status_code == 409
    assert len(client.get("/api/station/sessions").json()) == 1
    assert client.post("/api/station/sessions/ss-nope/complete", json={}).status_code == 404


def test_station_missing_response_with_response_time_is_400(backend, client):
    sid = _station(client)
    url = f"/api/station/sessions/{sid}/trials"
    for extra in ({"responded_at": "2026-11-07T10:00:00.000Z"}, {"response_latency_ms": 420.0}):
        r = client.post(url, json={"stimulus": STIM, "response": "missing", **extra})
        assert r.status_code == 400, r.text
    with pytest.raises(BadRequest):
        backend.add_trial(sid, STIM, "missing", responded_at="2026-11-07T10:00:00.000Z")
    ok = client.post(url, json={"stimulus": STIM, "response": "missing"})
    assert ok.status_code == 201 and ok.json()["trial_index"] == 0
    assert len(backend.station_session(sid)["trials"]) == 1


@pytest.mark.parametrize("response", ["yes", "FELT", "", None, "timeout", 1])
def test_station_response_must_be_felt_not_felt_or_missing(backend, client, response):
    sid = _station(client)
    r = client.post(f"/api/station/sessions/{sid}/trials", json={"stimulus": STIM, "response": response})
    assert r.status_code == 400, r.text
    assert backend.station_session(sid)["trials"] == []


def test_station_trial_needs_stimulus_and_known_session(backend, client):
    sid = _station(client)
    url = f"/api/station/sessions/{sid}/trials"
    assert client.post(url, json={"response": "felt"}).status_code == 400
    assert client.post(url, json={"stimulus": {}, "response": "felt"}).status_code == 400
    r = client.post("/api/station/sessions/ss-nope/trials", json={"stimulus": STIM, "response": "felt"})
    assert r.status_code == 404


def test_station_summary_counts_catch_and_stimulus_trials_separately(backend, client):
    sid = _station(client)
    url = f"/api/station/sessions/{sid}/trials"
    plan = [("felt", False), ("felt", False), ("not_felt", False), ("missing", False),
            ("felt", True), ("not_felt", True), ("not_felt", True)]
    for i, (resp, catch) in enumerate(plan):
        r = client.post(url, json={"stimulus": STIM, "response": resp, "is_catch": catch,
                                   "request_id": f"sum-{i}"})
        assert r.status_code == 201
    # retries of every trial change nothing
    for i, (resp, catch) in enumerate(plan):
        client.post(url, json={"stimulus": STIM, "response": resp, "is_catch": catch,
                               "request_id": f"sum-{i}"})
    summary = client.post(f"/api/station/sessions/{sid}/complete", json={}).json()["summary"]
    assert summary["trials"] == 7
    assert summary["stimulus_trials"] == {"felt": 2, "not_felt": 1, "missing": 1}
    assert summary["catch_trials"] == {"felt": 1, "not_felt": 2, "missing": 0}
    _assert_no_duplicates(backend)


def test_station_is_catch_string_false_is_not_counted_as_catch(backend, client):
    sid = _station(client)
    r = client.post(f"/api/station/sessions/{sid}/trials",
                    json={"stimulus": STIM, "response": "felt", "is_catch": "false"})
    if r.status_code == 400:
        return  # rejected: acceptable
    assert r.status_code == 201
    assert r.json()["is_catch"] is False
