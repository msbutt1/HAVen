"""Prompt 4 tests: causal EMG envelope and relaxed/squeeze calibration.

These verify ALGORITHM behaviour on synthetic signals only (known sinusoids,
DC offsets, simulator scenarios). Electrode quality, skin contact, real noise,
and vibration artefacts on real EMG remain hardware checks.
"""

import copy
import math

import pytest

from haven_signals import contract
from haven_signals.processing import ProcessingConfig, Processor
from haven_signals.processing.config import (
    CalibrationConfig, EmgFilterConfig, EmgProcessingConfig, StreamConfig,
)
from haven_signals.simulator import SimConfig, Simulation

FAST_CAL = CalibrationConfig(relaxed_s=1.0, squeeze_s=1.0, discard_s=0.2, min_updates=5)


def synth(amplitude, rate=1000.0, duration=2.0, f0=100.0, offset=2048.0, batch=50,
          device="t-dev", boot="t-boot", limits=(0, 4095), source="simulated"):
    """EMG-only contract stream: offset + amplitude(t) * sin(2 pi f0 t), rounded to counts."""
    amp = amplitude if callable(amplitude) else (lambda t, a=amplitude: a)
    base = {"schema": contract.SCHEMA, "version": 1, "device_id": device, "boot_id": boot,
            "provenance": {"source": source}}
    recs = [{**base, "type": "hello", "seq": 0, "t_us": 1000,
             "firmware": {"name": "test", "version": "0"},
             "streams": {"emg": {"nominal_rate_hz": rate, "channels": ["emg0"],
                                 "units": {"emg0": "adc_counts"},
                                 "raw_limits": {"min": limits[0], "max": limits[1]},
                                 "sensor_model": None}}}]
    dt = 1e6 / rate
    n_total = int(duration * rate)
    for b, start in enumerate(range(0, n_total, batch)):
        rows = []
        for i in range(start, min(start + batch, n_total)):
            t = (5000 + i * dt) / 1e6
            v = offset + amp(t) * math.sin(2 * math.pi * f0 * t)
            rows.append([min(limits[1], max(limits[0], round(v)))])
        recs.append({**base, "type": "emg_batch", "seq": b + 1, "sample_rate_hz": rate,
                     "first_sample_index": start,
                     "timing": {"mode": "uniform", "t0_us": round(5000 + start * dt), "interval_us": dt},
                     "channels": ["emg0"], "units": {"emg0": "adc_counts"}, "samples": rows,
                     "quality": {"flags": ["clipped"] if any(r[0] in limits for r in rows) else []}})
    for r in recs:
        contract.require_valid(r)
    return recs


def batch_t(rec):
    t = rec["timing"]
    return (t["t0_us"] if t["mode"] == "uniform" else t["t_us"][0]) / 1e6


def run(records, config=None, commands=None):
    """Feed records; commands = {device_time_s: phase} issued before the first batch at/after it."""
    proc = Processor(config)
    pending = sorted((commands or {}).items())
    out = []
    for r in records:
        while pending and "timing" in r and batch_t(r) >= pending[0][0]:
            out += proc.calibration_start(pending.pop(0)[1])
        out += proc.feed(r)
    return proc, out


def emg_feats(out):
    return [o for o in out if o["type"] == "stream_features" and o["stream"] == "emg"]


def act(f):
    return f["features"]["emg_activation"]


def env_value(f):
    v = act(f)["envelope"]["value"]
    return None if v is None else v["emg0"]


def events(out, kind):
    return [o for o in out if o["type"] == "stream_event" and o["event"] == kind]


def cal_states(out):
    return [e["detail"]["state"] for e in events(out, "calibration_state")]


# -- envelope: known synthetic amplitudes -------------------------------------

@pytest.mark.parametrize("f0", [60.0, 100.0, 300.0])
@pytest.mark.parametrize("amplitude", [20.0, 200.0])
def test_in_band_sinusoid_envelope_is_amplitude_over_root2(f0, amplitude):
    _, out = run(synth(amplitude, f0=f0))
    ok = [env_value(f) for f in emg_feats(out) if act(f)["envelope"]["status"] == "ok"][5:]
    assert ok
    expected = amplitude / math.sqrt(2)
    assert all(abs(v - expected) < 0.04 * expected + 0.5 for v in ok)  # 0.5 count quantisation


def test_out_of_band_low_frequency_is_attenuated():
    _, out = run(synth(200.0, f0=4.0, duration=3.0))
    ok = [env_value(f) for f in emg_feats(out) if act(f)["envelope"]["status"] == "ok"][10:]
    assert max(ok) < 0.1 * 200 / math.sqrt(2)


def test_pure_dc_offset_gives_near_zero_envelope():
    _, out = run(synth(0.0, offset=2048.0))
    ok = [env_value(f) for f in emg_feats(out) if act(f)["envelope"]["status"] == "ok"]
    assert ok and max(ok) < 0.5


@pytest.mark.parametrize("mode,fixed", [("ema", None), ("fixed", 3000.0)])
def test_envelope_independent_of_dc_offset(mode, fixed):
    cfg = ProcessingConfig(emg_processing=EmgProcessingConfig(offset_mode=mode, fixed_offset=fixed))
    _, a = run(synth(100.0, offset=3000.0), cfg)
    _, b = run(synth(100.0, offset=3000.0, limits=(0, 4095)),
               ProcessingConfig(emg_processing=EmgProcessingConfig(offset_mode="fixed", fixed_offset=3000.0)))
    va = [env_value(f) for f in emg_feats(a) if act(f)["envelope"]["status"] == "ok"][5:]
    vb = [env_value(f) for f in emg_feats(b) if act(f)["envelope"]["status"] == "ok"][5:]
    assert va and all(abs(x - y) < 0.02 * y for x, y in zip(va, vb))


def test_startup_warms_up_for_settle_plus_window_with_null_values():
    _, out = run(synth(100.0))
    fs = emg_feats(out)
    first_ok = next(i for i, f in enumerate(fs) if act(f)["envelope"]["status"] == "ok")
    # 50-sample batches at 1 kHz: 0.1 s settle + 0.1 s window = 200 samples = 4 batches
    assert first_ok == 3
    for f in fs[:first_ok]:
        assert act(f)["envelope"]["status"] == "warming_up"
        assert act(f)["envelope"]["value"] is None


def test_envelope_units_and_label():
    _, out = run(synth(50.0))
    a = act(emg_feats(out)[-1])
    assert a["envelope"]["units"] == {"emg0": "adc_counts_rms"}
    assert a["label"] == "relative_muscle_activation_proxy"
    assert "not force" in a["label_note"] and "%MVC" in a["label_note"]


# -- filter configuration vs Nyquist ------------------------------------------

def test_default_band_rejected_at_500hz_not_clamped():
    _, out = run(synth(50.0, rate=500.0))
    for f in emg_feats(out):
        env = act(f)["envelope"]
        assert env["status"] == "unavailable" and env["reasons"] == ["filter_config_invalid"]
        assert "exceeds" in env["detail"]


def test_explicit_band_valid_at_500hz():
    cfg = ProcessingConfig(emg_processing=EmgProcessingConfig(
        bandpass=EmgFilterConfig(low_hz=20.0, high_hz=200.0)))
    _, out = run(synth(50.0, rate=500.0), cfg)
    ok = [env_value(f) for f in emg_feats(out) if act(f)["envelope"]["status"] == "ok"][5:]
    assert ok and all(abs(v - 50 / math.sqrt(2)) < 2.5 for v in ok)


@pytest.mark.parametrize("bp,rate,ok", [
    (EmgFilterConfig(20, 450), 1000, True), (EmgFilterConfig(20, 450), 999, False),
    (EmgFilterConfig(20, 450), 500, False), (EmgFilterConfig(200, 100), 1000, False),
    (EmgFilterConfig(0, 100), 1000, False), (EmgFilterConfig(20, 100, order=5), 1000, False),
    (EmgFilterConfig(10, 90), 200, True),
])
def test_filter_check_against_nyquist_headroom(bp, rate, ok):
    assert (bp.check(rate) is None) is ok


@pytest.mark.parametrize("bad", [
    dict(offset_mode="median"), dict(offset_mode="fixed"), dict(envelope_window_s=0.001),
    dict(settle_s=-1),
])
def test_invalid_emg_processing_config_rejected(bad):
    with pytest.raises(ValueError):
        Processor(ProcessingConfig(emg_processing=EmgProcessingConfig(**bad)))


# -- invalidation: gaps, nulls, clipping, reboot, mismatch, stale, reset --------

def sim(scenario, duration=6.0, **kw):
    return [r.record for r in Simulation(SimConfig(scenario=scenario, duration_s=duration, seed=4, **kw),
                                         run_id="emg-test").records() if not r.lost]


def test_gap_resets_filter_and_rewarms():
    _, out = run(sim("missing_batches", duration=6.0))
    gap = next(e for e in events(out, "sample_gap") if e["stream"] == "emg")
    after = [f for f in emg_feats(out) if f["t_us"] >= gap["t_us"]]
    assert act(after[0])["envelope"]["status"] == "warming_up"
    assert "gap" in act(after[0])["envelope"]["reasons"]
    first_ok = next(f for f in after if act(f)["envelope"]["status"] == "ok")
    assert first_ok["t_us"] - gap["t_us"] >= 0.15e6  # settle + window minus one batch


def test_null_sample_resets_without_imputation():
    recs = synth(100.0)
    recs[10]["samples"][25] = [None]
    recs[10]["quality"]["flags"] = ["missing_values"]
    _, out = run(recs)
    f = emg_feats(out)[9]  # the batch containing the null (index shift: hello has no features)
    assert act(f)["envelope"]["status"] == "warming_up"
    assert "missing_values" in act(f)["envelope"]["reasons"]


def test_clipping_degrades_envelope():
    _, out = run(sim("clipping", duration=6.0))
    reasons = [act(f)["envelope"]["reasons"] for f in emg_feats(out) if act(f)["envelope"]["status"] == "degraded"]
    assert reasons and all(r == ["clipped"] for r in reasons)


def test_device_clipping_flag_degrades_envelope_without_host_limits():
    recs = synth(100.0)
    for r in recs[20:]:
        if r["type"] == "emg_batch":
            r["quality"]["flags"] = ["clipped"]
    cfg = ProcessingConfig(emg=StreamConfig(window_s=0.1, use_device_raw_limits=False))
    _, out = run(recs, cfg)
    assert act(emg_feats(out)[-1])["envelope"]["status"] == "degraded"


def test_rate_mismatch_makes_envelope_unavailable():
    cfg = ProcessingConfig(emg=StreamConfig(window_s=0.1, expected_rate_hz=800.0))
    _, out = run(synth(100.0), cfg)
    assert all(act(f)["envelope"]["status"] == "unavailable" for f in emg_feats(out))


def test_disconnect_and_stale_make_envelope_unavailable():
    _, out = run(sim("invalid_samples", duration=10.0))
    fs = emg_feats(out)
    disc = [f for f in fs if f["status"] == "unavailable" and (
        "sensor_disconnected" in f["reasons"] or "device_reports_disconnected" in f["reasons"])]
    assert disc and all(act(f)["envelope"]["value"] is None for f in disc)
    assert all(act(f)["envelope"]["status"] == "unavailable" for f in disc)


def test_reset_state_is_fully_cleared():
    """After a gap, the envelope equals a fresh pipeline fed only the post-gap data."""
    recs = synth(lambda t: 50 + 100 * t, duration=3.0)
    cut = 30
    gapped = recs[:cut] + recs[cut + 2:]  # lose two batches
    _, out_gap = run(gapped)
    tail = copy.deepcopy(recs[cut + 2:])
    _, out_fresh = run([recs[0]] + tail)
    a = [env_value(f) for f in emg_feats(out_gap) if f["t_us"] >= tail[0]["timing"]["t0_us"]]
    b = [env_value(f) for f in emg_feats(out_fresh)]
    assert a == b and any(v is not None for v in a)


def test_identical_input_gives_identical_output():
    recs = sim("vibration_emg_mixed", duration=5.0)
    assert run(recs)[1] == run(recs)[1]


# -- calibration --------------------------------------------------------------

def stepped(levels, seg_s=1.6):
    """Amplitude schedule: levels[i] for seg_s seconds each."""
    return lambda t: levels[min(int(t / seg_s), len(levels) - 1)]


def calibrated_run(levels=(10.0, 100.0, 10.0, 200.0, 0.0), extra_cfg=None, **synth_kw):
    cfg = ProcessingConfig(calibration=FAST_CAL, **(extra_cfg or {}))
    recs = synth(stepped(levels), duration=1.6 * len(levels), **synth_kw)
    return run(recs, cfg, {0.25: "relaxed", 1.65: "squeeze"})


def test_calibration_success_references_and_normalisation():
    proc, out = calibrated_run()
    assert cal_states(out) == ["capturing_relaxed", "relaxed_captured", "capturing_squeeze", "calibrated"]
    ref = proc.calibration_record()["reference"]
    assert ref["relaxed"]["emg0"] == pytest.approx(10 / math.sqrt(2), abs=0.6)
    assert ref["squeeze"]["emg0"] == pytest.approx(100 / math.sqrt(2), abs=3)
    norm = {round(f["t_us"] / 1e6, 2): act(f)["normalized"] for f in emg_feats(out)
            if act(f)["normalized"]["status"] == "ok"}
    relaxed_again = [n["value"]["emg0"] for t, n in norm.items() if 3.6 < t < 4.7]
    above = [n for t, n in norm.items() if 5.2 < t < 6.3]
    below = [n for t, n in norm.items() if t > 6.8]
    assert all(abs(v) < 0.05 for v in relaxed_again)
    # activation above the squeeze reference is preserved, not hidden
    assert all(n["value"]["emg0"] > 1.9 and n["display"]["emg0"] == 1.0 and n["above_reference"]["emg0"]
               for n in above)
    assert all(n["value"]["emg0"] < 0 and n["display"]["emg0"] == 0.0 and n["below_relaxed"]["emg0"]
               for n in below)


def test_calibration_is_reproducible():
    a, b = calibrated_run()[0].calibration_record(), calibrated_run()[0].calibration_record()
    assert a == b and a["calibration_id"].startswith("cal-")
    assert a["label"] == "relative_muscle_activation_proxy"
    assert a["pipeline"]["rate_hz"] == 1000.0 and a["provenance"] == {"source": "simulated"}


def test_insufficient_separation_fails_visibly_without_dividing():
    proc, out = calibrated_run(levels=(10.0, 12.0, 12.0))
    assert cal_states(out)[-1] == "failed"
    fail = events(out, "calibration_state")[-1]["detail"]
    assert fail["reason"] == "insufficient_separation" and "emg0" in fail["channels"]
    assert proc.calibration_record() is None
    last = act(emg_feats(out)[-1])["normalized"]
    assert last["status"] == "unavailable" and last["value"] is None
    assert last["reasons"] == ["failed", "insufficient_separation"]


def test_near_zero_relaxed_and_squeeze_rejected():
    _, out = calibrated_run(levels=(0.0, 0.0, 0.0))
    assert events(out, "calibration_state")[-1]["detail"]["reason"] == "insufficient_separation"


def test_insufficient_data_rejected():
    cfg = ProcessingConfig(calibration=CalibrationConfig(relaxed_s=1.0, squeeze_s=1.0,
                                                         discard_s=0.5, min_updates=20))
    _, out = run(synth(10.0, duration=3.0), cfg, {0.25: "relaxed"})
    assert events(out, "calibration_state")[-1]["detail"]["reason"] == "insufficient_data"


def test_capture_interrupted_by_gap():
    recs = synth(10.0, duration=3.0)
    recs = recs[:12] + recs[14:]  # gap during the relaxed capture
    _, out = run(recs, ProcessingConfig(calibration=FAST_CAL), {0.25: "relaxed"})
    last = events(out, "calibration_state")[-1]["detail"]
    assert last["state"] == "failed" and last["reason"] == "interrupted_gap"


def test_capture_rejects_clipping():
    recs = synth(stepped((10.0, 3000.0, 10.0)), duration=4.8)  # squeeze overdrives the ADC
    _, out = run(recs, ProcessingConfig(calibration=FAST_CAL), {0.25: "relaxed", 1.65: "squeeze"})
    last = events(out, "calibration_state")[-1]["detail"]
    assert last["state"] == "failed" and last["reason"] == "clipped"
    assert last["clipped_fraction"] > 0 and last["allowed"] == 0.0


def test_capture_waits_for_warmup_then_starts():
    # issued after the first batch configured EMG, while the envelope is still warming up
    _, out = run(synth(10.0, duration=2.0), ProcessingConfig(calibration=FAST_CAL), {0.06: "relaxed"})
    states = events(out, "calibration_state")
    assert [s["detail"]["state"] for s in states] == ["capturing_relaxed", "relaxed_captured"]
    assert states[1]["detail"]["capture"]["t_start_us"] >= 0.2e6  # after envelope warm-up


def test_squeeze_without_relaxed_rejected():
    proc = Processor(ProcessingConfig(calibration=FAST_CAL))
    for r in synth(10.0, duration=0.5):
        proc.feed(r)
    out = proc.calibration_start("squeeze")
    assert out[0]["event"] == "calibration_rejected"
    assert out[0]["detail"]["reason"] == "relaxed_reference_missing"


def test_start_before_emg_configured_rejected():
    out = Processor().calibration_start("relaxed")
    assert out[0]["detail"]["reason"] == "emg_not_configured"


def test_cancel_and_recalibration_clear_state():
    proc, out = calibrated_run()
    cid = proc.calibration_record()["calibration_id"]
    out = proc.calibration_start("relaxed")
    assert [o["event"] for o in out] == ["calibration_cleared", "calibration_state"]
    assert out[0]["detail"]["calibration_id"] == cid and proc.calibration_record() is None
    out = proc.calibration_cancel()
    assert out[-1]["detail"] == {"state": "failed", "reason": "cancelled", "phase": "relaxed"}


# -- calibration invalidation / compatibility ---------------------------------

def test_reboot_invalidates_calibration_by_default():
    proc, out = calibrated_run()
    more = synth(50.0, duration=1.0, boot="t-boot-2")
    out2 = []
    for r in more:
        out2 += proc.feed(r)
    st = [e["detail"] for e in events(out2, "calibration_state")]
    assert st and st[0]["state"] == "invalidated" and st[0]["reason"] == "boot_change"
    assert all(act(f)["normalized"]["value"] is None for f in emg_feats(out2))


def test_reboot_survival_is_opt_in():
    proc, _ = calibrated_run(extra_cfg=None)
    cfg = ProcessingConfig(calibration=CalibrationConfig(**{**FAST_CAL.__dict__, "survives_reboot": True}))
    p2, _ = run(synth(stepped((10.0, 100.0, 10.0)), duration=4.8), cfg, {0.25: "relaxed", 1.65: "squeeze"})
    out = []
    for r in synth(70.0, duration=1.0, boot="t-boot-2"):
        out += p2.feed(r)
    ok = [act(f)["normalized"] for f in emg_feats(out) if act(f)["normalized"]["status"] == "ok"]
    assert ok and all(abs(n["value"]["emg0"] - 0.69) < 0.06 for n in ok)


def test_loaded_calibration_used_when_compatible_and_invalidated_when_not():
    proc, _ = calibrated_run()
    record = proc.calibration_record()
    same = Processor(ProcessingConfig(calibration=FAST_CAL))
    out = same.load_calibration(record)
    for r in synth(55.0, duration=1.0):
        out += same.feed(r)
    assert [n for n in (act(f)["normalized"] for f in emg_feats(out)) if n["status"] == "ok"]

    other = Processor(ProcessingConfig(calibration=FAST_CAL,
                                       emg_processing=EmgProcessingConfig(envelope_window_s=0.2)))
    out = other.load_calibration(record)
    for r in synth(55.0, duration=1.0):
        out += other.feed(r)
    st = [e["detail"] for e in events(out, "calibration_state")]
    assert st[-1]["state"] == "invalidated" and st[-1]["reason"] == "processing_config_mismatch"
    assert all(act(f)["normalized"]["value"] is None for f in emg_feats(out))


def test_rate_change_invalidates_loaded_calibration():
    proc, _ = calibrated_run()
    p = Processor(ProcessingConfig(calibration=FAST_CAL,
                                   emg_processing=EmgProcessingConfig(bandpass=EmgFilterConfig(20, 350))))
    out = p.load_calibration(proc.calibration_record())
    for r in synth(55.0, rate=800.0, duration=1.0):
        out += p.feed(r)
    assert events(out, "calibration_state")[-1]["detail"]["reason"] == "processing_config_mismatch"


def test_provenance_change_invalidates_calibration():
    proc, _ = calibrated_run()
    out = []
    for r in synth(55.0, duration=1.0, source="live")[1:]:
        r["seq"] += 1000
        out += proc.feed(r)
    reasons = [e["detail"]["reason"] for e in events(out, "calibration_state")]
    assert "provenance_changed" in reasons


def test_load_rejects_non_calibration_record():
    out = Processor().load_calibration({"schema": "something-else"})
    assert out[0]["event"] == "calibration_rejected"


def test_simulator_relaxed_vs_strong_and_calibration():
    """Completion check: simulator activation levels are distinct and normalise sensibly."""
    recs = sim("strong_activation", duration=10.0)
    proc, out = run(recs, ProcessingConfig(calibration=CalibrationConfig(relaxed_s=2.0, squeeze_s=2.0)),
                    {0.3: "relaxed", 3.4: "squeeze"})
    assert cal_states(out)[-1] == "calibrated"
    ref = proc.calibration_record()["reference"]
    assert ref["squeeze"]["emg0"] > 8 * ref["relaxed"]["emg0"]
    late = [act(f)["normalized"]["value"]["emg0"] for f in emg_feats(out)
            if act(f)["normalized"]["status"] == "ok" and f["t_us"] > 7.5e6]
    assert late and all(abs(v) < 0.1 for v in late)
