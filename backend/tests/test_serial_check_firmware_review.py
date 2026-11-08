"""Adversarial review of serial_check()'s firmware-specific verdicts (acquisition, config_problems, counters,
device id, hello cadence, seq/loss classification, boot banner, adapter wrapper, CLI wiring). SIMULATED data only.

A fake clock replaces ``hardware.time`` so a 20 s check runs instantly and deterministically. Each test marked
``xfail(strict=True)`` documents a reproduced defect; the others pin verified behaviour.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from conftest import sim_frames

from vibeguard_backend import hardware, serial_select
from vibeguard_backend.adapters import AdapterDisconnected
from vibeguard_backend.hardware import SerialCheckResult, serial_check
from vibeguard_backend.preflight import FAIL, PASS, WARN, Report
from vibeguard_backend.serial_select import DeviceIdSerialAdapter

DEV = "vg-w-2805a5075554"
BOOT_A, BOOT_B = "b-0123456789abcdef", "b-fedcba9876543210"
HW = {"board": "esp32-devkit", "acquisition": "FAKE sources (SIMULATED)", "config_problems": "none",
      "link": "uart0 921600 8N1"}
ZERO = {"emg_dropped_samples": 0, "imu_dropped_samples": 0, "tx_dropped_records": 0, "tx_dropped_bytes": 0,
        "encode_errors": 0}


# ------------------------------------------------------------------------------ harness

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Timed:
    """Delivers (arrival_s, line) items on a fake clock; silence in between."""

    def __init__(self, clock, items):
        self.clock, self.items, self.i, self.port, self.base = clock, list(items), 0, "crafted", None

    def open(self):
        self.base = self.clock.t

    def read_line(self, timeout):
        if self.i < len(self.items) and self.base + self.items[self.i][0] <= self.clock.t + timeout:
            t, line = self.items[self.i]
            self.i += 1
            self.clock.t = max(self.clock.t, self.base + t)
            return line
        self.clock.t += timeout
        return None

    def close(self):
        pass


def _arrival(r):
    if r["type"] in ("emg_batch", "imu_batch"):
        t = r["timing"]
        return (t["t0_us"] + len(r["samples"]) * t.get("interval_us", 1000.0)) / 1e6 if t["mode"] == "uniform" \
            else t["t_us"][-1] / 1e6
    return r["t_us"] / 1e6


def records(duration_s=6, device_id=DEV, boot=BOOT_A, t_offset=0.0, hardware_=HW, counters=None):
    """[arrival, record] pairs of one simulated boot, dressed as the wearable firmware."""
    out = []
    for f in sim_frames("happy_path", duration_s=duration_s, run_id="fw"):
        r = json.loads(f)
        r["device_id"], r["boot_id"] = device_id, boot
        if r["type"] == "hello":
            r["hardware"] = dict(hardware_)
        if r["type"] == "status" and counters is not None:
            r["counters"] = counters(_arrival(r)) if callable(counters) else dict(counters)
        out.append([_arrival(r) + t_offset, r])
    return out


def resent_hello(items, every=5.0, upto=None):
    hello = next(r for _, r in items if r["type"] == "hello")
    t = every
    while t < (upto or items[-1][0]):
        h = json.loads(json.dumps(hello))
        h["t_us"] = int(t * 1e6)
        items.append([t, h])
        t += every
    return items


def wire(items, drop=lambda t, r: False):
    """Order by arrival, give every (boot) a shared seq counter like the firmware, drop, then serialise."""
    items = sorted(items, key=lambda p: p[0])
    seq, out = {}, []
    for t, r in items:
        r = json.loads(json.dumps(r))
        r["seq"] = seq.get(r["boot_id"], 0)
        seq[r["boot_id"]] = r["seq"] + 1
        gone = drop(t, r)
        r.pop("_doomed", None)
        if not gone:
            out.append((t, (json.dumps(r, separators=(",", ":")) + "\n").encode()))
    return out


def run(monkeypatch, items, seconds, **kw):
    clock = _Clock()
    monkeypatch.setattr(hardware, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    ad = _Timed(clock, items)
    return serial_check("x", 921600, seconds, adapter_factory=lambda p, b: ad, **kw)


def named(rep, name):
    found = [c for c in rep.checks if c.name == name]
    assert found, (name, [c.name for c in rep.checks])
    return found[0]


def has(rep, name):
    return any(c.name == name for c in rep.checks)


# ------------------------------------------------------------------------------ acquisition is real

def hw(**kw):
    return {**HW, **kw}


@pytest.mark.parametrize("acq", ["FAKE sources (SIMULATED)", "fake", "simulated sensors"])
def test_acquisition_warns_when_the_firmware_says_fake_or_simulated(monkeypatch, acq):
    res = run(monkeypatch, wire(records(hardware_=hw(acquisition=acq))), 6)
    assert named(res.report, "acquisition is real").level == WARN


@pytest.mark.parametrize("acq", [None, "", "ADS1299 + MPU6050 (real)"])
def test_acquisition_null_empty_or_real_text_raises_no_warning_and_does_not_crash(monkeypatch, acq):
    res = run(monkeypatch, wire(records(hardware_=hw(acquisition=acq))), 6)
    assert not has(res.report, "acquisition is real")
    json.dumps(res.stats)


@pytest.mark.parametrize("acq", ["REAL (not simulated)", "ADS1299 real sensors, no simulated source"])
def test_a_real_acquisition_string_that_negates_simulated_is_not_called_fake(monkeypatch, acq):
    res = run(monkeypatch, wire(records(hardware_=hw(acquisition=acq))), 6)
    assert not has(res.report, "acquisition is real")


def test_the_silent_absence_of_the_acquisition_key_leaves_no_verdict_at_all(monkeypatch):
    h = {k: v for k, v in HW.items() if k != "acquisition"}
    res = run(monkeypatch, wire(records(hardware_=h)), 6)
    assert not has(res.report, "acquisition is real")  # pinned: neither PASS nor WARN, so 'real' is never confirmed


# ------------------------------------------------------------------------------ config_problems

@pytest.mark.parametrize("cp", ["none", "None", "  NONE ", "", None, "ok", "[]", "null"])
def test_config_problems_that_mean_nothing_do_not_warn(monkeypatch, cp):
    res = run(monkeypatch, wire(records(hardware_=hw(config_problems=cp))), 6)
    assert not has(res.report, "firmware configuration")


@pytest.mark.parametrize("cp", ["imu range unset", "imu_range_unset,emg_gain_unset"])
def test_config_problems_text_warns_and_is_shown_verbatim(monkeypatch, cp):
    res = run(monkeypatch, wire(records(hardware_=hw(config_problems=cp))), 6)
    c = named(res.report, "firmware configuration")
    assert c.level == WARN and cp in c.detail


@pytest.mark.parametrize("cp", ["0", "no problems", "none detected", "n/a"])
def test_other_spellings_of_no_config_problems_do_not_warn(monkeypatch, cp):
    res = run(monkeypatch, wire(records(hardware_=hw(config_problems=cp))), 6)
    assert not has(res.report, "firmware configuration")


def test_a_missing_config_problems_key_gives_no_verdict_either_way(monkeypatch):
    h = {k: v for k, v in HW.items() if k != "config_problems"}
    res = run(monkeypatch, wire(records(hardware_=h)), 6)
    assert not has(res.report, "firmware configuration")  # pinned: absence of the field is not reported


# ------------------------------------------------------------------------------ device counters

def test_counters_are_taken_from_the_last_status_only_and_stats_keep_them(monkeypatch):
    res = run(monkeypatch, wire(records(counters=lambda t: {**ZERO, "tx_dropped_records": int(t) * 2})), 6)
    last = res.stats["device_counters"]["tx_dropped_records"]
    assert last == 2 * int(max(t for t, _ in records(counters=ZERO) if _["type"] == "status")) or last > 0
    c = named(res.report, "device counters")
    assert c.level == WARN and f"tx_dropped_records={last}" in c.detail


def test_counters_that_never_changed_during_the_check_are_not_presented_as_current_loss(monkeypatch):
    res = run(monkeypatch, wire(records(counters={**ZERO, "tx_dropped_records": 40, "tx_dropped_bytes": 30000})), 6)
    assert res.stats["seq_missing"] == 0
    c = named(res.report, "device counters")
    text = (c.detail + c.fix).lower()
    assert c.level == PASS or "since boot" in text or "before the check" in text


def test_drops_counted_before_a_reboot_are_not_forgotten(monkeypatch):
    a = records(duration_s=4, boot=BOOT_A, counters={**ZERO, "tx_dropped_records": 5})
    b = records(duration_s=4, boot=BOOT_B, t_offset=4.5, counters=ZERO)
    res = run(monkeypatch, wire(a + b), 9)
    assert res.stats["reboots"] == 1
    assert named(res.report, "device counters").level == WARN


def test_a_nonzero_unmapped_error_counter_is_not_reported_as_all_zero(monkeypatch):
    res = run(monkeypatch, wire(records(counters={**ZERO, "uart_rx_overruns": 9})), 6)
    c = named(res.report, "device counters")
    assert not (c.level == PASS and "all drop and error counters are zero" in c.detail)


def test_no_counters_at_all_gives_no_device_counters_verdict(monkeypatch):
    items = records()
    for _, r in items:
        r.pop("counters", None)
    res = run(monkeypatch, wire(items), 6)
    assert not has(res.report, "device counters") and "device_counters" not in res.stats  # pinned (silent)


def test_zero_counters_pass(monkeypatch):
    res = run(monkeypatch, wire(records(counters=ZERO)), 6)
    assert named(res.report, "device counters").level == PASS


# ------------------------------------------------------------------------------ device id

def test_device_id_pass_fail_and_reboot_keep_the_same_id(monkeypatch):
    a = records(duration_s=3, boot=BOOT_A)
    b = records(duration_s=3, boot=BOOT_B, t_offset=3.5)
    res = run(monkeypatch, wire(a + b), 7, expect_device_id=DEV)
    assert named(res.report, "device id").level == PASS and res.stats["reboots"] == 1
    assert named(res.report, "device rebooted").level == WARN
    res2 = run(monkeypatch, wire(records(device_id="vg-w-aaaaaaaaaaaa")), 6, expect_device_id=DEV)
    assert named(res2.report, "device id").level == FAIL and res2.report.failed


def test_two_boards_on_one_link_warn_without_an_expected_id_and_fail_with_one(monkeypatch):
    items = records() + records(device_id="vg-w-bbbbbbbbbbbb", boot=BOOT_B, t_offset=0.003)
    res = run(monkeypatch, wire(items), 6)
    assert named(res.report, "device id").level == WARN
    res2 = run(monkeypatch, wire(items), 6, expect_device_id=DEV)
    assert named(res2.report, "device id").level == FAIL


def test_a_second_board_is_not_reported_as_a_reboot(monkeypatch):
    items = records() + records(device_id="vg-w-bbbbbbbbbbbb", boot=BOOT_B, t_offset=0.003)
    res = run(monkeypatch, wire(items), 6)
    assert res.stats["reboots"] == 0 and not has(res.report, "device rebooted")


# ------------------------------------------------------------------------------ hello cadence

def test_a_resent_hello_carrying_the_next_seq_is_neither_a_duplicate_nor_a_gap(monkeypatch):
    items = resent_hello(records(duration_s=16), every=5.0)
    res = run(monkeypatch, wire(items), 16)
    assert res.stats["seq_missing"] == 0 and res.stats["seq_duplicates"] == 0 and res.stats["reboots"] == 0
    assert 4.9 <= res.stats["hello_interval_s"] <= 5.1
    assert named(res.report, "no records lost").level == PASS


def test_a_three_second_check_started_mid_run_cannot_see_the_5s_resent_hello(monkeypatch):
    items = [p for p in records(duration_s=8) if p[1]["type"] != "hello"]
    items = resent_hello(items + [[0.0, records(duration_s=1)[0][1]]], every=4.9)
    items = [p for p in items if p[1]["type"] != "hello" or p[0] > 4]
    res = run(monkeypatch, wire(items), 3)
    c = named(res.report, "hello received")
    assert c.level == WARN


def test_hello_warning_tells_the_operator_to_run_longer_than_the_resend_period(monkeypatch):
    items = [p for p in records(duration_s=8) if p[1]["type"] != "hello"]
    res = run(monkeypatch, wire(items), 3)
    c = named(res.report, "hello received")
    import re
    assert re.search(r"\b(5|6)\s?s\b|--seconds", c.fix + c.detail)


def test_a_hello_that_is_never_resent_in_a_long_check_is_reported(monkeypatch):
    res = run(monkeypatch, wire(records(duration_s=20)), 20)  # one hello at t=0, nothing for 20 s
    assert any(c.level == WARN and "hello" in (c.name + c.detail).lower() for c in res.report.checks)


# ------------------------------------------------------------------------------ seq gaps vs counters

def _tx_drop_stream(counter_after):
    items = records(duration_s=6, counters=lambda t: {**ZERO, "tx_dropped_records": counter_after if t > 2.2 else 0})
    batches = sorted((p for p in items if p[1]["type"] == "emg_batch" and p[0] > 2.0), key=lambda p: p[0])[:3]
    doomed = {id(p[1]) for p in batches}
    return items, doomed


def _drop_ids(items, doomed):
    kept = []
    for p in items:
        kept.append(p)
    # wire() assigns seq in arrival order; drop by identity of the original record via a marker field
    for _, r in items:
        r["_doomed"] = id(r) in doomed
    return kept


def test_a_whole_frame_dropped_by_the_tx_queue_is_a_seq_gap_and_an_unflagged_sample_index_jump(monkeypatch):
    items, doomed = _tx_drop_stream(3)
    items = _drop_ids(items, doomed)
    res = run(monkeypatch, wire(items, drop=lambda t, r: r["_doomed"]), 6)
    assert res.stats["seq_missing"] == 3 and res.stats["dropped_samples"] == 0
    assert res.stats["sample_index_gaps"] > 0
    c = named(res.report, "no records lost")
    assert c.level == WARN and "missing from the sample index" in c.detail


def test_seq_gaps_with_zero_tx_counters_before_and_after_are_not_blamed_on_the_tx_queue(monkeypatch):
    items, doomed = _tx_drop_stream(0)
    items = _drop_ids(items, doomed)
    res = run(monkeypatch, wire(items, drop=lambda t, r: r["_doomed"]), 6)
    assert res.stats["seq_missing"] == 3 and res.stats["device_counters"]["tx_dropped_records"] == 0
    c = named(res.report, "no records lost")
    assert "TX queue dropped" not in c.detail


def test_seq_gaps_matching_the_tx_dropped_records_counter_are_attributed_to_it(monkeypatch):
    items, doomed = _tx_drop_stream(3)
    items = _drop_ids(items, doomed)
    res = run(monkeypatch, wire(items, drop=lambda t, r: r["_doomed"]), 6)
    c = named(res.report, "no records lost")
    assert "tx_dropped_records" in c.detail + c.fix


# ------------------------------------------------------------------------------ boot banner

BANNER = [b"ets Jun  8 2016 00:22:57\n", b"\xfe\x00\xe0\x80\x80\xf8\x00\xfe\n", b"\x00\x00\xff\x1c\x9e\n"]


@pytest.mark.parametrize("duration", [1, 3])
def test_a_boot_banner_before_the_first_frame_does_not_degrade_the_contract_verdict(monkeypatch, duration):
    wired = wire(records(duration_s=duration))
    items = [(0.0, b) for b in BANNER] + [(t + 0.01, b) for t, b in wired]
    res = run(monkeypatch, items, duration + 1)
    c = named(res.report, "contract frames")
    assert res.stats["invalid"] and c.level == PASS or "boot" in (c.detail + c.fix).lower()
    assert not res.report.failed


def test_the_banner_lines_are_counted_as_invalid_and_the_valid_frames_still_pass_every_other_check(monkeypatch):
    wired = wire(records(duration_s=6, counters=ZERO))
    items = [(0.0, b) for b in BANNER] + [(t + 0.01, b) for t, b in wired]
    res = run(monkeypatch, items, 7)
    assert sum(res.stats["invalid"].values()) == 3 and res.stats["valid"] == len(wired)
    assert res.stats["seq_missing"] == 0 and res.stats["seq_duplicates"] == 0
    assert named(res.report, "device counters").level == PASS


# ------------------------------------------------------------------------------ DeviceIdSerialAdapter inside serial_check

class _FakeSerial:
    """Stands in for SerialAdapter inside serial_select: plays lines, keeps SerialAdapter's counters after close."""
    plan: dict = {}

    def __init__(self, port, baud, dtr=None, rts=None):
        self.port, self.dtr, self.rts = port, dtr, rts
        spec = self.plan.get(Path(port).name, {})
        self.lines = list(spec.get("lines", []))
        self.partial_discarded = spec.get("partial_discarded", 0)
        self.hello_recovered = spec.get("hello_recovered", 0)
        self.oversize_resyncs = 0

    def open(self):
        pass

    def read_line(self, timeout):
        if self.lines:
            return self.lines.pop(0)
        import time as _t
        _t.sleep(min(timeout, 0.01))
        return None

    def close(self):
        pass


def _board_lines(device_id, boot=BOOT_A, seconds=2):
    return [b for _, b in wire(records(duration_s=seconds, device_id=device_id, boot=boot))]


@pytest.fixture
def two_ports(tmp_path, monkeypatch):
    for n in ("ttyUSB0", "ttyUSB1"):
        (tmp_path / n).write_text("")
    monkeypatch.setattr(serial_select, "SerialAdapter", _FakeSerial)
    return tmp_path


def _real_clock_check(pattern, **kw):
    return serial_check(pattern, 921600, 1.0, adapter_factory=lambda p, b: DeviceIdSerialAdapter(
        p, b, DEV, dtr=False, rts=False, probe_s=0.5), expect_device_id=DEV, **kw)


def test_device_id_adapter_selects_the_right_board_end_to_end(two_ports):
    _FakeSerial.plan = {"ttyUSB0": {"lines": _board_lines("vg-w-aaaaaaaaaaaa")}, "ttyUSB1": {"lines": _board_lines(DEV)}}
    res = _real_clock_check(str(two_ports / "ttyUSB*"))
    assert named(res.report, "device id").level == PASS and res.stats["seq_missing"] == 0
    assert res.stats["valid"] > 0 and res.stats["valid"] == res.stats["lines"]


def test_a_glob_resolved_by_device_id_is_not_called_ambiguous(two_ports):
    _FakeSerial.plan = {"ttyUSB0": {"lines": _board_lines("vg-w-aaaaaaaaaaaa")}, "ttyUSB1": {"lines": _board_lines(DEV)}}
    res = _real_clock_check(str(two_ports / "ttyUSB*"))
    assert not has(res.report, "port is ambiguous")


def test_first_line_discarded_survives_the_wrapper_closing(two_ports):
    _FakeSerial.plan = {"ttyUSB0": {"lines": _board_lines("vg-w-aaaaaaaaaaaa")},
                        "ttyUSB1": {"lines": _board_lines(DEV), "partial_discarded": 1}}
    res = _real_clock_check(str(two_ports / "ttyUSB*"))
    assert res.stats["first_line_discarded"] == 1


def test_wrapper_forwards_hello_recovered_like_the_plain_adapter(two_ports):
    ad = DeviceIdSerialAdapter(str(two_ports / "ttyUSB*"), 921600, DEV, probe_s=0.5)
    assert hasattr(ad, "hello_recovered")


def test_no_matching_board_gives_advice_about_the_device_id(two_ports):
    _FakeSerial.plan = {"ttyUSB0": {"lines": _board_lines("vg-w-aaaaaaaaaaaa")}, "ttyUSB1": {"lines": _board_lines("vg-w-bbbbbbbbbbbb")}}
    res = _real_clock_check(str(two_ports / "ttyUSB*"))
    c = named(res.report, "port opens")
    assert c.level == FAIL and res.report.failed
    assert "device" in c.fix.lower() or "device-id" in c.fix.lower()


# ------------------------------------------------------------------------------ CLI wiring (serial-check)

def _cli(monkeypatch, tmp_path, argv, profile=None):
    from vibeguard_backend.cli import main
    seen = {}

    def fake_find(port, bauds, **kw):
        seen["find"] = kw
        return [{"baud": bauds[0], "lines": 5, "valid": 5, "share": 1.0}]

    def fake_check(port, baud, seconds, *a, **kw):
        seen["check"] = {"baud": baud, **kw}
        return SerialCheckResult(port, baud, seconds, Report(), {})

    monkeypatch.setattr(hardware, "find_baud", fake_find)
    monkeypatch.setattr(hardware, "serial_check", fake_check)
    if profile is not None:
        p = tmp_path / "hw.json"
        p.write_text(json.dumps(profile))
        argv = argv + ["--profile", str(p)]
    code = main(["serial-check", "--port", "/dev/ttyUSB*", "--seconds", "2"] + argv)
    return code, seen


ESP = {"name": "w", "baud": 921600, "device_id": DEV, "emg": {"output": "raw"}, "serial": {"dtr": False, "rts": False}}


def test_cli_passes_the_device_id_to_serial_check_and_exits_zero_on_a_clean_report(monkeypatch, tmp_path):
    code, seen = _cli(monkeypatch, tmp_path, ["--device-id", DEV])
    assert code == 0 and seen["check"]["expect_device_id"] == DEV
    assert isinstance(seen["check"]["adapter_factory"]("/dev/ttyUSB*", 921600), DeviceIdSerialAdapter)


def test_try_bauds_probes_through_the_device_id_adapter(monkeypatch, tmp_path):
    _, seen = _cli(monkeypatch, tmp_path, ["--device-id", DEV, "--try-bauds", "115200,921600"])
    assert seen["find"].get("adapter_factory") is not None


def test_profile_dtr_rts_false_reach_the_device_id_adapter(monkeypatch, tmp_path):
    _, seen = _cli(monkeypatch, tmp_path, [], profile=ESP)
    ad = seen["check"]["adapter_factory"]("/dev/ttyUSB*", 921600)
    assert ad.dtr is False and ad.rts is False


def test_profile_dtr_rts_false_reach_find_baud(monkeypatch, tmp_path):
    _, seen = _cli(monkeypatch, tmp_path, ["--try-bauds", "115200,921600"], profile=ESP)
    assert seen["find"]["dtr"] is False and seen["find"]["rts"] is False


def test_profile_baud_is_used_by_serial_check(monkeypatch, tmp_path):
    _, seen = _cli(monkeypatch, tmp_path, [], profile={**ESP, "baud": 230400})
    assert seen["check"]["baud"] == 230400


# ------------------------------------------------------------------------------ exit codes and --json

def test_stats_are_json_serialisable_without_a_default_hook_for_a_rich_run(monkeypatch):
    a = records(duration_s=4, boot=BOOT_A, counters={**ZERO, "tx_dropped_records": 2})
    b = records(duration_s=4, boot=BOOT_B, t_offset=4.5, device_id="vg-w-bbbbbbbbbbbb")
    res = run(monkeypatch, wire(resent_hello(a + b)) + [(0.0, b"\xff\xfe junk\n")], 9)
    json.dumps(res.stats, allow_nan=False)


def test_exit_status_follows_report_failed_for_the_firmware_checks(monkeypatch):
    ok = run(monkeypatch, wire(records(counters=ZERO)), 6)
    assert not ok.report.failed                                        # FAKE acquisition + counters only warn
    bad = run(monkeypatch, wire(records(device_id="vg-w-aaaaaaaaaaaa")), 6, expect_device_id=DEV)
    assert bad.report.failed
