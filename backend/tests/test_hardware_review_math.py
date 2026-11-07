"""Adversarial review of serial_check()'s measurements and verdicts, on crafted frame streams.

Everything here is SIMULATED: frames are built in memory and fed through the adapter_factory hook with a
virtual clock (no sleeping, no real serial port). Passing tests pin what was verified to work; each
xfail(strict=True) is a reproduced defect that would mislead the team on a real board.
"""

from __future__ import annotations

import copy
import json
import math
import types

import numpy as np
import pytest
from conftest import sim_frames

import vibeguard_backend.hardware as hw
from vibeguard_backend.adapters import AdapterDisconnected
from vibeguard_backend.hardware import serial_check
from vibeguard_backend.preflight import FAIL, PASS, WARN
from vibeguard_signals import contract

BAUD = 3_000_000  # virtual link: no bandwidth verdicts unless a test asks for one


def check(report, name):
    found = [c for c in report.checks if c.name == name]
    assert found, f"no check named {name!r}: {[c.name for c in report.checks]}"
    return found[0]


def names(report):
    return [c.name for c in report.checks]


# ------------------------------------------------------------------------------ crafted frames + virtual clock

_T = {}
for _f in sim_frames(duration_s=2):
    _r = json.loads(_f)
    _T.setdefault(_r["type"], _r)


def _line(rec) -> bytes:
    raw = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
    rec2, issues = contract.parse_line(raw)
    assert rec2 is not None and not issues, [(i.code, i.path, i.message) for i in issues]
    return raw


def hello(boot, seq=0, raw_limits=(0, 4095), emg_channels=1):
    r = copy.deepcopy(_T["hello"])
    r.update(boot_id=boot, seq=seq)
    emg = r["streams"]["emg"]
    emg["raw_limits"] = {"min": raw_limits[0], "max": raw_limits[1]} if raw_limits else None
    if emg_channels == 2:
        emg["channels"] = ["emg0", "emg1"]
        emg["units"] = {"emg0": "adc_counts", "emg1": "adc_counts"}
    return _line(r)


def status(boot, seq):
    r = copy.deepcopy(_T["status"])
    r.update(boot_id=boot, seq=seq)
    return _line(r)


def emg_batch(boot, seq, idx, rows, t0_us=None, rate=1000.0, flags=(), dropped=0, explicit=None):
    r = copy.deepcopy(_T["emg_batch"])
    ch = len(rows[0])
    r.update(boot_id=boot, seq=seq, first_sample_index=idx, sample_rate_hz=rate)
    if ch == 2:
        r["channels"], r["units"] = ["emg0", "emg1"], {"emg0": "adc_counts", "emg1": "adc_counts"}
    r["samples"] = [list(map(lambda v: None if v is None else int(v), row)) for row in rows]
    if explicit is not None:
        r["timing"] = {"mode": "explicit", "t_us": [int(t) for t in explicit]}
    else:
        t0 = 5000 + idx * 1e6 / rate if t0_us is None else t0_us
        r["timing"] = {"mode": "uniform", "t0_us": int(round(t0)), "interval_us": 1e6 / rate}
    q = {"flags": list(flags)}
    if dropped:
        q["dropped_samples_before"] = dropped
    r["quality"] = q
    return _line(r)


def emg_stream(x, boot="b1", seq0=1, idx0=0, rate=1000.0, batch=50, host_rate=None, t0_us=None, flags=None,
               dropped_every=None):
    """x: 1-D array (one channel) or 2-D (n, channels); returns [(host_delay_s, line)]; seq advances per batch."""
    x = np.asarray(x)
    if x.ndim == 1:
        x = x[:, None]
    out, seq = [], seq0
    host_rate = host_rate or rate
    for i in range(0, len(x), batch):
        rows = x[i:i + batch].tolist()
        t0 = None if t0_us is None else t0_us + i * 1e6 / rate
        out.append((len(rows) / host_rate, emg_batch(boot, seq, idx0 + i, rows, t0_us=t0, rate=rate,
                                                     flags=flags or ())))
        seq += 1
    return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class VReplayer:
    """Plays (delay_before_s, line) items on a virtual clock; silence advances the clock by the timeout."""

    def __init__(self, clock, items, interrupt_at_end=False):
        self.clock, self.items, self.i, self.port = clock, list(items), 0, "crafted"
        self.closed = False
        self.interrupt_at_end = interrupt_at_end

    def open(self):
        pass

    def read_line(self, timeout):
        if self.i < len(self.items):
            delay, line = self.items[self.i]
            self.i += 1
            self.clock.t += delay
            return line
        if self.interrupt_at_end:
            raise KeyboardInterrupt
        self.clock.t += timeout
        return None

    def close(self):
        self.closed = True


def run(items, seconds, monkeypatch, baud=BAUD, mains_hz=None, save=None, interrupt_at_end=False):
    clock = Clock()
    monkeypatch.setattr(hw, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=lambda s: None))
    adapters = []

    def factory(p, b):
        a = VReplayer(clock, items, interrupt_at_end)
        adapters.append(a)
        return a
    res = serial_check("x", baud, seconds=seconds, save=save, mains_hz=mains_hz, adapter_factory=factory)
    res.adapters = adapters
    return res


def emg_noise(n, seed=3, std=25.0, bias=2048):
    return np.clip(np.rint(bias + np.random.default_rng(seed).normal(0, std, n)), 0, 4095)


def with_hello(stream, boot="b1"):
    return [(0.0, hello(boot))] + stream


# ------------------------------------------------------------------------------ sample-rate measurement

def test_rate_is_right_when_samples_were_dropped_on_the_device(monkeypatch):
    # the device drops 100 samples before every 10th batch: first_sample_index jumps, the device clock does not
    items, seq, idx = [(0.0, hello("b1"))], 1, 0
    for k in range(120):
        dropped = 100 if k and k % 10 == 0 else 0
        idx += dropped
        items.append((0.05, emg_batch("b1", seq, idx, [[2048 + (i % 7)] for i in range(50)],
                                      flags=("overrun",) if dropped else (), dropped=dropped)))
        seq += 1
        idx += 50
    res = run(items, 8, monkeypatch)
    assert check(res.report, "emg sample rate").level == PASS
    assert res.stats["emg"]["measured_hz"] == pytest.approx(1000, rel=0.002)  # index span / time span stay consistent
    assert res.stats["dropped_samples"] == 1100
    assert check(res.report, "no records lost").level == WARN


def test_explicit_timing_with_jitter_gives_the_right_rate(monkeypatch):
    rng = np.random.default_rng(5)
    items, seq = [(0.0, hello("b1"))], 1
    for k in range(120):
        ts = [5000 + (k * 50 + i) * 1000 + int(rng.integers(-30, 30)) for i in range(50)]
        items.append((0.05, emg_batch("b1", seq, k * 50, [[2048 + i % 5] for i in range(50)], explicit=ts)))
        seq += 1
    res = run(items, 7, monkeypatch)
    assert check(res.report, "emg sample rate").level == PASS
    assert res.stats["emg"]["measured_hz"] == pytest.approx(1000, rel=0.002)


def test_a_reboot_mid_check_without_a_late_start_still_measures_sanely(monkeypatch):
    a = emg_stream(emg_noise(4000), boot="A")
    b = emg_stream(emg_noise(4000, seed=4), boot="B")
    res = run(with_hello(a, "A") + [(0.0, hello("B"))] + b, 10, monkeypatch)
    assert res.stats["reboots"] == 1
    assert check(res.report, "emg sample rate").level == PASS


def test_connect_midstream_then_reboot_does_not_yield_a_nan_rate_failure(monkeypatch):
    a = emg_stream(emg_noise(2000), boot="A", idx0=12000, t0_us=12_005_000)
    b = emg_stream(emg_noise(3000, seed=4), boot="B")
    res = run(with_hello(a, "A") + [(0.0, hello("B"))] + b, 8, monkeypatch)
    c = check(res.report, "emg sample rate")
    assert c.level != FAIL and "nan" not in c.detail.lower()
    json.dumps(res.stats, allow_nan=False)


def test_samples_arriving_slower_than_the_device_claims_do_not_pass(monkeypatch):
    s = emg_stream(emg_noise(8000), host_rate=800.0)  # device stamps say 1000 Hz; host receives 800 samples/s for 10 s
    res = run(with_hello(s), 10, monkeypatch)
    c = check(res.report, "emg clock vs host")  # device timestamps imply 1000 Hz, the host receives 800 Hz
    assert c.level == WARN and res.stats["emg"]["host_measured_hz"] < 900


def test_silent_sample_index_jumps_are_not_reported_as_no_loss(monkeypatch):
    items, seq, idx = [(0.0, hello("b1"))], 1, 0
    for k in range(100):
        if k and k % 10 == 0:
            idx += 200  # 200 samples vanished on the device and the firmware did not say so
        items.append((0.05, emg_batch("b1", seq, idx, [[2048 + i % 9] for i in range(50)])))
        seq += 1
        idx += 50
    res = run(items, 6, monkeypatch)
    assert res.stats["events"].get("sample_gap", 0) >= 1  # the pipeline itself noticed
    assert check(res.report, "no records lost").level != PASS


# ------------------------------------------------------------------------------ seq accounting across boots

def test_seq_gaps_are_counted_per_boot_and_interleaved_boots_are_not_duplicates(monkeypatch):
    def b(boot, seq, i):
        return (0.05, emg_batch(boot, seq, i * 50, [[2048 + k % 5] for k in range(50)]))
    items = [(0.0, hello("A")), b("A", 1, 0), b("A", 2, 1), b("B", 1, 0), b("B", 2, 1), b("A", 3, 2), b("B", 5, 2)]
    res = run(items, 3, monkeypatch)
    assert res.stats["seq_duplicates"] == 0
    assert res.stats["seq_missing"] == 2  # B went 2 -> 5
    assert res.stats["reboots"] == 1


def test_reboot_with_a_constant_boot_id_is_not_swallowed_as_duplicates(monkeypatch):
    a = emg_stream(emg_noise(4000), boot="same")
    b = emg_stream(emg_noise(4000, seed=4), boot="same")  # seq restarts at 1, index and device time restart at 0
    res = run(with_hello(a) + b, 10, monkeypatch)
    assert res.stats["emg"]["samples"] >= 7500  # nothing should vanish from the measurement
    assert res.stats["reboots"] >= 1


# ------------------------------------------------------------------------------ EMG rawness heuristic

def _band_limited_noise(n, fs, fc, std, seed):
    spec = np.fft.rfft(np.random.default_rng(seed).normal(0, 1, n))
    spec[np.fft.rfftfreq(n, 1 / fs) > fc] = 0
    y = np.fft.irfft(spec, n)
    return y / y.std() * std


def test_ordinary_raw_emg_passes_the_rawness_check(monkeypatch):
    res = run(with_hello(emg_stream(emg_noise(5000, std=60))), 6, monkeypatch)
    assert check(res.report, "emg is raw").level == PASS


def test_oversampled_band_limited_raw_emg_is_not_called_an_envelope(monkeypatch):
    x = np.clip(np.rint(2048 + _band_limited_noise(30000, 10000.0, 500.0, 150, 11)), 0, 4095)
    res = run(with_hello(emg_stream(x, rate=10000.0, batch=250)), 4, monkeypatch)
    assert check(res.report, "emg is raw").level != FAIL  # oversampled raw EMG is not an envelope


def test_raw_emg_with_baseline_wander_is_not_called_an_envelope(monkeypatch):
    n = 6000
    t = np.arange(n) / 1000.0
    x = np.clip(np.rint(2048 + 600 * np.sin(2 * np.pi * 1.5 * t) + _band_limited_noise(n, 1000.0, 450.0, 40, 2)), 0, 4095)
    res = run(with_hello(emg_stream(x)), 7, monkeypatch)
    assert check(res.report, "emg is raw").level != FAIL


def test_a_short_window_envelope_is_not_passed_as_raw(monkeypatch):
    rng = np.random.default_rng(8)
    raw = rng.normal(0, 80, 8000)  # steady contraction
    env = np.convolve(np.abs(raw), np.ones(20) / 20, mode="same")
    x = np.clip(np.rint(env), 0, 4095)
    res = run(with_hello(emg_stream(x)), 9, monkeypatch)
    assert check(res.report, "emg is raw").level != PASS  # a smoothed envelope is flagged, not passed as raw


def test_every_emg_channel_is_checked_for_rawness(monkeypatch):
    n = 4000
    ch0 = emg_noise(n, std=60)
    ch1 = np.clip(np.rint(600 + 400 * np.sin(2 * np.pi * 1.5 * np.arange(n) / 1000.0)), 0, 4095)  # obvious envelope
    items = [(0.0, hello("b1", emg_channels=2))] + emg_stream(np.stack([ch0, ch1], axis=1))
    res = run(items, 5, monkeypatch)
    assert check(res.report, "emg is raw").level == PASS               # channel 0 is genuinely raw
    ch1_checks = [c for c in res.report.checks if c.name.startswith("emg is raw (")]
    assert ch1_checks and ch1_checks[0].level == FAIL, "the envelope on channel 1 must be flagged"


# ------------------------------------------------------------------------------ mains hum

def test_50_hz_hum_is_found_when_the_mains_frequency_is_not_given(monkeypatch):
    t = np.arange(6000) / 1000.0
    x = np.clip(np.rint(2048 + 400 * np.sin(2 * np.pi * 50 * t) + np.random.default_rng(1).normal(0, 40, 6000)), 0, 4095)
    res = run(with_hello(emg_stream(x)), 7, monkeypatch)
    assert check(res.report, "mains hum").level == WARN and "50 Hz" in check(res.report, "mains hum").detail
    assert res.stats["emg"]["mains_60hz_power_share"] < 0.05


def test_hum_carried_by_harmonics_is_not_missed(monkeypatch):
    t = np.arange(8000) / 1000.0
    hum = 40 * np.sin(2 * np.pi * 50 * t) + sum(100 * np.sin(2 * np.pi * 50 * k * t + k) for k in (2, 3, 4, 5))
    x = np.clip(np.rint(2048 + hum + np.random.default_rng(1).normal(0, 25, 8000)), 0, 4095)
    res = run(with_hello(emg_stream(x)), 9, monkeypatch)
    assert "mains hum" in names(res.report)


def _late_change_stream(late):
    """30 s at 1 kHz: first 20 s clean noise, last 10 s = late(t)."""
    n = 30000
    x = emg_noise(n, std=25).astype(float)
    x[20000:] = late(np.arange(10000) / 1000.0, x[20000:])
    return emg_stream(np.clip(np.rint(x), 0, 4095), batch=100)


def test_hum_that_starts_after_the_20000_sample_cap_is_still_noticed(monkeypatch):
    items = with_hello(_late_change_stream(lambda t, x: x + 800 * np.sin(2 * np.pi * 60 * t)))
    res = run(items, 31, monkeypatch, mains_hz=60)
    assert "mains hum" in names(res.report)


# ------------------------------------------------------------------------------ clipping

def test_clipping_is_counted_when_raw_limits_are_declared(monkeypatch):
    x = emg_noise(4000).astype(float)
    x[::4] = 4095
    res = run(with_hello(emg_stream(x)), 5, monkeypatch)
    assert res.stats["emg"]["clipped_samples"] == 1000
    assert check(res.report, "emg clipping").level == FAIL  # a quarter of the samples railed: saturated, not a warning


def test_clipping_cannot_be_detected_without_raw_limits_but_the_report_says_so(monkeypatch):
    x = emg_noise(4000).astype(float)
    x[::2] = 4095
    res = run([(0.0, hello("b1", raw_limits=None))] + emg_stream(x), 5, monkeypatch)
    assert "clipped_samples" not in res.stats["emg"]  # not measured, so not reported as 0
    c = check(res.report, "emg clipping")
    assert c.level == WARN and "not checkable" in c.detail  # the report says so instead of staying silent
    assert check(res.report, "hardware facts known").level == WARN


def test_clipping_before_a_late_hello_is_still_reported(monkeypatch):
    railed = np.full(3000, 4095.0)
    items = emg_stream(railed, seq0=1) + [(0.0, hello("b1", seq=0 + 61))] + emg_stream(emg_noise(3000), seq0=62, idx0=3000)
    res = run(items, 7, monkeypatch)
    assert res.stats["emg"]["clipped_samples"] >= 3000 or "emg clipping" in names(res.report)


def test_half_the_samples_at_the_rails_is_a_failure(monkeypatch):
    x = emg_noise(4000).astype(float)
    x[::2] = 4095
    res = run(with_hello(emg_stream(x)), 5, monkeypatch)
    assert check(res.report, "emg clipping").level == FAIL


def test_clipping_after_the_20000_sample_cap_is_still_counted(monkeypatch):
    items = with_hello(_late_change_stream(lambda t, x: np.full_like(x, 4095.0)))
    res = run(items, 31, monkeypatch)
    assert res.stats["emg"]["clipped_samples"] > 0


# ------------------------------------------------------------------------------ link utilisation and stall

def test_link_utilisation_counts_the_newline_and_assumes_8n1(monkeypatch):
    s = with_hello(emg_stream(emg_noise(5000)))
    total = sum(len(l) for _, l in s)
    res = run(s, 6, monkeypatch, baud=115200)
    assert res.stats["bytes"] == total  # the newline is inside len(line)
    # utilisation is measured over the ACTIVE window (first byte to last valid frame), 8N1 assumed
    assert res.stats["link_utilisation"] == pytest.approx(res.stats["active_bytes_per_s"] / 11520, rel=0.01)
    assert res.stats["active_bytes_per_s"] >= total / res.stats["seconds"]


def test_a_saturated_link_is_not_diluted_by_a_silent_start(monkeypatch):
    s = with_hello(emg_stream(emg_noise(3000), host_rate=1000.0))  # 3 s of data
    total = sum(len(l) for _, l in s)
    baud = int(total / 3.0 * 10 / 0.9)  # 90% used while data flows
    items = [(2.0, s[0][1])] + s[1:]
    res = run(items, 5, monkeypatch, baud=baud)
    assert check(res.report, "link bandwidth").level == FAIL


def test_stall_is_flagged_when_the_device_goes_quiet_in_a_short_check(monkeypatch):
    s = with_hello(emg_stream(emg_noise(4000)))
    res = run(s, 10, monkeypatch)  # data for 4 s of 10
    assert check(res.report, "data keeps flowing").level == FAIL


def test_a_device_dead_for_minutes_at_the_end_of_a_long_check_is_flagged(monkeypatch):
    s = with_hello(emg_stream(emg_noise(1_050_000), batch=250))  # 1050 s of data, then 150 s of nothing
    res = run(s, 1200, monkeypatch)
    assert res.stats["stalled_for_s"] > 140
    assert check(res.report, "data keeps flowing").level == FAIL


# ------------------------------------------------------------------------------ interrupt, save file, bad save path

def test_ctrl_c_mid_check_closes_the_save_file_and_the_port(monkeypatch, tmp_path):
    s = with_hello(emg_stream(emg_noise(2000)))
    save = tmp_path / "cap.ndjson"
    clock = Clock()
    monkeypatch.setattr(hw, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=lambda s: None))
    made = []

    def factory(p, b):
        made.append(VReplayer(clock, s, interrupt_at_end=True))
        return made[0]
    res = serial_check("x", BAUD, seconds=30, save=save, adapter_factory=factory)  # Ctrl-C is caught: a report follows
    assert res.stats["interrupted"] is True and check(res.report, "check interrupted").level == WARN
    assert made[0].closed
    assert save.read_bytes() == b"".join(l for _, l in s)  # flushed and closed: every received line is on disk


def test_ctrl_c_mid_check_still_produces_a_report(monkeypatch, tmp_path):
    s = with_hello(emg_stream(emg_noise(2000)))
    try:
        res = run(s, 30, monkeypatch, interrupt_at_end=True)
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped serial_check; no report")
    assert check(res.report, "data arrives").level == PASS


def test_unwritable_save_path_does_not_leak_the_open_port(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr(hw, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=lambda s: None))
    made = []

    def factory(p, b):
        made.append(VReplayer(clock, []))
        return made[0]
    res = serial_check("x", BAUD, seconds=2, save=tmp_path / "no" / "such" / "dir" / "cap.ndjson", adapter_factory=factory)
    assert not made, "the save file is opened first, so the port is never opened for a bad path"
    c = check(res.report, "save file")
    assert c.level == FAIL and "cannot write" in c.detail


# ------------------------------------------------------------------------------ null / disconnected samples

def test_an_all_null_emg_stream_fails_the_check(monkeypatch):
    items, seq = [(0.0, hello("b1"))], 1
    for k in range(100):
        items.append((0.05, emg_batch("b1", seq, k * 50, [[None]] * 50, flags=("sensor_disconnected", "missing_values"))))
        seq += 1
    res = run(items, 6, monkeypatch)
    assert res.report.failed
