"""Adversarial review of the station engine (round 3). xfail(strict) = reproduced bug."""

from __future__ import annotations

import threading
import time

import pytest
from conftest import wait_until
from fastapi.testclient import TestClient

from vibeguard_backend.app import create_app
from vibeguard_backend.runtime import RuntimeConflict
from vibeguard_backend.service import BadRequest
from vibeguard_backend.station import MockStationController, StationEngine, StationError

STIM = {"motor_setting": 120, "units": "pwm_duty_0_255"}


def mk(backend, ctl=None):
    ctl = ctl or MockStationController()
    eng = StationEngine(backend, ctl)
    sid = backend.start_station_session("live", "review")["station_session_id"]
    return eng, ctl, sid


def trials(b, sid):
    return b.store.station_trials(sid)


# ---------------------------------------------------------------- verified working

def test_double_present_is_refused_and_one_timer(backend):
    eng, ctl, sid = mk(backend)
    eng.present(sid, STIM, response_timeout_s=5)
    with pytest.raises(RuntimeConflict):
        eng.present(sid, STIM)
    assert len(ctl.delivered) == 1
    eng.shutdown()


def test_idempotent_respond_records_once(backend):
    eng, ctl, sid = mk(backend)
    eng.present(sid, STIM, response_timeout_s=5)
    a = eng.respond(sid, "felt", request_id="r1")
    b = eng.respond(sid, "felt", request_id="r1")
    assert a == b and len(trials(backend, sid)) == 1


def test_request_id_reuse_across_commands_conflicts(backend):
    from vibeguard_backend.store import IdempotencyConflict
    eng, ctl, sid = mk(backend)
    eng.present(sid, STIM, response_timeout_s=5, request_id="same")
    with pytest.raises(IdempotencyConflict):
        eng.respond(sid, "felt", request_id="same")
    eng.shutdown()


def test_abort_after_abort_is_harmless_and_no_timer_leak(backend):
    eng, ctl, sid = mk(backend)
    eng.present(sid, STIM, response_timeout_s=0.2)
    eng.abort(sid)
    time.sleep(0.4)
    assert len(trials(backend, sid)) == 1 and trials(backend, sid)[0]["response"] == "missing"
    assert eng.abort(sid)["state"] == "aborted"


def test_complete_refused_while_trial_open_and_catch_trial_collects_answer(backend):
    eng, ctl, sid = mk(backend)
    eng.present(sid, STIM, is_catch=True, response_timeout_s=5)
    assert ctl.delivered == []
    with pytest.raises(RuntimeConflict):
        eng.complete(sid)
    t = eng.respond(sid, "not_felt")
    assert t["is_catch"] is True and t["response"] == "not_felt"


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), 31, True, "3", None])
def test_timeout_edge_values_rejected(backend, bad):
    eng, ctl, sid = mk(backend)
    with pytest.raises(BadRequest):
        eng.present(sid, STIM, response_timeout_s=bad)
    assert ctl.delivered == []


def test_controller_refusal_opens_no_trial(backend):
    eng, ctl, sid = mk(backend, MockStationController(reject_above=10))
    with pytest.raises(StationError):
        eng.present(sid, STIM)
    assert eng.open_trial(sid) is None


# ---------------------------------------------------------------- reproduced bugs

class SlowAbort(MockStationController):
    def abort(self):
        time.sleep(0.4)
        return super().abort()


def test_latency_excludes_motor_stop_time(backend):
    eng, ctl, sid = mk(backend, SlowAbort())
    eng.present(sid, STIM, response_timeout_s=5)
    time.sleep(0.05)
    t = eng.respond(sid, "felt")
    assert t["response_latency_ms"] < 300


class BlockingDeliver(MockStationController):
    """deliver() blocks (a slow serial write) until released, then turns the motor on."""

    def __init__(self):
        super().__init__()
        self.in_deliver = threading.Event()
        self.release = threading.Event()

    def deliver(self, stimulus, trial_id):
        self.in_deliver.set()
        self.release.wait(5)
        self.motor_on = True
        return {"accepted": True, "controller_time_us": None, "measured_amplitude": None, "error": None}


def test_abort_during_present_leaves_motor_off(backend):
    ctl = BlockingDeliver()
    eng, _, sid = mk(backend, ctl)
    th = threading.Thread(target=lambda: eng.present(sid, STIM, response_timeout_s=5))
    th.start()
    assert ctl.in_deliver.wait(3)
    ab = threading.Thread(target=lambda: eng.abort(sid))
    ab.start()
    time.sleep(0.2)
    ctl.release.set()
    th.join(); ab.join()
    assert not ctl.motor_on


class RaisesAfterStart(MockStationController):
    def deliver(self, stimulus, trial_id):
        self.motor_on = True
        raise OSError("serial timeout after command was written")


def test_deliver_exception_stops_motor(backend):
    ctl = RaisesAfterStart()
    eng, _, sid = mk(backend, ctl)
    with pytest.raises(StationError):
        eng.present(sid, STIM)
    assert not ctl.motor_on


def test_unstorable_stimulus_rejected_before_motor_starts(backend):
    eng, ctl, sid = mk(backend)
    deep = cur = {}
    for _ in range(20):
        cur["a"] = {}
        cur = cur["a"]
    try:
        eng.present(sid, {"motor_setting": 1, "x": deep}, response_timeout_s=5)
    except BadRequest:
        return
    assert not ctl.motor_on, "motor was driven with a stimulus that cannot be recorded"


def test_nan_stimulus_over_http_is_400_not_500(backend):
    with TestClient(create_app(backend), base_url="http://localhost", raise_server_exceptions=False) as c:
        sid = c.post("/api/station/sessions", json={}).json()["station_session_id"]
        r = c.post(f"/api/station/sessions/{sid}/stimulus", content='{"stimulus":{"motor_setting":NaN}}',
                   headers={"content-type": "application/json"})
        assert r.status_code == 400


class ReturnsNone(MockStationController):
    def deliver(self, stimulus, trial_id):
        return None


def test_malformed_controller_ack_is_station_error(backend):
    eng, _, sid = mk(backend, ReturnsNone())
    with pytest.raises(StationError):
        eng.present(sid, STIM)


def test_manual_trial_during_open_trial_does_not_break_respond(backend):
    eng, ctl, sid = mk(backend)
    eng.present(sid, STIM, response_timeout_s=5)
    backend.add_trial(sid, STIM, "felt")
    eng.respond(sid, "felt")


def test_abort_bad_reason_has_no_partial_effect(backend):
    eng, ctl, sid = mk(backend)
    eng.present(sid, STIM, response_timeout_s=5)
    with pytest.raises(BadRequest):
        eng.abort(sid, reason=123)  # type: ignore[arg-type]
    assert eng.open_trial(sid) is not None


def test_open_trial_not_persisted_across_crash(backend):
    """Documented behaviour (not xfail): an open trial lives only in memory. After a crash the session is
    marked interrupted but no row exists for the delivered stimulus, and nothing calls controller.abort()
    on startup (the engine is lazily created, so a motor left running is never stopped)."""
    eng, ctl, sid = mk(backend)
    eng.present(sid, STIM, response_timeout_s=30)
    assert trials(backend, sid) == []
    eng.shutdown()
    assert trials(backend, sid)[0]["response"] == "missing"


class SlowStop(MockStationController):
    """abort() blocks (a slow serial round trip) until released."""

    def __init__(self):
        super().__init__()
        self.stopping = threading.Event()
        self.release = threading.Event()

    def abort(self):
        self.stopping.set()
        self.release.wait(5)
        return super().abort()


def test_complete_cannot_slip_in_while_an_answer_is_being_recorded(backend):
    """respond() pops the trial, then the motor stops (slow), then the answer is stored. complete() in
    that window used to see no open trial, finish the session and make the answer fail with 409."""
    ctl = SlowStop()
    eng, _, sid = mk(backend, ctl)
    ctl.release.set()
    eng.present(sid, STIM, response_timeout_s=10)
    ctl.release.clear(); ctl.stopping.clear()
    out = {}
    th = threading.Thread(target=lambda: out.update(trial=eng.respond(sid, "felt")))
    th.start()
    assert ctl.stopping.wait(3)  # the answer is popped, the motor is stopping
    with pytest.raises(RuntimeConflict, match="still waiting"):
        eng.complete(sid)
    ctl.release.set()
    th.join(5)
    assert out["trial"]["response"] == "felt" and len(backend.store.station_trials(sid)) == 1
    assert eng.complete(sid)["state"] == "completed"


def test_timeout_that_cannot_be_recorded_is_reported_not_swallowed(backend):
    eng, ctl, sid = mk(backend)
    seen = []
    backend.hub.publish = lambda ch, rt, payload, _orig=backend.hub.publish: (seen.append(payload), _orig(ch, rt, payload))[1]
    eng.present(sid, STIM, response_timeout_s=0.2)
    backend.store.finish_station_session(sid, "completed", "closed behind the engine's back", None)
    assert wait_until(lambda: any(isinstance(p, dict) and p.get("event") == "station_controller_error" for p in seen), 3)
    assert not ctl.motor_on and eng.open_trial(sid) is None
