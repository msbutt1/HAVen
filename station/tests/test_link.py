"""Controller-link tests: the real adapter path against a SIMULATED controller.

Nothing here has touched hardware or Salaar's firmware. It proves that the engine
works through the line protocol exactly as it does through the direct simulated
adapter, and that link failures end the session visibly.
"""

import json

from haven_station import (ScriptedResponder, SimulatedStationAdapter, StationConfig, StationEngine,
                               VirtualClock, drive, run_simulated)
from haven_station.contract import validate_command
from haven_station.link import LinkStationAdapter, SimulatedController

EIGHT = ["felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt"]
CFG = StationConfig.full(start_setting=50, step=10, catch_probability=0.0)


def rig(script=EIGHT, cfg=CFG, **adapter_kw):
    clock = VirtualClock()
    controller = SimulatedController(clock, SimulatedStationAdapter(clock, ScriptedResponder(script)))
    link = LinkStationAdapter(controller, clock, clock.advance, **adapter_kw)
    engine = StationEngine(link, cfg, clock=clock, wall_clock=lambda: "2026-11-07T12:00:00+00:00",
                           session_id="link1")
    return engine, link, controller, clock


def test_full_session_over_the_link_matches_the_direct_simulated_adapter():
    engine, link, controller, clock = rig()
    drive(engine, controller, clock)
    direct, _ = run_simulated(ScriptedResponder(EIGHT), CFG, session_id="link1")
    a, b = engine.result, direct.result
    assert a["state"] == "completed" and a["threshold"]["value"] == b["threshold"]["value"] == 35.0
    assert a["reversals"]["levels"] == b["reversals"]["levels"]
    assert [t["stimulus_setting"] for t in a["trials"]] == [t["stimulus_setting"] for t in b["trials"]]


def test_commands_on_the_wire_are_valid_and_carry_the_motor_setting():
    engine, link, controller, clock = rig()
    drive(engine, controller, clock)
    cmds = controller.received
    assert all(validate_command(c) == [] for c in cmds)
    assert [c["setting"] for c in cmds if c["type"] == "deliver_stimulus"][:3] == [50, 40, 30]
    assert len({c["command_id"] for c in cmds}) == len(cmds)  # unique ids


def test_results_over_a_simulated_controller_are_never_labelled_live():
    engine, link, controller, clock = rig()
    drive(engine, controller, clock)
    assert link.provenance["source"] == "simulated" and engine.result["provenance"]["source"] == "simulated"


def test_only_a_transport_that_says_it_is_real_hardware_is_labelled_live():
    class Wire:
        def write_line(self, line): pass
        def read_lines(self): return []

    class Real(Wire):
        is_simulated = False
    # Fails closed: a transport that says nothing is NOT live.
    assert LinkStationAdapter(Wire(), lambda: 0.0, lambda s: None).provenance["source"] == "simulated"
    assert LinkStationAdapter(Real(), lambda: 0.0, lambda s: None).provenance["source"] == "live"


def test_abort_over_the_link_sends_stop_and_waits_for_the_motor_off_ack():
    engine, link, controller, clock = rig()
    engine.start()
    engine.tick()
    clock.advance_to(0.2)
    assert controller.sim.motor_on is True
    assert engine.abort() is True
    assert controller.received[-1]["type"] == "stop_stimulus"
    assert controller.sim.motor_on is False and engine.state == "aborted"


def test_unacknowledged_stop_ends_the_session_in_error():
    engine, link, controller, clock = rig(stop_ack_timeout_s=0.3)
    engine.start()
    engine.tick()
    controller.silent = True
    assert engine.abort() is True
    assert engine.state == "error" and engine.result["outcome"] == "abort_stop_not_confirmed"


def test_rejected_stimulus_command_ends_the_session_in_error():
    engine, link, controller, clock = rig()
    controller.reject_commands = True
    engine.start()
    engine.tick()
    clock.advance_to(0.1)
    engine.tick()
    assert engine.state == "error" and "rejected" in engine.result["error"]


def test_dead_controller_is_detected_by_the_ack_timeout():
    engine, link, controller, clock = rig(ack_timeout_s=1.0)
    controller.silent = True
    engine.start()
    engine.tick()
    clock.advance_to(1.5)
    engine.tick()
    assert engine.state == "error" and "not acknowledged" in engine.result["error"]


def test_garbage_on_the_link_is_counted_and_never_used():
    engine, link, controller, clock = rig()
    controller._out += ["not json\n", json.dumps({"schema": "other", "version": 1, "type": "response"}) + "\n",
                        json.dumps([1, 2]) + "\n"]
    engine.start()
    engine.tick()
    assert link.malformed_lines == 3 and engine.state == "testing" and not engine.trials


def test_controller_error_message_ends_the_session():
    engine, link, controller, clock = rig()
    engine.start()
    engine.tick()
    controller._out.append(json.dumps({"schema": "vibeguard.station_link", "version": 1,
                                       "type": "error", "message": "driver fault"}) + "\n")
    engine.tick()
    assert engine.state == "error" and "driver fault" in engine.result["error"]
