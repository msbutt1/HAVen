"""Host-side hardware path: hello handshake, device range, serial transport, self-test, live runner.

All against simulated controllers or pyserial's in-memory ``loop://`` port. Nothing here has touched
the real controller or a real USB serial port.
"""

import json

import pytest

from haven_station import (AdapterError, ScriptedResponder, SimResponse, SimulatedStationAdapter,
                               StationConfig, StationEngine, VirtualClock, run_simulated)
from haven_station.cli import main
from haven_station.link import LinkStationAdapter, SimulatedController
from haven_station.live import run_live
from haven_station.selftest import selftest
from haven_station.serial_transport import SerialTransport

EIGHT = ["felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt", "not_felt", "felt"]


def rig(script=EIGHT, setting_range=(1, 100)):
    clock = VirtualClock()
    ctl = SimulatedController(clock, SimulatedStationAdapter(clock, ScriptedResponder(script)),
                              setting_range=setting_range)
    return clock, ctl, LinkStationAdapter(ctl, clock, clock.advance)


# -- handshake ----------------------------------------------------------------------------------

def test_hello_reports_firmware_and_range_and_labels_provenance():
    clock, ctl, link = rig(setting_range=(1, 60))
    info = link.request_hello()
    assert info["setting_range"] == {"min": 1, "max": 60}
    assert link.provenance == {"source": "simulated", "detail": {"firmware": "simulated-station-controller 0"}}


def test_settings_outside_the_controllers_range_are_refused_before_sending():
    clock, ctl, link = rig(setting_range=(1, 50))
    link.request_hello()
    with pytest.raises(AdapterError):
        link.deliver_stimulus("t1", 60, 500)
    assert not [m for m in ctl.received if m["type"] == "deliver_stimulus"]
    link.deliver_stimulus("t2", 0, 500)      # setting 0 (motor off) is always allowed


def test_no_answer_to_hello_is_a_clear_error():
    clock, ctl, link = rig()
    ctl.silent = True
    with pytest.raises(AdapterError, match="did not answer hello"):
        link.request_hello(timeout_s=3.0)


def test_hello_is_resent_so_a_controller_that_was_still_booting_is_found():
    clock, ctl, _ = rig()
    ctl.silent = True

    def sleep(dt):
        clock.advance(dt)
        if clock() > 1.2:
            ctl.silent = False  # the board finished booting
    link = LinkStationAdapter(ctl, clock, sleep)
    assert link.request_hello(timeout_s=5.0)["setting_range"]["max"] == 100
    assert len([m for m in ctl.received if m["type"] == "hello"]) >= 2


@pytest.mark.parametrize("rng", [(0, 10), (5, 5), (10, 5)])
def test_an_unusable_reported_range_is_rejected(rng):
    clock, ctl, link = rig(setting_range=rng)
    with pytest.raises(AdapterError, match="unusable"):
        link.request_hello()


def test_a_reply_to_our_own_hello_is_not_mistaken_for_a_reboot():
    clock, ctl, link = rig()
    link.request_hello()
    ctl._out.append(json.dumps({"schema": "vibeguard.station_link", "version": 1, "type": "hello",
                                "command_id": "c00001", "firmware": {"name": "x", "version": "1"},
                                "setting_range": {"min": 1, "max": 100}}) + "\n")
    assert link.poll() == []


def test_device_range_rescales_the_staircase_and_drops_the_assumed_range_flag():
    full = StationConfig.full().for_device_range(1, 100)
    assert (full.start_setting, full.step, full.setting_range_source) == (60, 5, "device_hello")
    prev = StationConfig.preview().for_device_range(1, 100)
    assert prev.step == 10 and prev.mode == "preview"
    small = StationConfig.full().for_device_range(10, 50)
    assert (small.min_setting, small.max_setting, small.start_setting, small.step) == (10, 50, 34, 2)
    StationConfig.full().for_device_range(1, 10).validate()
    engine, _ = run_simulated(lambda i, s: SimResponse("felt" if s > 20 else "not_felt", 0.5), full, session_id="dr")
    codes = {x["code"] for x in engine.result["quality"]["limitations"]}
    assert "setting_range_assumed" not in codes


# -- engine fixes found while testing against the controller core -----------------------------

def test_the_next_stimulus_never_starts_while_the_previous_one_is_still_running():
    cfg = StationConfig.full(start_setting=50, step=10, catch_probability=0.0, inter_trial_s=0.0, stimulus_ms=1000)
    engine, adapter = run_simulated(lambda i, s: SimResponse("felt" if i % 2 else "not_felt", 0.1), cfg,
                                    session_id="fast")
    t = engine.result["trials"]
    assert all(b["command_t_s"] - a["command_t_s"] >= 1.0 - 1e-9 for a, b in zip(t, t[1:]))


def test_a_completed_session_leaves_the_motor_off_even_if_the_last_answer_came_mid_stimulus():
    engine, adapter = run_simulated(ScriptedResponder(EIGHT, delay_s=0.2), StationConfig.full(
        start_setting=50, step=10, catch_probability=0.0), session_id="done")
    assert engine.state == "completed" and adapter.motor_on is False
    assert adapter.commands[-1]["type"] == "stop_stimulus"


# -- serial transport (pyserial loopback: bytes written come straight back) -----------------------

def test_serial_transport_writes_and_reads_whole_lines_without_blocking():
    with SerialTransport("loop://", 115200) as tx:
        assert tx.read_lines() == []                          # nothing yet: returns at once
        tx.write_line('{"a":1}')
        tx.write_line('{"b":2}\n')
        assert tx.read_lines() == ['{"a":1}', '{"b":2}']


def test_serial_transport_buffers_partial_lines_and_tolerates_crlf_and_bad_bytes():
    with SerialTransport("loop://") as tx:
        tx._ser.write(b'{"a":')
        assert tx.read_lines() == []
        tx._ser.write(b'1}\r\n\xff\xfe junk\n\n')
        lines = tx.read_lines()
        assert lines[0] == '{"a":1}' and "junk" in lines[1] and len(lines) == 2


def test_serial_transport_drops_a_runaway_line_instead_of_growing():
    with SerialTransport("loop://", 10_000_000) as tx:  # fast, so the simulated line does not time out
        for _ in range(70):                                  # 70 kB with no newline, in chunks
            tx._ser.write(b"x" * 1000)
            assert tx.read_lines() == []
        assert tx.overflows == 1
        tx._ser.write(b"xxxx\n")                              # the runaway line finally ends
        tx.write_line('{"ok":1}')
        assert tx.read_lines() == ['{"ok":1}']              # and it recovers


def test_only_a_plain_port_name_is_labelled_live_a_loopback_url_is_simulated(monkeypatch):
    with SerialTransport("loop://") as tx:
        assert LinkStationAdapter(tx, lambda: 0.0, lambda s: None).provenance["source"] == "simulated"
    import serial
    real = serial.serial_for_url
    monkeypatch.setattr(serial, "serial_for_url", lambda port, **kw: real("loop://", **kw))
    with SerialTransport("COM9") as tx:                      # a plain device name: real hardware
        assert LinkStationAdapter(tx, lambda: 0.0, lambda s: None).provenance["source"] == "live"


def test_a_closed_port_becomes_an_adapter_error_not_a_crash():
    tx = SerialTransport("loop://")
    link = LinkStationAdapter(tx, lambda: 0.0, lambda s: None)
    tx.close()
    with pytest.raises(AdapterError):
        link.deliver_stimulus("t1", 50, 500)


# -- self-test ---------------------------------------------------------------------------------------

def run_selftest(ctl, link, clock, wrong_buttons=False, **kw):
    lines = []

    def prompt(resp):
        other = {"felt": "not_felt", "not_felt": "felt"}[resp]
        ctl.sim.inject_response(other if wrong_buttons else resp, 0.3)
    checks = selftest(link, clock, clock.advance, say=lines.append, prompt=prompt, **kw)
    return checks, lines


def test_selftest_passes_on_a_healthy_simulated_controller():
    clock, ctl, link = rig()
    checks, lines = run_selftest(ctl, link, clock)
    assert all(c.ok for c in checks) and len(checks) == 2 + 3 + 2
    assert any("human check" in x for x in lines)           # it admits it cannot feel the motor
    assert ctl.sim.motor_on is False


def test_selftest_reports_a_silent_controller_and_stops_there():
    clock, ctl, link = rig()
    ctl.silent = True
    checks, _ = run_selftest(ctl, link, clock)
    assert [c.name for c in checks] == ["controller answers hello"] and not checks[0].ok


def test_selftest_catches_swapped_buttons_and_missing_presses():
    clock, ctl, link = rig()
    checks, _ = run_selftest(ctl, link, clock, wrong_buttons=True, motor=False)
    assert [c.ok for c in checks[-2:]] == [False, False] and "swapped" in checks[-1].detail
    clock2, ctl2, link2 = rig()
    checks2 = selftest(link2, clock2, clock2.advance, say=lambda s: None, motor=False, button_wait_s=1.0)
    assert [c.ok for c in checks2[-2:]] == [False, False] and "no press" in checks2[-1].detail


def test_selftest_reports_a_motor_that_rejects_commands():
    clock, ctl, link = rig()
    link.request_hello()
    ctl.reject_commands = True
    checks, _ = run_selftest(ctl, link, clock)
    assert any(not c.ok and "motor pulse" in c.name for c in checks)


# -- live runner -------------------------------------------------------------------------------------

def make_engine(clock, ctl, link, cfg=None):
    link.request_hello()
    return StationEngine(link, cfg or StationConfig.full(start_setting=50, step=10, catch_probability=0.0),
                         clock=clock, session_id="live1")


def test_live_runner_runs_a_session_to_completion_and_reports_events():
    clock, ctl, link = rig()
    seen = []
    result = run_live(make_engine(clock, ctl, link), sleep=clock.advance, poll_interval_s=0.05,
                      on_events=seen.extend)
    assert result["outcome"] == "threshold_estimated" and result["threshold"]["value"] == 35.0
    assert [e["type"] for e in seen].count("session_result") == 1


def test_live_runner_aborts_and_stops_the_motor_on_ctrl_c():
    clock, ctl, link = rig()
    engine = make_engine(clock, ctl, link)
    calls = {"n": 0}

    def sleep(dt):
        clock.advance(dt)
        calls["n"] += 1
        if calls["n"] == 5:
            raise KeyboardInterrupt
    result = run_live(engine, sleep=sleep, poll_interval_s=0.05)
    assert result["state"] == "aborted" and result["abort_reason"] == "keyboard_interrupt"
    assert ctl.sim.motor_on is False


def test_live_runner_honours_an_operator_abort_flag():
    clock, ctl, link = rig()
    engine = make_engine(clock, ctl, link)
    result = run_live(engine, sleep=clock.advance, poll_interval_s=0.05,
                      should_abort=lambda: len(engine.trials) >= 2)
    assert result["state"] == "aborted" and result["abort_reason"] == "operator_abort"


# -- CLI (error paths only: real-time runs are exercised by hand) -----------------------------------------

def test_live_and_selftest_need_a_port_or_simulate(capsys):
    assert main(["live"]) == 3 and "--port is required" in capsys.readouterr().err
    assert main(["selftest"]) == 3


def test_ports_command_runs(capsys):
    assert main(["ports"]) in (0, 1)
