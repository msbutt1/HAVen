"""Tool-use detection: tool vibration vs normal arm use, tool-time tracking, warnings."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from vibeguard_signals.demo import demo_processing_config
from vibeguard_signals.processing import Processor
from vibeguard_signals.processing.config import ToolUseConfig
from vibeguard_signals.processing.tool_use import ToolUseDetector
from vibeguard_signals.simulator import SimConfig, Simulation

RATE = 500.0
G = 9.80665
UNITS = {"ax": "m/s^2", "ay": "m/s^2", "az": "m/s^2"}


def detector(**kw) -> ToolUseDetector:
    d = ToolUseDetector(ToolUseConfig(**kw), None, None)
    d.configure(RATE, ["ax", "ay", "az"], UNITS)
    return d


def run(d: ToolUseDetector, signal, seconds: float, t0: float = 0.0, batch: int = 25):
    """Feed signal(t) -> (ax, ay, az) in m/s^2 (gravity added); return per-batch records."""
    recs = []
    n = int(seconds * RATE)
    for start in range(0, n, batch):
        rows = []
        for i in range(start, min(n, start + batch)):
            t = t0 + i / RATE
            ax, ay, az = signal(t)
            rows.append([ax, ay, az + G])
        d.process(rows)
        recs.append(d.update((t0 + (start + len(rows)) / RATE) * 1e6))
    return recs


def tone(hz, amp_g, axis=0):
    def f(t):
        v = [0.0, 0.0, 0.0]
        v[axis] = amp_g * G * math.sin(2 * math.pi * hz * t)
        return tuple(v)
    return f


def quiet(t):
    return (0.0, 0.0, 0.0)


def test_sustained_tool_vibration_detected_after_on_hold():
    d = detector()
    recs = run(d, tone(100.0, 1.0), 6.0)
    first_tool = next(i for i, r in enumerate(recs) if r["state"] == "tool")
    t_on = (first_tool + 1) * 25 / RATE
    assert 2.5 <= t_on <= 3.3          # 1 s filter/window settling + 2 s on-hold
    assert recs[-1]["dominant_hz"] == pytest.approx(100.0, abs=2.0)
    assert recs[-1]["tool_band_rms_ms2"] == pytest.approx(G / math.sqrt(2), rel=0.05)
    assert recs[-1]["session_tool_s"] == pytest.approx(5.0, abs=0.2)   # back-filled on-hold


def test_knocks_at_rest_never_count_as_tool():
    def knocks(t):
        tk = t % 2.0
        az = 3.0 * G * math.sin(2 * math.pi * 80 * tk) * math.exp(-tk / 0.015) if tk < 0.05 else 0.0
        return (0.0, 0.0, az)
    recs = run(detector(), knocks, 30.0)
    assert all(r["state"] != "tool" for r in recs)
    assert recs[-1]["session_tool_s"] == 0.0


def test_vigorous_low_frequency_shaking_is_arm_use():
    def shake(t):
        return (2.0 * G * math.sin(2 * math.pi * 8.0 * t), 1.0 * G * math.sin(2 * math.pi * 3.0 * t), 0.0)
    recs = run(detector(), shake, 20.0)
    assert all(r["state"] != "tool" for r in recs)
    ok = [r for r in recs if r["status"] == "ok"]
    assert max(r["motion_band_rms_ms2"] for r in ok) > 5 * max(r["tool_band_rms_ms2"] for r in ok)


def test_short_pause_keeps_one_bout_long_pause_splits():
    d = detector()
    run(d, tone(120.0, 0.8), 10.0)
    run(d, quiet, 1.5, t0=10.0)                  # < off_hold: same bout
    recs = run(d, tone(120.0, 0.8), 10.0, t0=11.5)
    assert recs[-1]["bouts"] == 1 and recs[-1]["state"] == "tool"
    run(d, quiet, 8.0, t0=21.5)                  # > off_hold: bout ends
    recs = run(d, tone(120.0, 0.8), 6.0, t0=29.5)
    assert recs[-1]["bouts"] == 2
    # pauses are not tool time
    assert recs[-1]["session_tool_s"] < 10 + 10 + 6 + 0.5


def test_warnings_continuous_and_session():
    d = detector(continuous_warn_s=5.0, session_warn_s=10.0)
    recs = run(d, tone(100.0, 1.0), 12.0)
    assert "continuous_tool_use_long" in recs[-1]["warnings"]
    assert "session_tool_time_high" in recs[-1]["warnings"]
    notice = [r for r in recs if "session_tool_time_approaching" in r["warnings"]]
    assert notice                                # early notice at 80 % came first
    run(d, quiet, 8.0, t0=12.0)
    r = d.update(None)
    assert "continuous_tool_use_long" not in r["warnings"]   # bout ended
    assert "session_tool_time_high" in r["warnings"]         # session total persists


def test_no_conversion_means_unavailable_never_guessed():
    d = ToolUseDetector(ToolUseConfig(), None, None)
    d.configure(RATE, ["ax", "ay", "az"], {"ax": "raw_counts", "ay": "raw_counts", "az": "raw_counts"})
    d.process([[0, 0, 4096]] * 600)
    r = d.update(1.2e6)
    assert r["status"] == "unavailable" and r["state"] == "unknown"
    assert r["reasons"] == ["tool_use_config_invalid"]


def test_unobserved_gap_is_not_tool_time_and_session_survives_reboot():
    d = detector()
    run(d, tone(100.0, 1.0), 6.0)
    before = d.session_tool_s
    d.update(60e6)                                # 54 s jump with no data: unobserved
    d.reset("gap")
    assert d.session_tool_s == before
    d.on_boot_change()                            # device time restarts
    recs = run(d, tone(100.0, 1.0), 6.0, t0=0.0)
    assert recs[-1]["session_tool_s"] > before    # same session continues
    d.reset_session()
    assert d.update(None)["session_tool_s"] == 0.0


def test_simulator_tool_vs_arm_scenario():
    sim = Simulation(SimConfig(scenario="tool_vs_arm", duration_s=60.0, seed=1), run_id="tool-test")
    proc = Processor(demo_processing_config())
    out = []
    for r in sim.records():
        if not r.lost:
            out += proc.feed(r.record)
    tu = [o for o in out if o["type"] == "tool_use"]
    # one tool_use record per IMU feature record
    assert len(tu) == sum(1 for o in out if o["type"] == "stream_features" and o["stream"] == "imu")

    def state_at(s):
        return min(tu, key=lambda o: abs(o["t_us"] - s * 1e6))["state"]
    for s in (3, 8, 12, 16, 40, 43, 59):          # rest and arm-use phases
        assert state_at(s) == "no_tool", s
    for s in (25, 33, 50, 53):                    # tool phases (after on-hold)
        assert state_at(s) == "tool", s
    last = tu[-1]
    assert last["bouts"] == 2
    assert last["session_tool_s"] == pytest.approx(27.0, abs=1.5)   # truth: 18 s + 9 s
    doms = {round(o["dominant_hz"]) for o in tu if o["state"] == "tool" and o["dominant_hz"]}
    assert any(abs(h - 120) <= 2 for h in doms) and any(abs(h - 90) <= 2 for h in doms)


def test_processor_new_shift_resets_session():
    sim = Simulation(SimConfig(scenario="tool_vs_arm", duration_s=60.0, seed=1), run_id="tool-test")
    proc = Processor(demo_processing_config())
    for r in sim.records():
        if not r.lost:
            proc.feed(r.record)
    ev = proc.tool_use_reset_session()
    assert ev[0]["event"] == "tool_session_reset" and ev[0]["detail"]["bouts"] == 2
    assert proc.tool_use.session_tool_s == 0.0


def test_config_validation():
    with pytest.raises(ValueError):
        ToolUseConfig(off_threshold_ms2=2.0, on_threshold_ms2=1.0).validate()
    with pytest.raises(ValueError):
        ToolUseConfig(motion_band_hz=(0.5, 30.0)).validate()
    assert ToolUseConfig(tool_band_low_hz=300.0).check(500.0) is not None


def test_vigorous_shaking_after_tool_ends_bout_and_is_not_tool_time():
    """10 Hz, 3 g shaking leaks above the 'still active' tool-band level, but the
    tool-vs-motion ratio identifies it as arm use: the bout ends, no tool time."""
    d = detector()
    run(d, tone(100.0, 1.0), 8.0)
    tool_time = d.session_tool_s

    def shake(t):
        return (3.0 * G * math.sin(2 * math.pi * 10.0 * t), 0.0, 0.0)
    recs = run(d, shake, 12.0, t0=8.0)
    leaked = [r for r in recs[40:] if r["status"] == "ok"]
    assert min(r["tool_band_rms_ms2"] for r in leaked) > 0.6          # above off threshold
    assert recs[-1]["state"] == "no_tool"
    assert recs[-1]["session_tool_s"] < tool_time + 3.5                 # at most the off-hold tail


# -- steadiness and per-wearer tool calibration (simulated signals) ------------

def handshake(seed=1):
    """Simulated handshake/waving: 2-3 Hz pumping with irregular jolts (broadband)."""
    rng = np.random.default_rng(seed)
    jolts = np.cumsum(rng.uniform(0.15, 0.6, 200))        # irregular jolt times
    amps = rng.uniform(1.0, 3.0, 200)

    def f(t):
        x = 0.8 * G * math.sin(2 * math.pi * 2.5 * t) + 0.4 * G * math.sin(2 * math.pi * 0.9 * t)
        k = np.searchsorted(jolts, t)
        if k > 0 and t - jolts[k - 1] < 0.02:              # 20 ms knock, decaying
            dt = t - jolts[k - 1]
            x += amps[k - 1] * G * math.exp(-dt / 0.005) * math.sin(2 * math.pi * 70 * dt)
        return (x, 0.5 * x, 0.2 * G * math.sin(2 * math.pi * 1.7 * t))
    return f


def massage_gun(hz=40.0, amp_g=1.5):
    """Simulated percussive massage gun held by the handle: pulse train (harmonics)
    at a constant rate, with slow arm movement on top."""
    def f(t):
        ph = (t * hz) % 1.0
        pulse = amp_g * G * (math.exp(-ph / 0.08) - 0.12)
        arm = 0.3 * G * math.sin(2 * math.pi * 0.7 * t)
        return (pulse + arm, 0.4 * pulse, 0.1 * pulse)
    return f


def wandering(t):
    """Vibration whose frequency jumps every 0.25 s (irregular, not a tool)."""
    hz = [30, 75, 140, 55, 110, 40, 180, 90][int(t / 0.25) % 8]
    return (1.0 * G * math.sin(2 * math.pi * hz * t), 0.0, 0.0)


def calibrate(d, movement=handshake(), tool=massage_gun()):
    t = 0.0
    for phase, sig in (("still", quiet), ("movement", movement), ("tool", tool)):
        d.calibration_start(phase)
        for _ in range(100):                               # until this capture ends
            run(d, sig, 0.5, t0=t)
            t += 0.5
            if not d.cal_state.startswith("capturing"):
                break
    return t


def test_wandering_frequency_is_not_a_tool():
    d = detector()
    recs = run(d, wandering, 10.0)
    assert all(r["state"] != "tool" for r in recs)
    assert any(r["candidate"] for r in recs)          # loud enough, but not steady


def test_steady_tool_is_steady():
    d = detector()
    recs = run(d, tone(100.0, 1.0), 6.0)
    first = next(r for r in recs if r["state"] == "tool")
    assert first["steadiness"] == 1.0


def test_tool_calibration_separates_handshake_from_massage_gun():
    d = detector()
    t = calibrate(d)
    ev = [e for e in d.events if e[0] == "tool_calibration_state"]
    assert ev[-1][1]["state"] == "calibrated", ev[-1]
    assert d.cfg.on_threshold_ms2 != ToolUseConfig().on_threshold_ms2
    d.reset_session()
    recs = run(d, handshake(seed=7), 20.0, t0=t)          # a NEW handshake: never a tool
    assert all(r["state"] != "tool" for r in recs)
    recs = run(d, massage_gun(hz=45.0), 8.0, t0=t + 20)   # the gun: detected
    assert recs[-1]["state"] == "tool"
    assert recs[-1]["thresholds"]["source"] == "calibrated"


def test_tool_calibration_fails_when_movement_looks_like_tool():
    d = detector()
    calibrate(d, movement=massage_gun(), tool=massage_gun())
    last = [e for e in d.events if e[0] == "tool_calibration_state"][-1][1]
    assert last["state"] == "failed"
    assert last["reason"] in ("movement_looks_like_tool", "tool_not_reliably_detected")
    assert d.cfg == ToolUseConfig()                        # defaults kept


def test_tool_capture_requires_movement_first_and_reset_restores_defaults():
    d = detector()
    d.calibration_start("tool")
    assert d.events[-1][0] == "tool_calibration_rejected"
    calibrate(d)
    d.calibration_reset()
    assert d.cfg == ToolUseConfig() and d.cal_state == "uncalibrated"


def test_processor_emits_tool_calibration_events():
    p = Processor(demo_processing_config())
    outs = p.tool_calibration_start("still")
    assert outs[0]["event"] == "tool_calibration_state"
    assert outs[0]["detail"]["state"] == "capturing_still"
