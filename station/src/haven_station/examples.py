"""Deterministic example payloads for Ali (screens) and Abduljabbar (storage).

Generated from real engine runs on the simulated adapter, so they cannot drift
from the code: ``tests/test_contract.py`` rebuilds them and compares with the
committed files in ``contract/examples/``. All of it is SIMULATED.
"""

from __future__ import annotations

from .adapters import ScriptedResponder, SimulatedSubject, SimulatedStationAdapter, VirtualClock
from .config import StationConfig
from .link import COMMAND_SCHEMA, LINK_SCHEMA, VERSION
from .simulation import SIM_WALL_CLOCK, drive, run_simulated
from .engine import StationEngine


def build_examples() -> dict[str, object]:
    full, _ = run_simulated(SimulatedSubject(35, seed=1), seed=1, session_id="sim-full-001",
                            detail={"scenario": "example_full"})
    prev, _ = run_simulated(SimulatedSubject(35, seed=1), StationConfig.preview(), seed=1,
                            session_id="sim-preview-001", detail={"scenario": "example_preview"})
    aborted, _ = run_simulated(SimulatedSubject(35, seed=1), seed=1, session_id="sim-aborted-001",
                               abort_after_trials=5, detail={"scenario": "example_abort_after_5"})
    # A person who misses some windows and says "felt" on a catch trial.
    script = ["not_felt", None, "not_felt", "felt", "felt", "not_felt", "felt", "not_felt", "felt",
              "not_felt", "felt", "felt", "not_felt", "felt"]
    missing, _ = run_simulated(ScriptedResponder(script), StationConfig.preview(), seed=1,
                               session_id="sim-missing-001", detail={"scenario": "example_missing_response"})

    # A mid-session status snapshot, taken while waiting for a response.
    clock = VirtualClock()
    adapter = SimulatedStationAdapter(clock, SimulatedSubject(35, seed=1))
    eng = StationEngine(adapter, StationConfig.preview(), clock=clock,
                        wall_clock=lambda: SIM_WALL_CLOCK, session_id="sim-snapshot-001")
    eng.start()
    eng.tick()
    snapshot = eng.snapshot()

    log = prev.event_log
    pick = lambda t: next(e for e in log if e["type"] == t)  # noqa: E731
    link = lambda **kw: {"schema": LINK_SCHEMA, "version": VERSION, **kw}  # noqa: E731
    return {
        "result_full_simulated": full.result,
        "result_preview_simulated": prev.result,
        "result_aborted_simulated": aborted.result,
        "result_preview_with_missing_response": missing.result,
        "trial_example": prev.result["trials"][0],
        "event_trial_started": pick("trial_started"),
        "event_trial_completed": pick("trial_completed"),
        "event_reversal": pick("reversal"),
        "event_state_changed": pick("state_changed"),
        "status_snapshot": snapshot,
        "command_hello": {"schema": COMMAND_SCHEMA, "version": VERSION, "command_id": "c00000",
                          "type": "hello"},
        "link_hello": link(type="hello", command_id="c00000",
                           firmware={"name": "vibeguard-station-ref", "version": "0.1"},
                           setting_range={"min": 1, "max": 100}),
        "command_deliver_stimulus": {"schema": COMMAND_SCHEMA, "version": VERSION, "command_id": "c00001",
                                     "type": "deliver_stimulus", "trial_id": "sim-preview-001-t001",
                                     "setting": 60, "duration_ms": 1000},
        "command_deliver_catch": {"schema": COMMAND_SCHEMA, "version": VERSION, "command_id": "c00002",
                                  "type": "deliver_stimulus", "trial_id": "sim-preview-001-t002",
                                  "setting": 0, "duration_ms": 1000},
        "command_stop_stimulus": {"schema": COMMAND_SCHEMA, "version": VERSION, "command_id": "c00003",
                                  "type": "stop_stimulus"},
        "link_ack_accepted": link(type="ack", command_id="c00001", status="accepted"),
        "link_ack_stop_completed": link(type="ack", command_id="c00003", status="completed"),
        "link_ack_rejected": link(type="ack", command_id="c00001", status="rejected", reason="setting out of range"),
        "link_stimulus_started": link(type="stimulus_started", trial_id="sim-preview-001-t001", device_t_us=1200000),
        "link_response_felt": link(type="response", response="felt", trial_id="sim-preview-001-t001",
                                   device_t_us=1800000),
        "link_error": link(type="error", message="driver fault"),
    }


def classify(name: str) -> str:
    """Which validator applies to an example."""
    if name.startswith("result_"):
        return "result"
    if name.startswith("event_"):
        return "event"
    if name.startswith("trial_"):
        return "trial"
    if name.startswith("command_"):
        return "command"
    if name.startswith("link_"):
        return "link"
    return "none"
