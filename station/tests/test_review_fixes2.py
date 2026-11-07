"""Regression tests for issues found by the second independent review (hardware path).

Python-side cases use a fake transport; controller-protocol cases run against the compiled C++
core (skipped if there is no C++ compiler). Simulated inputs only.
"""

import json

import pytest

from vibeguard_station import AdapterError, StationConfig, StationEngine, VirtualClock
from vibeguard_station.link import LinkStationAdapter

from test_firmware_core import CXX, Core, CoreTransport, cmd, core, deliver, exe  # noqa: F401  (fixtures)

LINK = {"schema": "vibeguard.station_link", "version": 1}
HELLO = {**LINK, "type": "hello", "firmware": {"name": "fw", "version": "1"}, "setting_range": {"min": 1, "max": 100}}


class Tx:
    is_simulated = True

    def __init__(self, inbox=()):
        self.inbox, self.sent = list(inbox), []

    def write_line(self, line):
        self.sent.append(line)

    def read_lines(self):
        out, self.inbox = self.inbox, []
        return out


def link_with(*inbox):
    clock = VirtualClock()
    tx = Tx(inbox)
    return clock, tx, LinkStationAdapter(tx, clock, clock.advance)


# -- Python side -------------------------------------------------------------------------------------

def test_garbage_that_blows_the_json_parser_is_counted_not_fatal():
    clock, tx, link = link_with("[" * 100_000, "x" * 5000, json.dumps(HELLO))
    assert link.request_hello()["setting_range"]["max"] == 100
    assert link.malformed_lines == 2 and link.poll() == []


@pytest.mark.parametrize("ack", [{"command_id": "c00001"}, {"command_id": "c00001", "status": "banana"}])
def test_an_ack_without_a_usable_status_acknowledges_nothing(ack):
    clock, tx, link = link_with()
    link.deliver_stimulus("t1", 50, 500)
    tx.inbox = [json.dumps({**LINK, "type": "ack", **ack})]
    assert link.poll() == [] and link.malformed_lines == 1
    clock.advance(2.0)
    assert "not acknowledged" in link.poll()[0].detail


def test_events_that_arrive_in_the_same_read_as_the_hello_are_kept():
    press = {**LINK, "type": "response", "response": "felt", "device_t_us": 5}
    clock, tx, link = link_with(json.dumps(HELLO), json.dumps(press))
    link.request_hello()
    events = link.poll()
    assert [e.kind for e in events] == ["response"] and events[0].response == "felt"


def test_session_ids_must_leave_room_for_the_trial_suffix():
    class A:
        provenance = {"source": "simulated"}
    StationEngine(A(), StationConfig.full(), session_id="x" * 58)
    with pytest.raises(ValueError):
        StationEngine(A(), StationConfig.full(), session_id="x" * 59)


# -- controller core -----------------------------------------------------------------------------------

pytestmark_core = pytest.mark.skipif(CXX is None, reason="no C++ compiler on PATH")


@pytestmark_core
def test_longest_legal_session_id_works_end_to_end_through_the_core(core):
    clock = VirtualClock()
    link = LinkStationAdapter(CoreTransport(core, clock, ["felt", "felt", "not_felt", "felt", "not_felt", "felt",
                                                          "not_felt", "felt", "not_felt", "felt"]),
                              clock, clock.advance)
    link.request_hello()
    engine = StationEngine(link, StationConfig.full(start_setting=50, step=10, catch_probability=0.0),
                           clock=clock, session_id="s" * 58)
    engine.start()
    for _ in range(20_000):
        clock.advance(0.05)
        engine.tick()
        if engine.state != "testing":
            break
    assert engine.state == "completed" and len(engine.trials[0].trial_id) <= 64


@pytestmark_core
@pytest.mark.parametrize("line", [
    "xx" + deliver(),                                       # junk before
    deliver() + "GARBAGE",                                  # junk after
    deliver().replace('"setting": 50', '"setting": 50 junk'),   # junk after a number
    deliver().replace('"duration_ms": 1000', '"duration_ms": 1000, "setting": 99'),  # duplicate key
    '{"x": ' + deliver() + '}',                             # nested
])
def test_malformed_or_ambiguous_commands_never_start_the_motor(core, line):
    out = core.talk(line)  # either ignored, or answered with an explicit rejection: never accepted
    assert not any('"accepted"' in x or "stimulus_started" in x for x in out)
    assert core.motor() == 0


@pytestmark_core
@pytest.mark.parametrize("stop", [
    json.dumps({"schema": "vibeguard.station_command", "version": 1, "type": "stop_stimulus"}),      # no id
    json.dumps({"schema": "vibeguard.station_command", "version": 1, "command_id": "bad id!", "type": "stop_stimulus"}),
    json.dumps({"schema": "wrong", "version": 9, "command_id": "s1", "type": "stop_stimulus"}),
])
def test_a_stop_turns_the_motor_off_even_when_the_rest_of_the_line_is_damaged(core, stop):
    core.msgs(deliver(setting=70, ms=5000))
    assert core.motor() == 70
    core.talk(stop)
    assert core.motor() == 0


@pytestmark_core
def test_a_command_just_after_a_stimulus_ends_is_not_answered_busy(core):
    core.talk("#t 0")
    core.msgs(deliver(cid="c1", setting=50, ms=1000))
    core.talk("#t 1000001")                                 # time is up, but loop() has not run yet
    out = core.msgs(deliver(cid="c2", trial="t2", setting=60, ms=500))
    assert [m["type"] for m in out] == ["stimulus_ended", "ack", "stimulus_started"]
    assert out[1]["status"] == "accepted" and core.motor() == 60
