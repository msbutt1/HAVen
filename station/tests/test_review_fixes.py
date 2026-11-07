"""Regression tests for issues found by an independent adversarial review.

Each test reproduces a confirmed defect (simulated inputs only).
"""

import json

import pytest

from vibeguard_station import (ResultStore, ScriptedResponder, SimulatedStationAdapter, StationConfig,
                               StationEngine, VirtualClock, run_simulated)
from vibeguard_station.cli import main
from vibeguard_station.link import LinkStationAdapter, SimulatedController
from vibeguard_station.store import AlreadySavedError

EIGHT = ["felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt"]
CFG = StationConfig.full(start_setting=50, step=10, catch_probability=0.0)


def manual(script=None, cfg=CFG):
    clock = VirtualClock()
    adapter = SimulatedStationAdapter(clock, ScriptedResponder(script or ["felt"] * 20))
    engine = StationEngine(adapter, cfg, clock=clock, wall_clock=lambda: "2026-11-07T12:00:00+00:00",
                           session_id="r1")
    return engine, adapter, clock


def limits(result):
    return {x["code"] for x in result["quality"]["limitations"]}


# -- any adapter exception ends the session visibly ---------------------------------

def test_unexpected_exception_during_stop_still_ends_the_session_in_error():
    engine, adapter, _ = manual()
    engine.start()
    engine.tick()
    adapter.stop_stimulus = lambda: (_ for _ in ()).throw(RuntimeError("serial gone"))
    assert engine.abort() is True
    assert engine.state == "error" and engine.result["outcome"] == "abort_stop_not_confirmed"


def test_unexpected_exception_while_polling_ends_in_error_not_stuck_in_testing():
    engine, adapter, _ = manual()
    engine.start()
    engine.tick()
    adapter.poll = lambda: (_ for _ in ()).throw(KeyError("boom"))
    engine.tick()
    assert engine.state == "error" and "KeyError" in engine.result["error"]


def test_controller_garbage_field_types_are_counted_not_fatal():
    clock = VirtualClock()
    ctl = SimulatedController(clock, SimulatedStationAdapter(clock, ScriptedResponder(EIGHT)))
    link = LinkStationAdapter(ctl, clock, clock.advance)
    engine = StationEngine(link, CFG, clock=clock, session_id="r2")
    engine.start()
    engine.tick()
    ctl._out.append(json.dumps({"schema": "vibeguard.station_link", "version": 1, "type": "ack",
                                "command_id": [1], "status": "accepted"}) + "\n")
    engine.tick()
    assert engine.state == "testing" and link.malformed_lines == 1


# -- non-finite times ------------------------------------------------------------------

def test_nan_response_time_is_refused_and_the_result_stays_saveable():
    engine, _, _ = manual()
    engine.start()
    engine.tick()
    with pytest.raises(ValueError):
        engine.submit_response("felt", t=float("nan"))
    engine.abort()
    assert engine.result is not None


def test_cli_rejects_non_finite_parameters(tmp_path, capsys):
    assert main(["run", "--true-threshold", "nan", "--out", str(tmp_path)]) == 2
    assert not list(tmp_path.glob("*.json"))


def test_cli_reports_a_failed_save_instead_of_claiming_it_saved(tmp_path, capsys, monkeypatch):
    def boom(self, result):
        raise OSError("disk full")
    monkeypatch.setattr(ResultStore, "__call__", boom)
    assert main(["run", "--out", str(tmp_path)]) == 1
    cap = capsys.readouterr()
    assert "NOT SAVED" in cap.err and "saved     " not in cap.out


# -- store: collisions and partial writes ------------------------------------------------

def test_ids_differing_by_colon_case_or_underscore_never_share_a_file(tmp_path):
    store = ResultStore(tmp_path)
    ids = ["x:1", "x_1", "ABC", "abc"]
    for sid in ids:
        assert store.save({"session_id": sid, "n": sid}) is True
    assert {store.load(s)["n"] for s in ids} == set(ids)
    assert store.list_sessions() == sorted(ids)


def test_sink_raises_already_saved_and_leaves_the_first_file_alone(tmp_path):
    store = ResultStore(tmp_path)
    store({"session_id": "s", "v": 1})
    with pytest.raises(AlreadySavedError):
        store({"session_id": "s", "v": 2})
    assert store.load("s")["v"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_unserialisable_result_leaves_no_file_behind(tmp_path):
    store = ResultStore(tmp_path)
    with pytest.raises(ValueError):
        store.save({"session_id": "bad", "x": float("nan")})
    assert store.list_sessions() == [] and not list(tmp_path.iterdir())


def test_a_sink_returning_false_is_a_normal_save_not_a_silent_loss():
    engine, _ = run_simulated(ScriptedResponder(EIGHT), CFG, session_id="f", sink=lambda r: False)
    assert engine.saved and not engine.already_saved


# -- duplicates and the response window ---------------------------------------------------

def test_a_repeated_answer_for_an_answered_trial_is_a_duplicate_not_late():
    engine, adapter, clock = manual()
    engine.start()
    engine.tick()
    tid = engine.snapshot()["current_trial_id"]
    clock.advance_to(0.6)
    engine.tick()                               # trial answered by the script
    engine._on_response("felt", 0.7, "button", tid)
    engine.abort()
    assert "duplicate_responses_ignored" in limits(engine.result)
    assert "late_responses_ignored" not in limits(engine.result)


def test_the_response_window_is_half_open_whatever_order_calls_arrive_in():
    for order in ("submit_first", "tick_first"):
        engine, adapter, clock = manual([None] * 5)
        engine.start()
        engine.tick()
        clock.advance_to(4.0)                    # exactly the deadline
        if order == "submit_first":
            engine.submit_response("felt")
            engine.tick()
        else:
            engine.tick()
            engine.submit_response("felt")
        assert engine.trials[0].response_status == "missing", order


# -- the label cannot disagree with the protocol -------------------------------------------

def test_a_short_run_can_never_carry_the_full_test_label():
    with pytest.raises(TypeError):
        StationConfig.preview(mode="full")
    with pytest.raises(ValueError):
        StationConfig.full(reversals_to_stop=4, reversals_averaged=2).validate()
    with pytest.raises(ValueError):
        StationConfig.preview(reversals_to_stop=8, reversals_averaged=6, max_trials=30).validate()


def test_stimulus_longer_than_the_response_window_is_rejected():
    with pytest.raises(ValueError):
        StationConfig.full(stimulus_ms=5000, response_window_s=4.0).validate()


def test_a_stale_stimulus_end_does_not_stop_a_newer_trials_motor():
    clock = VirtualClock()
    adapter = SimulatedStationAdapter(clock, ScriptedResponder([None] * 5))
    adapter.deliver_stimulus("a", 50, 1000)
    clock.advance_to(0.5)
    adapter.deliver_stimulus("b", 40, 1000)      # newer trial starts before "a" would have ended
    clock.advance_to(1.2)
    adapter.poll()                               # delivers a's stimulus_ended
    assert adapter.motor_on is True
