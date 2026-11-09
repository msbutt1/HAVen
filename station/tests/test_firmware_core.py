"""Tests for the reference controller protocol core (C++), compiled and run on this PC.

Proves the C++ protocol logic matches the contract and works with the Python link adapter and
engine. It does NOT test the ESP32 board layer (PWM, buttons, serial): the .ino is uncompiled.
Skipped if no C++ compiler is on PATH.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from haven_station import StationConfig, StationEngine, VirtualClock
from haven_station.contract import validate_command, validate_link_message
from haven_station.link import LinkStationAdapter

FW = Path(__file__).resolve().parents[1] / "firmware"
CXX = shutil.which("g++") or shutil.which("clang++")
pytestmark = pytest.mark.skipif(CXX is None, reason="no C++ compiler on PATH")

CMD = {"schema": "vibeguard.station_command", "version": 1}
EIGHT = ["felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt"]


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    out = tmp_path_factory.mktemp("fw") / "core_cli.exe"
    subprocess.run([CXX, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-o", str(out),
                    str(FW / "tests" / "core_cli.cpp")], check=True)
    return str(out)


class Core:
    def __init__(self, exe):
        self.p = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    def talk(self, *lines):
        """Send lines, then read everything the core printed up to a sync barrier."""
        for line in lines:
            self.p.stdin.write(line.rstrip("\n") + "\n")
        self.p.stdin.write("#sync\n")
        self.p.stdin.flush()
        out = []
        while True:
            line = self.p.stdout.readline().rstrip("\n")
            if line == "#sync":
                return out
            out.append(line)

    def msgs(self, *lines):
        return [json.loads(x) for x in self.talk(*lines) if not x.startswith("#")]

    def motor(self):
        return int(self.talk("#motor")[0].split()[1])

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)


@pytest.fixture
def core(exe):
    c = Core(exe)
    yield c
    c.close()


def cmd(**kw):
    return json.dumps({**CMD, **kw})


def deliver(cid="c1", trial="t1", setting=50, ms=1000):
    return cmd(command_id=cid, type="deliver_stimulus", trial_id=trial, setting=setting, duration_ms=ms)


# -- handshake ----------------------------------------------------------------------------

def test_boot_hello_and_hello_reply_are_valid_and_report_the_range(core):
    boot = core.msgs("#boot")[0]
    assert validate_link_message(boot) == [] and "command_id" not in boot
    reply = core.msgs(cmd(command_id="h1", type="hello"))[0]
    assert validate_link_message(reply) == [] and reply["command_id"] == "h1"
    assert reply["setting_range"] == {"min": 1, "max": 100} and reply["firmware"]["name"].startswith("vibeguard")


# -- stimulus -------------------------------------------------------------------------------

def test_deliver_acks_starts_the_motor_and_ends_it_on_time(core):
    core.talk("#t 1000000")
    out = core.msgs(deliver(setting=60, ms=500))
    assert [m["type"] for m in out] == ["ack", "stimulus_started"] and out[0]["status"] == "accepted"
    assert all(validate_link_message(m) == [] for m in out)
    assert core.motor() == 60 and out[1]["device_t_us"] == 1_000_000
    assert core.msgs("#t 1400000", "#loop") == [] and core.motor() == 60      # not yet
    ended = core.msgs("#t 1500000", "#loop")
    assert [m["type"] for m in ended] == ["stimulus_ended"] and ended[0]["trial_id"] == "t1"
    assert core.motor() == 0


def test_catch_trial_setting_zero_never_turns_the_motor_on(core):
    core.talk("#t 0")
    out = core.msgs(deliver(setting=0, ms=300))
    assert out[0]["status"] == "accepted" and core.motor() == 0
    assert [m["type"] for m in core.msgs("#t 300000", "#loop")] == ["stimulus_ended"]


@pytest.mark.parametrize("bad,reason", [
    (deliver(setting=101), "setting out of range"), (deliver(setting=-5), "setting out of range"),
    (deliver(ms=0), "invalid command"), (deliver(ms=999999), "invalid command"),
    (cmd(command_id="c9", type="deliver_stimulus", trial_id="t1", setting=50), "invalid command"),
    (cmd(command_id="c9", type="self_destruct"), "unknown command"),
])
def test_bad_commands_are_rejected_without_touching_the_motor(core, bad, reason):
    out = core.msgs(bad)
    assert out[0]["status"] == "rejected" and out[0]["reason"] == reason and core.motor() == 0
    assert validate_link_message(out[0]) == []


def test_a_second_stimulus_while_one_is_running_is_rejected_as_busy(core):
    core.msgs(deliver(cid="c1", setting=40, ms=1000))
    out = core.msgs(deliver(cid="c2", trial="t2", setting=80))
    assert out[0]["reason"] == "busy" and core.motor() == 40


def test_stop_turns_the_motor_off_acks_and_cancels_the_pending_end(core):
    core.msgs(deliver(setting=70))
    out = core.msgs(cmd(command_id="s1", type="stop_stimulus"))
    assert out[0]["status"] == "completed" and out[0]["command_id"] == "s1" and core.motor() == 0
    assert core.msgs("#t 5000000", "#loop") == []  # no stimulus_ended for a stopped trial


# -- buttons ---------------------------------------------------------------------------------

def test_button_press_reports_the_response_with_the_last_trial_and_a_valid_time(core):
    core.talk("#t 2000000")
    core.msgs(deliver(trial="abc.1"))
    core.talk("#t 2600000")
    felt = core.msgs("#press felt")[0]
    nf = core.msgs("#press not_felt")[0]
    assert validate_link_message(felt) == [] and felt["response"] == "felt" and felt["trial_id"] == "abc.1"
    assert nf["response"] == "not_felt" and felt["device_t_us"] == 2_600_000


def test_a_press_before_any_trial_has_no_trial_id(core):
    msg = core.msgs("#press felt")[0]
    assert "trial_id" not in msg and validate_link_message(msg) == []


# -- robustness --------------------------------------------------------------------------------

def test_garbage_overlong_and_wrong_schema_lines_are_counted_and_never_answered(core):
    out = core.talk("not json at all", "x" * 600, json.dumps({"schema": "other", "version": 1, "type": "hello",
                                                              "command_id": "a"}),
                    cmd(command_id="a", type="hello").replace('"version": 1', '"version": 2'))
    assert out == []
    assert core.talk("#malformed")[0] == "#malformed 4"
    assert core.msgs(cmd(command_id="ok", type="hello"))[0]["command_id"] == "ok"  # still works


def test_a_value_equal_to_a_key_name_cannot_confuse_the_parser(core):
    out = core.msgs(cmd(command_id="type", type="hello"))
    assert out[0]["type"] == "hello" and out[0]["command_id"] == "type"


def test_device_time_extends_past_the_32_bit_wrap(core):
    core.talk("#t 4294967000")
    core.msgs(deliver(ms=1))
    core.talk("#t 800")                                     # the 32-bit microsecond counter wrapped
    ended = core.msgs("#loop")[0]
    assert ended["type"] == "stimulus_ended" and ended["device_t_us"] == (1 << 32) + 800


# -- end to end: Python adapter + engine against the compiled core -----------------------------

class CoreTransport:
    """LineTransport over the compiled core. Presses are scripted per trial (0.5 s after the start)."""
    is_simulated = True  # a PC build of the core, not hardware

    def __init__(self, core, clock, script):
        self.core, self.clock, self.script = core, clock, list(script)
        self._out, self._due, self._started = [], [], 0

    def _t(self):
        return f"#t {int(round(self.clock() * 1e6))}"

    def _take(self, lines):
        for x in lines:
            if '"stimulus_started"' in x:
                resp = self.script[self._started] if self._started < len(self.script) else None
                self._started += 1
                if resp:
                    self._due.append((self.clock() + 0.5, resp))
            self._out.append(x)

    def write_line(self, line):
        assert validate_command(json.loads(line)) == []
        self._take(self.core.talk(self._t(), line))

    def read_lines(self):
        for item in [d for d in self._due if d[0] <= self.clock()]:
            self._due.remove(item)
            self.core.talk(self._t())
            self._take(self.core.talk(f"#press {item[1]}"))
        self._take(self.core.talk(self._t(), "#loop"))
        out, self._out = self._out, []
        return [x for x in out if not x.startswith("#")]


def test_full_session_through_the_python_adapter_and_the_compiled_core(core):
    clock = VirtualClock()
    tx = CoreTransport(core, clock, EIGHT)
    link = LinkStationAdapter(tx, clock, clock.advance)
    info = link.request_hello()
    assert info["setting_range"] == {"min": 1, "max": 100}
    cfg = StationConfig.full(catch_probability=0.0, start_setting=50, step=10)
    engine = StationEngine(link, cfg, clock=clock, session_id="e2e")
    engine.start()
    for _ in range(20_000):
        clock.advance(0.05)
        engine.tick()
        if engine.state != "testing":
            break
    r = engine.result
    assert engine.state == "completed" and r["outcome"] == "threshold_estimated"
    assert r["threshold"]["value"] == 35.0 and r["reversals"]["levels"] == [30, 40, 30, 40, 30, 40, 30, 40]
    assert r["provenance"]["detail"]["firmware"] == "vibeguard-station-ref 0.1"
    assert core.motor() == 0


def test_abort_through_the_compiled_core_confirms_motor_off(core):
    clock = VirtualClock()
    link = LinkStationAdapter(CoreTransport(core, clock, EIGHT), clock, clock.advance)
    link.request_hello()
    engine = StationEngine(link, StationConfig.full(catch_probability=0.0), clock=clock, session_id="e2e-abort")
    engine.start()
    engine.tick()
    clock.advance(0.1)
    engine.tick()
    assert core.motor() > 0
    assert engine.abort() is True and engine.state == "aborted" and core.motor() == 0


def test_controller_reboot_mid_session_is_a_visible_error(core):
    clock = VirtualClock()
    tx = CoreTransport(core, clock, EIGHT)
    link = LinkStationAdapter(tx, clock, clock.advance)
    link.request_hello()
    engine = StationEngine(link, StationConfig.full(catch_probability=0.0), clock=clock, session_id="e2e-boot")
    engine.start()
    engine.tick()
    tx._take(core.talk("#boot"))            # the firmware restarts and announces itself unasked
    clock.advance(0.05)
    engine.tick()
    assert engine.state == "error" and "restarted" in engine.result["error"]
