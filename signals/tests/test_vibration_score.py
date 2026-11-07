"""Prompt 5 tests: vibration features and the EXPERIMENTAL score.

Known-input algorithm checks on synthetic data only. The activation-weighting
tests verify that the declared FORMULA is implemented; they say nothing about
real vibration transmission into the hand, which this prototype does not
measure. No test here makes a standards or dose claim.
"""

import math

import pytest

from vibeguard_signals import contract
from vibeguard_signals.processing import ProcessingConfig, Processor, StreamConfig
from vibeguard_signals.processing.config import CalibrationConfig, ScoreConfig, VibrationConfig
from vibeguard_signals.processing.score import ScoreEngine
from vibeguard_signals.simulator import SIM_COUNTS_PER_G, SimConfig, Simulation

G = 9.80665
SIM_CONV = {a: (G / SIM_COUNTS_PER_G, 0.0) for a in ("ax", "ay", "az")}
SIM_SRC = "simulator synthetic scale 4096 counts/g (test)"


def imu_stream(axes_fn, rate=400.0, duration=4.0, batch=20, boot="t-boot", units="raw_counts",
               conversion=None, channels=("ax", "ay", "az"), seq0=0):
    """IMU-only contract stream; axes_fn(t_s) -> tuple of values per channel (None allowed)."""
    base = {"schema": contract.SCHEMA, "version": 1, "device_id": "t-dev", "boot_id": boot,
            "provenance": {"source": "simulated"}}
    recs = [{**base, "type": "hello", "seq": seq0, "t_us": 1000,
             "firmware": {"name": "test", "version": "0"},
             "streams": {"imu": {"nominal_rate_hz": rate, "channels": list(channels),
                                 "units": {c: units for c in channels}, "raw_limits": None,
                                 "sensor_model": None}}}]
    if conversion:
        recs[0]["streams"]["imu"]["conversion"] = conversion
    dt = 1e6 / rate
    n = int(duration * rate)
    for b, start in enumerate(range(0, n, batch)):
        rows, ts = [], []
        for i in range(start, min(start + batch, n)):
            t = 5000 + i * dt
            ts.append(round(t))
            vals = axes_fn(t / 1e6)
            rows.append([None if v is None else (round(v) if units == "raw_counts" else float(v))
                         for v in vals])
        rec = {**base, "type": "imu_batch", "seq": seq0 + b + 1, "sample_rate_hz": rate,
               "first_sample_index": start, "timing": {"mode": "explicit", "t_us": ts},
               "channels": list(channels), "units": {c: units for c in channels}, "samples": rows,
               "quality": {"flags": ["missing_values"] if any(None in r for r in rows) else []}}
        if conversion:
            rec["conversion"] = conversion
        recs.append(rec)
    for r in recs:
        contract.require_valid(r)
    return recs


def sine(a, f):
    return lambda t: a * math.sin(2 * math.pi * f * t)


def run(records, config=None):
    proc = Processor(config)
    out = []
    for r in records:
        out += proc.feed(r)
    return proc, out


def vib_feats(out):
    return [o["features"]["vibration"] | {"t_us": o["t_us"]} for o in out
            if o["type"] == "stream_features" and o["stream"] == "imu"]


def ok_vib(out, skip=2):
    return [v for v in vib_feats(out) if v["status"] == "ok"][skip:]


def scores(out):
    return [o for o in out if o["type"] == "experimental_score"]


def conv_cfg(**kw):
    return ProcessingConfig(imu=StreamConfig(window_s=0.5, conversion=SIM_CONV, conversion_source=SIM_SRC), **kw)


# -- vibration: analytical inputs ---------------------------------------------------

@pytest.mark.parametrize("f", [20.0, 50.0, 120.0])
@pytest.mark.parametrize("amp", [100.0, 2000.0])
def test_single_axis_sinusoid_rms(f, amp):
    _, out = run(imu_stream(lambda t: (sine(amp, f)(t), 0.0, 4096.0)))
    vs = ok_vib(out)
    assert vs
    for v in vs:
        assert v["dynamic_rms"]["ax"] == pytest.approx(amp / math.sqrt(2), rel=0.03)
        assert v["dynamic_rms"]["ay"] < 0.6 and v["dynamic_rms"]["az"] < 0.6  # rounding noise only
        assert v["combined_rms"] == pytest.approx(amp / math.sqrt(2), rel=0.03)
        assert v["static_mean"]["az"] == pytest.approx(4096.0, abs=1.0)


def test_two_axis_combined_is_root_sum_of_squares():
    _, out = run(imu_stream(lambda t: (sine(300, 40)(t), sine(400, 70)(t), 4096.0)))
    for v in ok_vib(out):
        assert v["combined_rms"] == pytest.approx(math.hypot(300, 400) / math.sqrt(2), rel=0.03)


def test_stationary_gravity_is_not_vibration_after_warmup():
    tilt = (1065.0, -410.0, 3933.0)  # |g| ~ 4096 counts, tilted
    _, out = run(imu_stream(lambda t: tilt))
    vs = vib_feats(out)
    for v in vs:
        if v["status"] == "warming_up":
            assert v["combined_rms"] is None and v["dynamic_rms"] is None
    ok = [v for v in vs if v["status"] == "ok"]
    assert ok and all(v["combined_rms"] < 0.5 for v in ok)
    s = ok[-1]["static_mean"]
    assert math.sqrt(sum(x * x for x in s.values())) == pytest.approx(4096, abs=3)


def test_simulator_stationary_gravity_dynamic_is_noise_only():
    recs = [r.record for r in Simulation(SimConfig(scenario="stationary_gravity", duration_s=4, seed=2),
                                         run_id="vib").records()]
    _, out = run(recs)
    ok = [v for v in vib_feats(out) if v["status"] == "ok"]
    assert ok and all(v["combined_rms"] < 10 for v in ok)  # synthetic noise sigma 3 counts/axis


def test_slow_arm_motion_is_attenuated():
    _, out = run(imu_stream(lambda t: (sine(500, 1.0)(t), 0.0, 4096.0), duration=6.0))
    assert all(v["combined_rms"] < 0.05 * 500 / math.sqrt(2) for v in ok_vib(out, skip=5))


def test_warmup_is_settle_plus_window():
    _, out = run(imu_stream(lambda t: (sine(100, 50)(t), 0.0, 4096.0)))
    first_ok = next(v for v in vib_feats(out) if v["status"] == "ok")
    # samples start at 5 ms; settle 0.25 s + window 1.0 s
    assert (first_ok["t_us"] - 5000) / 1e6 >= 1.25 - 1 / 400 - 1e-9


def test_passband_and_weighting_are_declared():
    _, out = run(imu_stream(lambda t: (0.0, 0.0, 4096.0)))
    v = vib_feats(out)[-1]
    assert v["passband_hz"]["low"] == 5.0 and v["passband_hz"]["high"] is None
    assert "sensor-limited" in v["passband_hz"]["high_note"] and "200 Hz" in v["passband_hz"]["high_note"]
    assert "not ISO 5349" in v["weighting"]


# -- units ------------------------------------------------------------------------

def test_no_conversion_keeps_raw_and_marks_physical_unavailable():
    _, out = run(imu_stream(lambda t: (sine(400, 50)(t), 0.0, 4096.0)))
    v = ok_vib(out)[-1]
    assert v["units"] == {"ax": "raw_counts", "ay": "raw_counts", "az": "raw_counts"}
    assert v["combined_rms"] is not None
    assert v["physical"]["status"] == "unavailable" and v["physical"]["reasons"] == ["no_conversion"]
    assert v["physical"]["combined_rms"] is None


def test_config_conversion_gives_m_s2_with_cited_source():
    _, out = run(imu_stream(lambda t: (sine(2048, 50)(t), 0.0, 4096.0)), conv_cfg())
    v = ok_vib(out)[-1]
    assert v["physical"]["units"] == "m/s^2" and v["physical"]["conversion_source"] == SIM_SRC
    assert v["physical"]["combined_rms"] == pytest.approx(0.5 * G / math.sqrt(2), rel=0.03)


def test_device_declared_m_s2_used_directly():
    conv = {a: {"scale": 1.0, "source": "test fixture"} for a in ("ax", "ay", "az")}
    recs = imu_stream(lambda t: (sine(3.0, 50)(t), 0.0, G), units="m/s^2", conversion=conv)
    _, out = run(recs)
    v = ok_vib(out)[-1]
    assert v["physical"]["conversion_source"] == "device_declared_conversion"
    assert v["physical"]["combined_rms"] == pytest.approx(3.0 / math.sqrt(2), rel=0.03)


def test_conversion_requires_source():
    with pytest.raises(ValueError, match="conversion_source"):
        Processor(ProcessingConfig(imu=StreamConfig(window_s=0.5, conversion=SIM_CONV)))


# -- configuration and invalid data ---------------------------------------------------

@pytest.mark.parametrize("cfg,rate,ok", [
    (VibrationConfig(), 400, True), (VibrationConfig(highpass_hz=200), 400, False),
    (VibrationConfig(lowpass_hz=150), 400, True), (VibrationConfig(lowpass_hz=190), 400, False),
    (VibrationConfig(highpass_hz=5, lowpass_hz=4), 400, False), (VibrationConfig(), 12, True),
    (VibrationConfig(), 10, False),  # 5 Hz high-pass needs > 5 / 0.45 = 11.1 Hz sampling
])
def test_vibration_filter_check(cfg, rate, ok):
    assert (cfg.check(rate) is None) is ok


def test_invalid_vibration_config_is_visible():
    cfg = ProcessingConfig(vibration=VibrationConfig(highpass_hz=190.0))
    _, out = run(imu_stream(lambda t: (0.0, 0.0, 4096.0)), cfg)
    assert all(v["reasons"] == ["vibration_config_invalid"] and v["detail"] for v in vib_feats(out))


def test_gyro_only_stream_has_no_vibration():
    _, out = run(imu_stream(lambda t: (1.0, 2.0, 3.0), channels=("gx", "gy", "gz")))
    v = vib_feats(out)[-1]
    assert v["status"] == "unavailable" and "no accelerometer" in v["detail"]


def test_null_row_resets_without_imputation():
    def fn(t):
        return (None, None, None) if 2.0 <= t < 2.003 else (sine(100, 50)(t), 0.0, 4096.0)
    _, out = run(imu_stream(fn))
    after = [v for v in vib_feats(out) if v["t_us"] > 2.0e6]
    # unavailable if the null rows ended a batch, warming_up if valid rows followed them
    assert after[0]["status"] in ("unavailable", "warming_up")
    assert "missing_values" in after[0]["reasons"] and after[0]["combined_rms"] is None


def test_gap_resets_vibration():
    recs = imu_stream(lambda t: (sine(100, 50)(t), 0.0, 4096.0))
    recs = recs[:70] + recs[72:]
    _, out = run(recs)
    after = [v for v in vib_feats(out) if v["t_us"] >= recs[70]["timing"]["t_us"][0]]
    assert after[0]["status"] == "warming_up" and "gap" in after[0]["reasons"]


def test_clipping_degrades_vibration():
    recs = imu_stream(lambda t: (sine(100, 50)(t), 0.0, 4096.0))
    for r in recs[1:]:
        r["quality"]["flags"] = ["clipped"]
    _, out = run(recs)
    degraded = [v for v in vib_feats(out) if v["status"] == "degraded"]
    assert degraded and all(v["reasons"] == ["clipped"] for v in degraded)
    assert not [v for v in vib_feats(out) if v["status"] == "ok"]


# -- score engine: formula and integration (unit level) ------------------------------

VIB = lambda v, status="ok": {"physical": {"status": status, "combined_rms": v if status in ("ok", "degraded") else None,  # noqa: E731
                                           "reasons": [] if status == "ok" else [status]},
                              "reasons": []}
ACT = lambda a, status="ok": {"status": status, "value": {"emg0": a} if status in ("ok", "degraded") else None,  # noqa: E731
                              "reasons": [] if status == "ok" else ["uncalibrated"]}


def drive(engine, duration_s, step_s, v=2.0, a=0.5, t0=0.0):
    t = t0
    while t <= t0 + duration_s + 1e-9:
        engine.update_vibration(t * 1e6, VIB(v))
        engine.update_activation(t * 1e6, ACT(a))
        t += step_s
    return engine


def test_rate_formula_and_exact_integration():
    e = drive(ScoreEngine(ScoreConfig()), 600.0, 0.05)
    # rate = 1 * (2/1)^2 * (1 + 1 * 0.5) = 6 au/h; 600 s -> 1 au
    assert e.snapshot(None)["rate"]["value"] == pytest.approx(6.0)
    assert e.cumulative == pytest.approx(1.0, rel=1e-9)
    assert e.valid_s == pytest.approx(600.0) and e.excluded_s == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("step", [0.01, 0.05, 0.2])
def test_identical_duration_different_update_rates(step):
    e = drive(ScoreEngine(ScoreConfig()), 60.0, step)
    assert e.cumulative == pytest.approx(6.0 * 60 / 3600, rel=1e-6)
    assert e.valid_s == pytest.approx(60.0, rel=1e-6)


def test_activation_influence_is_the_declared_formula():
    cfg = ScoreConfig(activation_weight=0.8, activation_cap=1.5)
    rates = {a: ScoreEngine(cfg).rate_for(2.0, a) for a in (-0.3, 0.0, 1.0, 1.5, 4.0)}
    base = 4.0
    assert rates[-0.3] == rates[0.0] == pytest.approx(base)         # below relaxed clamps to 0
    assert rates[1.0] == pytest.approx(base * 1.8)                  # 1 + w * a
    assert rates[1.5] == rates[4.0] == pytest.approx(base * 2.2)    # capped at activation_cap


def test_activation_ignored_mode_is_vibration_only_and_declared():
    e = ScoreEngine(ScoreConfig(activation_mode="ignored"))
    for i in range(101):
        e.update_vibration(i * 1e4, VIB(1.0))
    snap = e.snapshot(None)
    assert snap["inputs"]["activation"]["status"] == "ignored"
    assert e.cumulative == pytest.approx(1.0 * 1.0 / 3600, rel=1e-6)


def test_invalid_intervals_are_excluded_with_reasons():
    e = ScoreEngine(ScoreConfig())
    drive(e, 10.0, 0.05)
    good = e.cumulative
    # 5 s of unavailable vibration
    t = 10.05
    while t < 15.0:
        e.update_vibration(t * 1e6, VIB(None, "unavailable"))
        e.update_activation(t * 1e6, ACT(0.5))
        t += 0.05
    assert e.cumulative == good  # including the interval closed by the first invalid update
    assert e.excluded_by_reason["vibration_unavailable"] == pytest.approx(5.0, abs=0.06)
    assert e.snapshot(None)["status"] == "paused"


def test_update_silence_counts_only_the_fresh_part():
    e = ScoreEngine(ScoreConfig())
    drive(e, 1.0, 0.05)
    before = e.valid_s
    e.update_vibration(3.0e6, VIB(2.0))   # 2 s silence on both inputs
    e.update_activation(3.0e6, ACT(0.5))
    assert e.valid_s - before == pytest.approx(0.25)  # an update vouches for at most fresh_s
    assert e.excluded_by_reason["activation_stale & vibration_stale"] == pytest.approx(1.75)


def test_stale_activation_alone_pauses_score():
    e = ScoreEngine(ScoreConfig())
    drive(e, 1.0, 0.05)
    t = 1.05
    while t < 3.0:  # vibration continues, activation silent
        e.update_vibration(t * 1e6, VIB(2.0))
        t += 0.05
    assert e.excluded_by_reason["activation_stale"] == pytest.approx(1.95 - 0.25, abs=0.06)
    assert "activation_stale" in e.snapshot(t * 1e6)["reasons"]


def test_uncalibrated_activation_never_accumulates():
    e = ScoreEngine(ScoreConfig())
    for i in range(200):
        e.update_vibration(i * 5e4, VIB(5.0))
        e.update_activation(i * 5e4, ACT(None, "unavailable"))
    assert e.cumulative == 0.0 and e.valid_s == 0.0


def test_duplicate_and_out_of_order_updates_never_double_count():
    a, b = ScoreEngine(ScoreConfig()), ScoreEngine(ScoreConfig())
    drive(a, 5.0, 0.05)
    t = 0.0
    while t <= 5.0 + 1e-9:
        for _ in range(3):  # repeated
            b.update_vibration(t * 1e6, VIB(2.0))
            b.update_activation(t * 1e6, ACT(0.5))
        b.update_vibration(max(0.0, t - 0.5) * 1e6, VIB(2.0))  # late, older timestamp
        t += 0.05
    assert b.cumulative == pytest.approx(a.cumulative, rel=1e-9)
    assert b.valid_s == pytest.approx(a.valid_s) and b.excluded_s >= 0


def test_boot_change_never_integrates_across_boots():
    e = ScoreEngine(ScoreConfig())
    drive(e, 5.0, 0.05)
    c, v = e.cumulative, e.valid_s
    e.on_boot_change()
    drive(e, 5.0, 0.05, t0=0.0)  # device time restarts at 0
    assert e.boot_segments == 2
    assert e.cumulative == pytest.approx(2 * c) and e.valid_s == pytest.approx(2 * v)


# -- end-to-end through the processor --------------------------------------------------

def sim(scenario, duration=10.0, batch_ms=50.0, seed=1):
    return [r.record for r in Simulation(SimConfig(scenario=scenario, duration_s=duration, seed=seed,
                                                   batch_ms=batch_ms), run_id="score").records()
            if not r.lost]


def test_score_records_identity_units_and_labels():
    _, out = run(sim("vibration", duration=4.0), conv_cfg())
    sc = scores(out)
    assert sc
    for s in sc:
        assert s["schema"] == "vibeguard.features" and s["timebase"] == "device_monotonic_us"
        assert s["provenance"]["source"] == "simulated" and s["device_id"] == "sim-wearable-01"
        assert s["rate"]["units"] == "au/h" and s["cumulative"]["units"] == "au"
        assert s["label"] == "experimental_combined_score"
        assert "not A(8)" in s["label_note"] and "not validated vibration transmission" in s["label_note"]
        assert s["inputs"]["vibration"]["units"] == "m/s^2"


def test_uncalibrated_processor_never_accumulates():
    _, out = run(sim("fault_mix", duration=10.0), conv_cfg())
    assert all(s["cumulative"]["value"] == 0.0 for s in scores(out))
    assert any(r.startswith("activation_") for r in scores(out)[-1]["excluded_by_reason"])


def test_no_conversion_means_no_score():
    _, out = run(sim("vibration", duration=4.0), ProcessingConfig(score=ScoreConfig(activation_mode="ignored")))
    last = scores(out)[-1]
    assert last["cumulative"]["value"] == 0.0
    assert "vibration_no_conversion" in last["excluded_by_reason"]


@pytest.mark.parametrize("batch_ms", [25.0, 100.0])
def test_end_to_end_same_duration_different_batch_sizes(batch_ms):
    cfg = conv_cfg(score=ScoreConfig(activation_mode="ignored"))
    _, ref = run(sim("vibration", duration=8.0, batch_ms=50.0), cfg)
    _, out = run(sim("vibration", duration=8.0, batch_ms=batch_ms), cfg)
    assert scores(out)[-1]["cumulative"]["value"] == pytest.approx(scores(ref)[-1]["cumulative"]["value"], rel=0.03)


def test_fault_intervals_excluded_end_to_end():
    cfg = conv_cfg(score=ScoreConfig(activation_mode="ignored"))
    _, clean = run(sim("happy_path", duration=10.0), cfg)
    _, faulty = run(sim("missing_batches", duration=10.0), cfg)
    c, f = scores(clean)[-1], scores(faulty)[-1]
    assert f["valid_duration_s"] < c["valid_duration_s"] - 1.0  # 1 s loss + re-warm-up
    assert f["excluded_duration_s"] > c["excluded_duration_s"] + 1.0
    assert f["cumulative"]["value"] < c["cumulative"]["value"]


def test_provenance_change_starts_new_accumulation():
    recs = sim("vibration", duration=6.0)
    for r in recs[150:]:
        r["provenance"] = {"source": "replay", "original_source": "simulated", "recording_id": "r1"}
    cfg = conv_cfg(score=ScoreConfig(activation_mode="ignored"))
    _, out = run(recs, cfg)
    resets = [o for o in out if o["type"] == "stream_event" and o["event"] == "score_accumulation_reset"]
    assert len(resets) == 1
    after = [s for s in scores(out) if s["provenance"]["source"] == "replay"]
    assert after[0]["accumulation"] == 2 and after[0]["cumulative"]["value"] == 0.0


def test_activation_weighting_end_to_end_follows_formula():
    """Same synthetic vibration, stronger synthetic activation -> higher rate, by the formula."""
    cfg = conv_cfg(calibration=CalibrationConfig(relaxed_s=1.0, squeeze_s=1.0, discard_s=0.2, min_updates=5))
    recs = sim("happy_path", duration=10.0)
    proc = Processor(cfg)
    out, cmds = [], [(0.3, "relaxed"), (2.1, "squeeze")]
    for r in recs:
        t = r.get("timing", {})
        t = (t.get("t0_us") or (t.get("t_us") or [0])[0]) / 1e6
        while cmds and t >= cmds[0][0]:
            out += proc.calibration_start(cmds.pop(0)[1])
        out += proc.feed(r)
    sc = [s for s in scores(out) if s["status"] == "accumulating"]
    relaxed_vib = [s for s in sc if 4.6e6 < s["t_us"] < 5.9e6]   # vibration, relaxed (activation ~0)
    strong_vib = [s for s in sc if 6.6e6 < s["t_us"] < 7.9e6]    # vibration + activation ~1
    for group, a_expected in ((relaxed_vib, 0.0), (strong_vib, 1.0)):
        assert group
        for s in group:
            v, a = s["inputs"]["vibration"]["value"], s["inputs"]["activation"]["value"]
            assert s["rate"]["value"] == pytest.approx((v ** 2) * (1 + min(2.0, max(0.0, a))), rel=1e-4)
            assert a == pytest.approx(a_expected, abs=0.25)
    mean = lambda g: sum(s["rate"]["value"] for s in g) / len(g)  # noqa: E731
    assert 1.6 < mean(strong_vib) / mean(relaxed_vib) < 2.4


# -- dedicated regressions for the four scoring cases (Prompt 5 fixes) ----------------

@pytest.mark.parametrize("which", ["vibration", "activation"])
def test_case1_unusable_arriving_update_excludes_the_interval_it_closes(which):
    e = drive(ScoreEngine(ScoreConfig()), 1.0, 0.05)  # last valid updates at t = 1.0 s
    valid, cum = e.valid_s, e.cumulative
    # the other input reports the same interval as usable; arrival order is covered
    # exhaustively in test_review2_fixes.py
    if which == "vibration":
        e.update_vibration(1.05e6, VIB(None, "unavailable"))
        e.update_activation(1.05e6, ACT(0.5))
    else:
        e.update_activation(1.05e6, ACT(None, "unavailable"))
        e.update_vibration(1.05e6, VIB(2.0))
    assert e.valid_s == valid and e.cumulative == cum
    reason = "vibration_unavailable" if which == "vibration" else "activation_uncalibrated"
    assert e.excluded_by_reason[reason] == pytest.approx(0.05)


def test_case2_older_update_never_replaces_newer_held_input():
    e = drive(ScoreEngine(ScoreConfig()), 1.0, 0.05)  # holds vibration 2.0 m/s^2 at t = 1.0 s
    e.update_vibration(0.5e6, VIB(9.0))               # late, older, different value
    held = e.snapshot(1.0e6)["inputs"]["vibration"]
    assert held["value"] == 2.0 and held["t_us"] == 1_000_000 and held["age_s"] == 0.0
    before = e.cumulative
    e.update_vibration(1.05e6, VIB(2.0))
    e.update_activation(1.05e6, ACT(0.5))
    assert e.cumulative - before == pytest.approx(6.0 * 0.05 / 3600)  # held 2.0 was used, not 9.0


def test_case3_duplicate_updates_integrate_once():
    once, thrice = ScoreEngine(ScoreConfig()), ScoreEngine(ScoreConfig())
    for i in range(101):
        t = i * 5e4
        once.update_vibration(t, VIB(2.0)); once.update_activation(t, ACT(0.5))
        for _ in range(3):
            thrice.update_vibration(t, VIB(2.0)); thrice.update_activation(t, ACT(0.5))
    assert thrice.cumulative == once.cumulative and thrice.valid_s == once.valid_s
    assert thrice.excluded_s == once.excluded_s


def test_case4_out_of_order_updates_never_double_count_or_go_negative():
    ordered, shuffled = ScoreEngine(ScoreConfig()), ScoreEngine(ScoreConfig())
    times = [i * 5e4 for i in range(101)]
    for t in times:
        ordered.update_vibration(t, VIB(2.0)); ordered.update_activation(t, ACT(0.5))
    for i, t in enumerate(times):  # every update followed by a replay of an older one
        shuffled.update_vibration(t, VIB(2.0)); shuffled.update_activation(t, ACT(0.5))
        old = times[max(0, i - 10)]  # 0.5 s old: beyond the freshness limit if it were held
        shuffled.update_activation(old, ACT(0.5)); shuffled.update_vibration(old, VIB(2.0))
    assert shuffled.cumulative == pytest.approx(ordered.cumulative, rel=1e-12)
    assert shuffled.valid_s == pytest.approx(ordered.valid_s)
    assert shuffled.excluded_s >= 0 and shuffled.valid_s <= (times[-1] - times[0]) / 1e6 + 1e-9
