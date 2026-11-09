"""Contract tests: example payloads match the code, validators accept real output and reject defects.

Examples are SIMULATED. They are regenerated from the engine and compared with the
committed files, so the documented payloads cannot drift from the implementation.
"""

import copy
import json
from pathlib import Path

import pytest

from haven_station import contract
from haven_station.adapters import SimulatedSubject
from haven_station.config import StationConfig
from haven_station.examples import build_examples, classify
from haven_station.simulation import run_simulated
from haven_station.store import result_json

EXAMPLES = Path(__file__).resolve().parents[1] / "contract" / "examples"
VALIDATORS = {"result": contract.validate_result, "event": contract.validate_event,
              "trial": contract.validate_trial, "command": contract.validate_command,
              "link": contract.validate_link_message}


@pytest.mark.parametrize("name", sorted(build_examples()))
def test_committed_example_matches_what_the_code_produces(name):
    built = build_examples()[name]
    assert (EXAMPLES / f"{name}.json").read_text(encoding="utf-8") == result_json(built)


@pytest.mark.parametrize("name", sorted(build_examples()))
def test_every_example_validates(name):
    kind = classify(name)
    if kind == "none":
        pytest.skip("not a schema'd record")
    assert VALIDATORS[kind](build_examples()[name]) == []


@pytest.mark.parametrize("mode,seed,kw", [
    ("full", 1, {}), ("full", 7, dict(miss_rate=0.2, false_positive_rate=0.3)),
    ("preview", 3, {}), ("preview", 4, dict(miss_rate=0.3)),
])
def test_generated_results_and_events_validate(mode, seed, kw):
    cfg = StationConfig.preview() if mode == "preview" else StationConfig.full()
    engine, _ = run_simulated(SimulatedSubject(35, seed=seed, **kw), cfg, seed=seed, session_id=f"v{seed}")
    assert contract.validate_result(engine.result) == []
    assert all(contract.validate_event(e) == [] for e in engine.event_log)
    assert all(contract.validate_trial(t) == [] for t in engine.result["trials"])


def test_aborted_and_error_results_validate():
    ab, _ = run_simulated(SimulatedSubject(35, seed=1), seed=1, session_id="ab", abort_after_trials=4)
    assert ab.result["state"] == "aborted" and contract.validate_result(ab.result) == []


def _good():
    e, _ = run_simulated(SimulatedSubject(35, seed=1), StationConfig.preview(), seed=1, session_id="g")
    return e.result


def test_validator_rejects_a_timeout_recorded_as_not_felt():
    r = _good()
    r["trials"][0]["response_status"] = "missing"
    r["trials"][0]["response"] = "not_felt"
    assert any("timeout" in p for p in contract.validate_result(r))


def test_validator_rejects_a_threshold_without_the_estimated_outcome():
    r = _good()
    r["outcome"] = "max_trials_reached"
    assert any("threshold" in p for p in contract.validate_result(r))


def test_validator_rejects_a_measured_amplitude():
    r = _good()
    r["trials"][0]["measured_amplitude"] = 1.2
    assert any("measured_amplitude" in p for p in contract.validate_result(r))


def test_validator_rejects_wrong_units_and_a_preview_posing_as_a_full_test():
    r = _good()
    r["stimulus_units"] = "m/s^2"
    r["result_label"] = "vibration_perception_staircase_estimate_prototype"
    probs = contract.validate_result(r)
    assert any("stimulus_units" in p for p in probs) and any("result_label" in p for p in probs)


def test_validator_requires_the_honesty_limitations():
    r = _good()
    r["quality"]["limitations"] = [x for x in r["quality"]["limitations"]
                                   if x["code"] not in ("preview_not_full_test", "data_simulated")]
    probs = contract.validate_result(r)
    assert any("preview_not_full_test" in p for p in probs) and any("data_simulated" in p for p in probs)


def test_validator_rejects_catch_trials_with_a_stimulus_and_inconsistent_counts():
    r = copy.deepcopy(_good())
    r["trials"][0]["kind"] = "catch"
    r["trial_counts"]["total"] += 1
    probs = contract.validate_result(r)
    assert any("catch trials use setting" in p for p in probs) and any("trial_counts.total" in p for p in probs)


@pytest.mark.parametrize("prov,ok", [
    ({"source": "live"}, True), ({"source": "simulated", "detail": {"seed": 1}}, True),
    ({"source": "replay", "recording_id": "rec-1", "original_source": "live"}, True),
    ({"source": "replay"}, False), ({"source": "replay", "recording_id": "r", "original_source": "replay"}, False),
    ({"source": "real"}, False), ("live", False),
])
def test_provenance_rules(prov, ok):
    assert (contract.validate_provenance(prov) == []) is ok


def test_command_and_link_validators_reject_malformed_messages():
    good = json.loads((EXAMPLES / "command_deliver_stimulus.json").read_text())
    assert contract.validate_command(good) == []
    for mutate in (lambda c: c.update(type="explode"), lambda c: c.update(setting=-1),
                   lambda c: c.update(duration_ms=0), lambda c: c.update(version=2),
                   lambda c: c.pop("trial_id")):
        bad = copy.deepcopy(good)
        mutate(bad)
        assert contract.validate_command(bad), mutate
    assert contract.validate_link_message({"schema": "vibeguard.station_link", "version": 1,
                                           "type": "response", "response": "maybe", "device_t_us": 5})
