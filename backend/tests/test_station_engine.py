"""Station trial engine (PROPOSED interface): deliver -> wait -> felt/not_felt/missing. Mock controller only."""

from __future__ import annotations

import threading
import time

import pytest
from conftest import wait_until

from vibeguard_backend.runtime import RuntimeConflict
from vibeguard_backend.service import BadRequest
from vibeguard_backend.station import MockStationController, StationEngine, StationError

STIM = {"motor_setting": 120, "units": "pwm_duty_0_255"}


@pytest.fixture
def station(backend):
    ctl = MockStationController()
    eng = StationEngine(backend, ctl)
    sid = backend.start_station_session("live", "engine test")["station_session_id"]
    yield eng, ctl, sid, backend
    eng.shutdown()


def trials(backend, sid):
    return backend.store.station_trials(sid)


def test_felt_response_is_recorded_with_latency_and_motor_stops(station):
    eng, ctl, sid, b = station
    open_ = eng.present(sid, STIM, response_timeout_s=5)
    assert ctl.motor_on and open_["trial_index"] == 0 and eng.open_trial(sid)["stimulus"] == STIM
    time.sleep(0.05)
    t = eng.respond(sid, "felt")
    assert t["response"] == "felt" and t["response_latency_ms"] >= 40
    assert t["measured_amplitude"] is None, "no sensor, so no measured amplitude may be claimed"
    assert not ctl.motor_on and eng.open_trial(sid) is None
    assert trials(b, sid)[0]["stimulus"] == STIM


def test_timeout_is_recorded_as_missing_not_not_felt(station):
    eng, ctl, sid, b = station
    eng.present(sid, STIM, response_timeout_s=0.15)
    assert wait_until(lambda: len(trials(b, sid)) == 1, 3)
    t = trials(b, sid)[0]
    assert t["response"] == "missing" and t["responded_at"] is None and t["response_latency_ms"] is None
    assert "no response" in t["note"] and not ctl.motor_on
    with pytest.raises(RuntimeConflict):  # a late answer after the timeout is refused, not recorded
        eng.respond(sid, "felt")
    assert len(trials(b, sid)) == 1


def test_abort_stops_motor_closes_open_trial_as_missing_and_ends_aborted(station):
    eng, ctl, sid, b = station
    eng.present(sid, STIM, response_timeout_s=10)
    s = eng.abort(sid, "operator pressed abort")
    assert s["state"] == "aborted" and s["end_reason"] == "operator pressed abort"
    assert not ctl.motor_on and ctl.aborts >= 1
    t = trials(b, sid)
    assert len(t) == 1 and t[0]["response"] == "missing" and "aborted" in t[0]["note"]
    assert s["summary"]["stimulus_trials"]["missing"] == 1
    with pytest.raises(RuntimeConflict):
        eng.present(sid, STIM)  # session closed


def test_catch_trial_delivers_nothing_but_collects_the_response(station):
    eng, ctl, sid, b = station
    eng.present(sid, {"motor_setting": 0, "units": "pwm_duty_0_255"}, is_catch=True, response_timeout_s=5)
    assert ctl.delivered == [] and not ctl.motor_on
    eng.respond(sid, "not_felt")
    t = trials(b, sid)[0]
    assert t["is_catch"] is True and t["response"] == "not_felt"
    assert b.finish_station_session(sid, "completed")["summary"]["catch_trials"]["not_felt"] == 1


def test_one_open_trial_at_a_time_and_complete_waits_for_it(station):
    eng, _, sid, _ = station
    eng.present(sid, STIM, response_timeout_s=5)
    with pytest.raises(RuntimeConflict):
        eng.present(sid, STIM)
    with pytest.raises(RuntimeConflict):
        eng.complete(sid)
    eng.respond(sid, "not_felt")
    assert eng.complete(sid)["state"] == "completed"


def test_repeated_commands_do_not_duplicate_trials(station):
    eng, ctl, sid, b = station
    a = eng.present(sid, STIM, response_timeout_s=5, request_id="p1")
    again = eng.present(sid, STIM, response_timeout_s=5, request_id="p1")
    assert a["trial_id"] == again["trial_id"] and len(ctl.delivered) == 1
    r1 = eng.respond(sid, "felt", request_id="r1")
    r2 = eng.respond(sid, "felt", request_id="r1")
    assert r1["trial_id"] == r2["trial_id"] and len(trials(b, sid)) == 1
    s1 = eng.abort(sid, request_id="a1")
    s2 = eng.abort(sid, request_id="a1")
    assert s1["state"] == s2["state"] == "aborted"


def test_controller_refusal_or_failure_opens_no_trial(backend):
    sid = backend.start_station_session("live")["station_session_id"]
    eng = StationEngine(backend, MockStationController(reject_above=100))
    with pytest.raises(StationError, match="above the limit"):
        eng.present(sid, {"motor_setting": 200, "units": "pwm_duty_0_255"})
    assert eng.open_trial(sid) is None and trials(backend, sid) == []
    eng2 = StationEngine(backend, MockStationController(fail=True))
    with pytest.raises(StationError):
        eng2.present(sid, STIM)
    assert eng2.open_trial(sid) is None and trials(backend, sid) == []
    eng.present(sid, {"motor_setting": 50, "units": "pwm_duty_0_255"}, response_timeout_s=5)  # still usable
    eng.shutdown()


def test_validation_and_shutdown(backend):
    sid = backend.start_station_session("live")["station_session_id"]
    ctl = MockStationController()
    eng = StationEngine(backend, ctl)
    for bad in ({"response_timeout_s": 0}, {"response_timeout_s": 999}, {"response_timeout_s": "x"},
                {"is_catch": "false"}, {"stimulus": []}):
        kwargs = {"stimulus": STIM, **bad}
        with pytest.raises(BadRequest):
            eng.present(sid, **kwargs)
    with pytest.raises(BadRequest):
        eng.respond(sid, "timeout")  # a timeout is never a response; the engine records it as missing
    eng.present(sid, STIM, response_timeout_s=30)
    assert ctl.motor_on
    eng.shutdown()  # backend stopping: motor safe, open trial closed as missing
    assert not ctl.motor_on and eng.open_trial(sid) is None
    t = backend.store.station_trials(sid)
    assert len(t) == 1 and t[0]["response"] == "missing" and "backend stopped" in t[0]["note"]
