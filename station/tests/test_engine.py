"""Station engine tests: state machine, staircase end to end, timeouts, abort, save-once.

All inputs are simulated (scripted or synthetic responders on a virtual clock).
These verify the engine's bookkeeping and state rules, not the quality of any
perception measurement or any real hardware.
"""

import pytest

from haven_station import (AdapterError, ResultStore, ScriptedResponder, SimResponse,
                               SimulatedStationAdapter, SimulatedSubject, StationConfig, StationEngine,
                               StationStateError, VirtualClock, run_simulated)
from haven_station.adapters import AdapterEvent
from haven_station.staircase import Staircase
from haven_station.store import result_json

# start 50, step 10: these responses visit 50,40,30,40,30,40,30,40,30,40 -> 8 reversals, threshold 35
EIGHT = ["felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt"]
CFG = StationConfig.full(start_setting=50, step=10, catch_probability=0.0)


def go(script, cfg=CFG, **kw):
    return run_simulated(ScriptedResponder(script), cfg, session_id=kw.pop("session_id", "t1"), **kw)


def manual(responder=None, cfg=CFG, **kw):
    """Engine + adapter on a virtual clock that the test advances itself."""
    clock = VirtualClock()
    adapter = SimulatedStationAdapter(clock, responder or ScriptedResponder([None] * 50))
    engine = StationEngine(adapter, cfg, clock=clock, wall_clock=lambda: "2026-11-07T12:00:00+00:00",
                           session_id=kw.pop("session_id", "m1"), **kw)
    return engine, adapter, clock


def limits(result):
    return {x["code"] for x in result["quality"]["limitations"]}


# -- staircase end to end ---------------------------------------------------------

def test_synthetic_sequence_steps_reverses_and_terminates_with_threshold():
    engine, adapter = go(EIGHT)
    r = engine.result
    assert engine.state == "completed" and r["outcome"] == "threshold_estimated"
    delivered = [c["setting"] for c in adapter.commands if c["type"] == "deliver_stimulus"]
    assert delivered == [50, 40, 30, 40, 30, 40, 30, 40, 30, 40]
    assert r["reversals"]["levels"] == [30, 40, 30, 40, 30, 40, 30, 40]
    assert r["threshold"]["value"] == 35.0
    assert r["threshold"]["units"] == "motor_setting"
    assert r["threshold"]["reversal_levels_used"] == [30, 40, 30, 40, 30, 40]
    assert r["trial_counts"]["total"] == 10


def test_no_further_commands_after_the_stop_rule_fires():
    engine, adapter = go(EIGHT)
    n = len(adapter.commands)
    engine.tick()
    assert len(adapter.commands) == n and engine.state == "completed"


def test_timing_is_recorded_per_trial():
    engine, _ = go(EIGHT)
    t = engine.result["trials"]
    assert t[0]["command_t_s"] == 0.0 and t[0]["latency_ms"] == 600.0
    assert t[1]["command_t_s"] == pytest.approx(1.6)  # 0.6 response + 1.0 inter-trial delay
    assert t[0]["stimulus_started_t_s"] == 0.0


# -- timeouts are missing data ----------------------------------------------------

def test_timeout_is_recorded_as_missing_never_as_not_felt():
    engine, adapter = go([None] + EIGHT)
    first = engine.result["trials"][0]
    assert first["response_status"] == "missing" and first["response"] is None
    assert first["note"] == "no response within the window"
    assert engine.result["trial_counts"]["missing"] == 1
    assert "missing_responses" in limits(engine.result)
    assert not any(t["response"] == "not_felt" and t["response_status"] == "missing"
                   for t in engine.result["trials"])


def test_missing_response_repeats_the_same_level_and_does_not_move_the_staircase():
    engine, adapter = go([None] + EIGHT)
    delivered = [c["setting"] for c in adapter.commands if c["type"] == "deliver_stimulus"]
    assert delivered[:3] == [50, 50, 40]
    assert engine.result["threshold"]["value"] == 35.0  # same estimate as without the timeout
    assert engine.result["trials"][0]["staircase"] is None


def test_missing_response_waits_the_full_window_on_the_engine_clock():
    engine, _ = go([None] + EIGHT)
    t = engine.result["trials"]
    assert t[1]["command_t_s"] == pytest.approx(5.0)  # 4.0 s window + 1.0 s inter-trial delay


def test_three_missing_in_a_row_end_the_test_without_a_threshold():
    engine, _ = go([None, None, None])
    r = engine.result
    assert engine.state == "completed" and r["outcome"] == "too_many_missing_responses"
    assert r["threshold"] is None and r["trial_counts"]["missing"] == 3


def test_a_response_resets_the_consecutive_missing_counter():
    engine, _ = go([None, None, "felt", None, None, "not_felt"] + EIGHT)
    assert engine.result["outcome"] == "threshold_estimated"
    assert engine.result["trial_counts"]["missing"] == 4


def test_late_response_is_missing_and_never_credited_to_the_next_trial():
    late = SimResponse("felt", delay_s=5.0)  # window is 4 s
    engine, _ = go([late] + EIGHT)
    r = engine.result
    assert r["trials"][0]["response_status"] == "missing"
    assert r["trials"][1]["response_t_s"] != 5.0
    assert "late_responses_ignored" in limits(r)
    assert r["threshold"]["value"] == 35.0  # the late "felt" did not disturb the staircase


def test_unsolicited_and_stale_responses_are_ignored_and_counted():
    engine, adapter, clock = manual(ScriptedResponder(["felt"] * 5))
    engine.start()
    engine.tick()                                    # trial 1 waiting
    adapter._events.append(AdapterEvent("response", 0.1, response="not_felt", trial_id="some-other-trial"))
    clock.advance_to(0.2)
    engine.tick()                                    # stale trial id: ignored, trial 1 still waiting
    assert engine.snapshot()["phase"] == "awaiting_response"
    clock.advance_to(0.6)
    engine.tick()                                    # real response arrives; now between trials
    adapter.inject_response("felt")
    engine.tick()                                    # nothing waiting: unsolicited
    engine.abort()
    assert len(engine.trials) == 1 and engine.trials[0].response == "felt"
    assert {"late_responses_ignored", "unsolicited_responses_ignored"} <= limits(engine.result)


def test_ui_button_response_path_records_latency_and_source():
    engine, adapter, clock = manual()
    engine.start()
    engine.tick()
    clock.advance_to(1.25)
    engine.submit_response("felt", source="on_screen")
    t = engine.trials[0]
    assert t.response == "felt" and t.response_source == "on_screen" and t.latency_ms == 1250.0
    engine.abort()
    assert "non_button_response_source" in limits(engine.result)


# -- abort ------------------------------------------------------------------------

def test_abort_stops_the_motor_immediately_and_leaves_a_clear_state():
    engine, adapter, clock = manual(ScriptedResponder(["felt"] * 5))
    engine.start()
    engine.tick()
    clock.advance_to(0.2)
    assert adapter.motor_on is True                     # stimulus is running
    assert engine.abort("user_abort") is True
    assert adapter.motor_on is False
    assert adapter.commands[-1] == {"type": "stop_stimulus", "t_s": 0.2}
    r = engine.result
    assert engine.state == "aborted" and r["state"] == "aborted" and r["outcome"] == "aborted"
    assert r["abort_reason"] == "user_abort" and r["threshold"] is None
    assert r["trials"][-1]["response_status"] == "aborted" and r["trials"][-1]["response"] is None
    assert engine.snapshot()["phase"] == "done"


def test_nothing_happens_after_an_abort_even_when_time_passes():
    engine, adapter, clock = manual(ScriptedResponder(["felt"] * 5))
    engine.start()
    engine.tick()
    engine.abort()
    n = len(adapter.commands)
    clock.advance_to(60.0)
    engine.tick()
    engine.submit_response("felt")  # a button press racing the abort is ignored
    assert len(adapter.commands) == n and len(engine.trials) == 1 and engine.state == "aborted"


def test_abort_between_trials_has_no_in_flight_trial_to_record():
    engine, adapter, clock = manual(ScriptedResponder(["felt"] * 5))
    engine.start()
    engine.tick()
    clock.advance_to(0.6)
    engine.tick()                                        # trial 1 done, waiting for the next
    assert engine.abort() is True
    assert [t["response_status"] for t in engine.result["trials"]] == ["received"]


def test_abort_when_the_stop_cannot_be_confirmed_ends_in_error_not_aborted():
    engine, adapter, clock = manual(ScriptedResponder(["felt"] * 5))
    engine.start()
    engine.tick()
    adapter.fail_on_stop = True
    assert engine.abort() is True
    r = engine.result
    assert engine.state == "error" and r["outcome"] == "abort_stop_not_confirmed"
    assert "abort_stop_not_confirmed" in limits(r)


def test_abort_before_start_or_after_the_end_is_a_safe_no_op_that_still_sends_stop():
    engine, adapter, _ = manual()
    assert engine.abort() is False and engine.state == "ready"
    assert adapter.commands[-1]["type"] == "stop_stimulus"
    done, adapter2 = go(EIGHT)
    assert done.abort() is False and done.state == "completed"
    assert adapter2.commands[-1]["type"] == "stop_stimulus"


# -- adapter failures -------------------------------------------------------------

def test_stimulus_command_failure_ends_in_error_and_tries_to_stop():
    engine, adapter, _ = manual()
    adapter.fail_on_deliver = True
    engine.start()
    engine.tick()
    r = engine.result
    assert engine.state == "error" and r["outcome"] == "adapter_error"
    assert r["trials"][0]["response_status"] == "error"
    assert adapter.commands[-1]["type"] == "stop_stimulus"


def test_link_failure_mid_trial_ends_in_error_with_the_trial_marked_error():
    engine, adapter, _ = manual()
    engine.start()
    engine.tick()
    adapter.fail_on_poll = True
    engine.tick()
    assert engine.state == "error" and engine.trials[-1].response_status == "error"
    assert adapter.motor_on is False


def test_controller_fault_event_ends_in_error():
    engine, adapter, clock = manual()
    engine.start()
    engine.tick()
    adapter._events.append(AdapterEvent("fault", 0.0, detail="driver overcurrent"))
    engine.tick()
    assert engine.state == "error" and engine.result["error"] == "driver overcurrent"


# -- saved exactly once -----------------------------------------------------------

def test_result_is_handed_to_the_sink_exactly_once():
    saved = []
    engine, adapter = go(EIGHT, sink=saved.append)
    engine.tick()
    engine.abort()
    assert engine.retry_save() is False
    assert len(saved) == 1 and saved[0]["session_id"] == "t1" and engine.saved is True


def test_aborted_session_is_also_saved_once():
    saved = []
    engine, _ = go(EIGHT, sink=saved.append, abort_after_trials=3)
    engine.abort()
    engine.abort()
    assert len(saved) == 1 and saved[0]["state"] == "aborted"


def test_failed_save_is_visible_and_retry_saves_once():
    attempts = []

    def flaky(result):
        attempts.append(result["session_id"])
        if len(attempts) == 1:
            raise OSError("disk full")

    engine, _ = go(EIGHT, sink=flaky)
    assert engine.saved is False and engine.save_error == "OSError: disk full"
    assert any(e["type"] == "result_save_failed" for e in engine.event_log)
    assert engine.retry_save() is True and engine.saved and engine.save_error is None
    assert engine.retry_save() is False and len(attempts) == 2


def test_store_never_overwrites_a_saved_session(tmp_path):
    store = ResultStore(tmp_path)
    first, _ = go(EIGHT, sink=store, session_id="same")
    original = store.path_for("same").read_text(encoding="utf-8")
    other, _ = go(["not_felt"] * 5 + EIGHT, sink=store, session_id="same")
    assert other.already_saved is True and first.already_saved is False
    assert any(e["type"] == "result_already_saved" for e in other.event_log)
    assert store.path_for("same").read_text(encoding="utf-8") == original
    assert store.save(first.result) is False and store.list_sessions() == ["same"]


def test_store_round_trips_the_result(tmp_path):
    store = ResultStore(tmp_path)
    engine, _ = go(EIGHT, sink=store)
    assert store.load("t1") == engine.result


# -- catch trials -----------------------------------------------------------------

def _boundary_subject(idx, setting):
    return SimResponse("felt" if setting == 0 or setting > 35 else "not_felt", 0.5)


def test_catch_trials_send_no_stimulus_never_move_the_staircase_and_failures_are_reported():
    cfg = StationConfig.full(catch_probability=0.5)
    engine, adapter = run_simulated(_boundary_subject, cfg, seed=4, session_id="c1")
    r = engine.result
    catches = [t for t in r["trials"] if t["kind"] == "catch"]
    assert catches and all(t["stimulus_setting"] == 0 for t in catches)
    assert r["catch_trials"]["failed"] == len(catches)          # this subject always says "felt"
    assert "catch_trial_false_positives" in limits(r)
    # The staircase saw only stimulus trials: replaying just those responses reproduces it.
    s = Staircase(cfg)
    for t in r["trials"]:
        if t["kind"] == "stimulus" and t["response_status"] == "received":
            s.record(t["response"] == "felt")
    assert s.reversal_levels == r["reversals"]["levels"]
    # Catch trials are not first, not back to back, and delivered with setting 0.
    kinds = [t["kind"] for t in r["trials"]]
    assert kinds.index("catch") >= cfg.catch_min_stimulus_trials
    assert "catch" not in [a for a, b in zip(kinds, kinds[1:]) if a == b]
    assert [c["setting"] for c in adapter.commands if c["type"] == "deliver_stimulus"].count(0) == len(catches)


def test_catch_pass_and_missing_catch_are_distinguished():
    def resp(idx, setting):
        if setting == 0:
            return None if idx % 2 else SimResponse("not_felt", 0.5)
        return SimResponse("felt" if setting > 35 else "not_felt", 0.5)
    engine, _ = run_simulated(resp, StationConfig.full(catch_probability=0.5), seed=2, session_id="c2")
    c = engine.result["catch_trials"]
    assert c["trials"] == c["passed"] + c["failed"] + c["no_response"] and c["no_response"] + c["passed"] > 0


def test_no_catch_trials_is_stated_as_a_limitation():
    engine, _ = go(EIGHT)
    assert "no_catch_trials" in limits(engine.result)


# -- limits and outcomes ----------------------------------------------------------

def test_trial_limit_ends_the_test_without_a_threshold():
    cfg = StationConfig.full(start_setting=1, step=1, max_trials=9, catch_probability=0.0)
    engine, _ = run_simulated(lambda i, s: SimResponse("not_felt", 0.5), cfg, session_id="lim")
    assert engine.result["outcome"] == "max_trials_reached" and engine.result["threshold"] is None
    assert engine.result["trial_counts"]["total"] == 9


def test_always_felt_ends_at_the_floor_and_never_felt_at_the_ceiling():
    felt = StationConfig.full(start_setting=3, step=5, catch_probability=0.0)
    e1, _ = run_simulated(lambda i, s: SimResponse("felt", 0.5), felt, session_id="f1")
    assert e1.result["outcome"] == "floor_reached" and e1.result["threshold"] is None
    nope = StationConfig.full(start_setting=98, step=5, catch_probability=0.0)
    e2, _ = run_simulated(lambda i, s: SimResponse("not_felt", 0.5), nope, session_id="f2")
    assert e2.result["outcome"] == "ceiling_reached"
    assert "ceiling_reached" in limits(e2.result)


# -- labels, preview, provenance ----------------------------------------------------

def test_result_is_labelled_with_units_provenance_and_limitations():
    engine, _ = go(EIGHT, session_id="lab")
    r = engine.result
    assert r["stimulus_units"] == "motor_setting" and r["provenance"]["source"] == "simulated"
    assert all(t["stimulus_units"] == "motor_setting" and t["measured_amplitude"] is None for t in r["trials"])
    assert {"motor_setting_not_measured_amplitude", "contact_pressure_uncontrolled", "setting_range_assumed",
            "data_simulated"} <= limits(r)
    assert "Not a diagnostic test" in r["framing"] and r["is_preview"] is False


def test_preview_is_identified_as_a_preview_and_is_shorter():
    full, _ = run_simulated(SimulatedSubject(35, seed=1), seed=1, session_id="p0")
    prev, _ = run_simulated(SimulatedSubject(35, seed=1), StationConfig.preview(), seed=1, session_id="p1")
    r = prev.result
    assert r["is_preview"] is True and r["mode"] == "preview"
    assert r["result_label"] == "PREVIEW_short_run_not_equivalent_to_full_test"
    assert "preview_not_full_test" in limits(r)
    assert r["trial_counts"]["total"] < full.result["trial_counts"]["total"]
    assert r["threshold"]["method"] == "mean_of_last_2_reversal_levels"
    assert full.result["result_label"] != r["result_label"]


def test_the_simulated_adapter_can_never_claim_live():
    clock = VirtualClock()
    assert SimulatedStationAdapter(clock, ScriptedResponder([])).provenance["source"] == "simulated"


def test_engine_rejects_an_adapter_with_unknown_provenance():
    class Bad:
        provenance = {"source": "real_trust_me"}
    with pytest.raises(ValueError):
        StationEngine(Bad(), CFG)


# -- state machine misuse ---------------------------------------------------------

def test_commands_in_the_wrong_state_are_refused_or_ignored():
    engine, _, _ = manual()
    assert engine.tick() == [] and engine.state == "ready"
    engine.start()
    with pytest.raises(StationStateError):
        engine.start()
    with pytest.raises(ValueError):
        engine.submit_response("maybe")
    engine.abort()
    with pytest.raises(StationStateError):
        engine.start()


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "sl/ash"])
def test_invalid_session_ids_are_rejected(bad):
    with pytest.raises(ValueError):
        manual(session_id=bad)


def test_snapshot_reports_phase_for_screens():
    engine, adapter, clock = manual(ScriptedResponder(["felt"] * 5))
    assert engine.snapshot()["phase"] == "idle"
    engine.start()
    assert engine.snapshot()["phase"] == "between_trials"
    engine.tick()
    s = engine.snapshot()
    assert s["phase"] == "awaiting_response" and s["current_setting"] == 50 and s["stimulus_units"] == "motor_setting"
    clock.advance_to(0.6)
    engine.tick()
    assert engine.snapshot()["phase"] == "between_trials" and engine.snapshot()["trials_completed"] == 1


# -- events -----------------------------------------------------------------------

def test_event_stream_is_ordered_complete_and_labelled():
    engine, _ = go(EIGHT)
    log = engine.event_log
    assert [e["seq"] for e in log] == list(range(1, len(log) + 1))
    assert all(e["provenance"]["source"] == "simulated" and e["session_id"] == "t1" for e in log)
    assert sum(e["type"] == "reversal" for e in log) == 8
    assert sum(e["type"] == "session_result" for e in log) == 1
    assert sum(e["type"] == "trial_started" for e in log) == sum(e["type"] == "trial_completed" for e in log) == 10
    changes = [(e["previous"], e["current"]) for e in log if e["type"] == "state_changed"]
    assert changes == [("ready", "testing"), ("testing", "completed")]


def test_drain_events_returns_each_event_once():
    engine, adapter, clock = manual(ScriptedResponder(["felt"] * 5))
    engine.start()
    first = engine.tick()
    assert [e["type"] for e in first] == ["state_changed", "trial_started"]
    assert engine.tick() == []


# -- determinism and sanity ---------------------------------------------------------

def test_same_seed_gives_byte_identical_results_and_a_different_seed_does_not():
    def run(seed):
        e, _ = run_simulated(SimulatedSubject(35, miss_rate=0.1, false_positive_rate=0.2, seed=seed),
                             seed=seed, session_id="d")
        return result_json(e.result)
    assert run(5) == run(5) and run(5) != run(6)


def test_estimate_tracks_a_simulated_persons_threshold_across_seeds():
    estimates = []
    for seed in range(20):
        e, _ = run_simulated(SimulatedSubject(35, seed=seed), seed=seed, session_id=f"s{seed}")
        assert e.result["outcome"] == "threshold_estimated"
        estimates.append(e.result["threshold"]["value"])
    mean = sum(estimates) / len(estimates)
    assert abs(mean - 35) < 4 and all(abs(x - 35) < 15 for x in estimates)
