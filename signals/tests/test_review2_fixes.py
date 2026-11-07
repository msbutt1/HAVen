"""Regression tests for the post-Prompt-6 independent review (findings A-C).

A: score finalisation must not depend on EMG/IMU arrival order.
B: signal resets must reach an active calibration capture directly.
C: loaded calibration records must be validated before installation.
Synthetic inputs only.
"""

import copy
import itertools
import math
import random

import pytest

from vibeguard_signals.processing import ProcessingConfig, Processor
from vibeguard_signals.processing.config import CalibrationConfig, ScoreConfig
from vibeguard_signals.processing.score import ScoreEngine

from test_emg import calibrated_run, emg_feats, events, run, synth
from test_vibration_score import ACT, VIB

FAST_CAL = CalibrationConfig(relaxed_s=1.0, squeeze_s=1.0, discard_s=0.2, min_updates=5)


def feed(engine, ups):
    """ups: [(kind, t_s, payload)] in arrival order."""
    for kind, t, payload in ups:
        (engine.update_vibration if kind == "vib" else engine.update_activation)(t * 1e6, payload)
    return engine


def totals(e):
    return (round(e.cumulative, 12), round(e.valid_s, 9), round(e.excluded_s, 9),
            {k: round(v, 9) for k, v in sorted(e.excluded_by_reason.items())})


def prefix(n=20, step=0.05):
    out = []
    for i in range(n + 1):
        out += [("vib", i * step, VIB(2.0)), ("act", i * step, ACT(0.5))]
    return out


# -- A: arrival order ------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["vib", "act"])
def test_one_input_invalid_either_arrival_order_equal_timestamps(bad):
    nxt = {"vib": VIB(None, "unavailable") if bad == "vib" else VIB(2.0),
           "act": ACT(None, "unavailable") if bad == "act" else ACT(0.5)}
    results = []
    for order in (("vib", "act"), ("act", "vib")):
        e = feed(ScoreEngine(ScoreConfig()), prefix() + [(k, 1.05, nxt[k]) for k in order])
        results.append(totals(e))
        assert e.valid_s == pytest.approx(1.0)  # the invalid 0.05 s is never valid
    assert results[0] == results[1]
    key = "vibration_unavailable" if bad == "vib" else "activation_uncalibrated"
    assert results[0][3] == {key: 0.05}


def test_both_inputs_invalid_either_arrival_order():
    results = []
    for order in (("vib", "act"), ("act", "vib")):
        tail = [(k, 1.05, VIB(None, "unavailable") if k == "vib" else ACT(None, "unavailable")) for k in order]
        results.append(totals(feed(ScoreEngine(ScoreConfig()), prefix() + tail)))
    assert results[0] == results[1]
    assert results[0][3] == {"activation_uncalibrated & vibration_unavailable": 0.05}


@pytest.mark.parametrize("lag_ms", [3.0, 25.0, 49.0])
def test_staggered_updates_invalid_vibration_never_counted(lag_ms):
    """IMU updates lag EMG by lag_ms; vibration invalid over (1.0, 2.0]."""
    lag = lag_ms / 1000
    ups = []
    for i in range(61):
        t = i * 0.05
        v = VIB(None, "unavailable") if 1.0 < t + lag <= 2.0 + 1e-9 else VIB(2.0)
        ups += [("act", t, ACT(0.5)), ("vib", t + lag, v)]
    e = feed(ScoreEngine(ScoreConfig()), ups)
    # same updates, IMU delivered before EMG at every step (lead stays within freshness)
    swapped = []
    for i in range(0, len(ups), 2):
        swapped += [ups[i + 1], ups[i]]
    reordered = feed(ScoreEngine(ScoreConfig()), swapped)
    # vibration invalid for exactly 1.0 s of its own timeline; nothing in it counts
    assert e.excluded_by_reason.get("vibration_unavailable", 0) == pytest.approx(1.0, abs=1e-6)
    assert totals(e) == totals(reordered)


def test_order_invariance_randomised():
    """Any interleaving that keeps each stream's own order, with neither stream leading
    the other by more than the freshness limit, gives identical results. (A stream that
    leads by more than that makes the lagging one stale: explicit freshness handling,
    tested in test_lead_beyond_freshness_declares_lagging_input_stale.)"""
    rng = random.Random(7)
    vib, act = [], []
    t_v = t_a = 0.0
    for _ in range(200):
        t_v += rng.choice([0.025, 0.05, 0.05, 0.1])
        t_a += rng.choice([0.03, 0.05, 0.05, 0.08])
        vib.append(("vib", round(t_v, 6), VIB(rng.uniform(0.5, 3)) if rng.random() > 0.2 else VIB(None, "unavailable")))
        act.append(("act", round(t_a, 6), ACT(rng.uniform(0, 1)) if rng.random() > 0.2 else ACT(None, "unavailable")))
    cfg = ScoreConfig(vibration_fresh_s=1.0, activation_fresh_s=1.0)
    by_time = sorted(vib + act, key=lambda u: u[1])

    def merged(seed, max_lead=0.9):
        r = random.Random(seed)
        a, b, out = list(vib), list(act), []
        while a or b:
            take_a = a and (not b or r.random() < 0.5)
            if a and b and take_a and a[0][1] - b[0][1] > max_lead:
                take_a = False   # would lead too far: deliver the other stream first
            elif a and b and not take_a and b[0][1] - a[0][1] > max_lead:
                take_a = True
            out.append((a if take_a else b).pop(0))
        return out

    ref = totals(feed(ScoreEngine(cfg), by_time))
    for seed in range(6):
        assert totals(feed(ScoreEngine(cfg), merged(seed))) == ref


def test_lead_beyond_freshness_declares_lagging_input_stale():
    e = feed(ScoreEngine(ScoreConfig()), prefix())  # both live up to 1.0 s
    ups = [("vib", 1.0 + i * 0.05, VIB(2.0)) for i in range(1, 41)] + \
          [("act", 1.0 + i * 0.05, ACT(0.5)) for i in range(1, 41)]
    feed(e, ups)  # vibration delivered 2 s ahead of activation
    assert e.excluded_by_reason["activation_stale"] == pytest.approx(2.0 - 0.25, abs=1e-6)
    assert e.valid_s == pytest.approx(1.0 + 0.25)  # the late activation still covers its last 0.25 s


def test_freshness_still_explicit_and_buffers_bounded():
    e = ScoreEngine(ScoreConfig())
    feed(e, prefix())
    for i in range(1, 2001):  # activation silent; vibration continues for 100 s
        e.update_vibration((1.0 + i * 0.05) * 1e6, VIB(2.0))
        assert e.pending_segments() <= 2 * (0.25 / 0.05 + 2)
    assert e.excluded_by_reason["activation_stale"] == pytest.approx(100.0 - 0.25, abs=0.06)
    assert "activation_stale" in e.snapshot(None)["reasons"]


def test_processor_scores_identical_when_streams_interleave_differently():
    """End to end: the same telemetry with EMG/IMU records reordered among
    themselves (each stream's own order kept) gives the same final score."""
    from vibeguard_signals.demo import CALIBRATION_SCHEDULE, demo_processing_config, process_records
    from vibeguard_signals.simulator import SimConfig, Simulation
    # Fault-free stream: the processor's own device-clock staleness rule reacts to arrival
    # order at transport-loss boundaries, so exact invariance is only claimed without loss.
    recs = [r.record for r in Simulation(SimConfig(scenario="happy_path", duration_s=10, seed=1),
                                         run_id="order").records() if not r.lost]
    sched = ((0.3, "relaxed"), (2.1, "squeeze"))
    from vibeguard_signals.processing.config import CalibrationConfig as _CC
    cfg = ProcessingConfig(imu=demo_processing_config().imu,
                           calibration=_CC(relaxed_s=1.0, squeeze_s=1.0, discard_s=0.2, min_updates=5))
    _, a = process_records(recs, cfg, sched)
    # move each IMU batch after the next EMG batch where possible (seq order changes too,
    # so renumber within the boot to keep it a valid in-order stream)
    swapped = list(recs)
    for i in range(1, len(swapped) - 1):
        if swapped[i]["type"] == "imu_batch" and swapped[i + 1]["type"] == "emg_batch":
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
    for n, r in enumerate(swapped):
        r = swapped[n] = copy.deepcopy(r)
        r["seq"] = n
    _, b = process_records(swapped, cfg, sched)
    fa = [o for o in a if o["type"] == "experimental_score"][-1]
    fb = [o for o in b if o["type"] == "experimental_score"][-1]
    assert fa["cumulative"]["value"] > 0
    assert fb["cumulative"]["value"] == fa["cumulative"]["value"]
    assert fb["valid_duration_s"] == fa["valid_duration_s"]
    assert fb["excluded_by_reason"] == fa["excluded_by_reason"]


# -- B: resets reach active captures ------------------------------------------------------

LONG_BATCH_CAL = CalibrationConfig(relaxed_s=2.0, squeeze_s=2.0, discard_s=0.2, min_updates=3)


def test_gap_interrupts_capture_even_when_recovery_batch_completes_warmup():
    recs = synth(10.0, duration=4.0, batch=250)  # 0.25 s batches >= 0.2 s warm-up
    _, out = run(recs[:4] + recs[5:], ProcessingConfig(calibration=LONG_BATCH_CAL), {0.3: "relaxed"})
    last = events(out, "calibration_state")[-1]["detail"]
    assert last["state"] == "failed" and last["reason"] == "interrupted_gap"


def test_null_sample_interrupts_capture_with_long_batches():
    recs = synth(10.0, duration=4.0, batch=250)
    recs[4]["samples"][10] = [None]
    recs[4]["quality"]["flags"] = ["missing_values"]
    _, out = run(recs, ProcessingConfig(calibration=LONG_BATCH_CAL), {0.3: "relaxed"})
    last = events(out, "calibration_state")[-1]["detail"]
    assert last["state"] == "failed" and last["reason"] == "interrupted_missing_values"


def test_reset_before_capture_starts_does_not_fail_it():
    """A gap during envelope warm-up (capture commanded, not yet started) only delays it."""
    recs = synth(10.0, duration=4.0)  # 50-sample batches; warm-up needs 4 batches
    gapped = recs[:4] + recs[5:]      # gap at ~0.2 s, before the first usable envelope
    _, out = run(gapped, ProcessingConfig(calibration=LONG_BATCH_CAL), {0.06: "relaxed"})
    st = [e["detail"] for e in events(out, "calibration_state")]
    assert [s["state"] for s in st] == ["capturing_relaxed", "relaxed_captured"]
    assert st[-1]["capture"]["t_start_us"] > recs[5]["timing"]["t0_us"]  # began after the gap


def test_clean_long_batches_still_calibrate():
    recs = synth(10.0, duration=4.0, batch=250)
    _, out = run(recs, ProcessingConfig(calibration=LONG_BATCH_CAL), {0.3: "relaxed"})
    assert events(out, "calibration_state")[-1]["detail"]["state"] == "relaxed_captured"


# -- C: loaded calibration validation ---------------------------------------------------

GOOD = None


def good_record():
    global GOOD
    if GOOD is None:
        GOOD = calibrated_run()[0].calibration_record()
    return copy.deepcopy(GOOD)


MUTATIONS = {
    "squeeze_equals_relaxed": lambda r: r["reference"]["squeeze"].update(emg0=r["reference"]["relaxed"]["emg0"]),
    "squeeze_below_ratio": lambda r: r["reference"]["squeeze"].update(emg0=1.5 * r["reference"]["relaxed"]["emg0"]),
    "missing_squeeze_channel": lambda r: r["reference"]["squeeze"].pop("emg0"),
    "extra_relaxed_channel": lambda r: r["reference"]["relaxed"].update(emg1=3.0),
    "nan_reference": lambda r: r["reference"]["relaxed"].update(emg0=float("nan")),
    "inf_reference": lambda r: r["reference"]["squeeze"].update(emg0=float("inf")),
    "string_reference": lambda r: r["reference"]["squeeze"].update(emg0="70"),
    "bool_reference": lambda r: r["reference"]["relaxed"].update(emg0=True),
    "negative_reference": lambda r: r["reference"]["relaxed"].update(emg0=-1.0),
    "reference_not_dict": lambda r: r.update(reference="oops"),
    "no_channels": lambda r: r.update(reference={"relaxed": {}, "squeeze": {}}),
    "missing_calibration_id": lambda r: r.pop("calibration_id"),
    "missing_signature": lambda r: r.pop("signature"),
    "pipeline_channel_mismatch": lambda r: r["pipeline"].update(channels=["emg1"]),
    "wrong_label": lambda r: r.update(label="grip_force_newtons"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_invalid_loaded_calibration_rejected_visibly_without_crashing(name):
    rec = good_record()
    MUTATIONS[name](rec)
    proc = Processor(ProcessingConfig(calibration=FAST_CAL))
    out = proc.load_calibration(rec)
    rej = [o for o in out if o["event"] == "calibration_rejected"]
    assert rej and rej[0]["detail"]["reason"] == "invalid_calibration_record" and rej[0]["detail"]["problems"]
    assert proc.calibration_record() is None
    for r in synth(50.0, duration=1.0):
        out += proc.feed(r)  # processing continues
    norm = act_norm(out)
    assert norm["status"] == "unavailable" and norm["value"] is None


def act_norm(out):
    return emg_feats(out)[-1]["features"]["emg_activation"]["normalized"]


def test_rejected_load_keeps_existing_valid_calibration():
    proc, _ = calibrated_run()
    cid = proc.calibration_record()["calibration_id"]
    bad = good_record()
    MUTATIONS["squeeze_equals_relaxed"](bad)
    out = proc.load_calibration(bad)
    assert out[0]["event"] == "calibration_rejected"
    assert proc.calibration_record()["calibration_id"] == cid


def test_non_dict_record_rejected():
    for bad in (None, [], "x", {"schema": "vibeguard.emg_calibration", "version": 2}):
        out = Processor().load_calibration(bad)
        assert out[0]["event"] == "calibration_rejected"


def test_valid_record_still_loads_and_normalises():
    proc = Processor(ProcessingConfig(calibration=FAST_CAL))
    out = proc.load_calibration(good_record())
    for r in synth(55.0, duration=1.0):
        out += proc.feed(r)
    norm = act_norm(out)
    assert norm["status"] == "ok" and math.isfinite(norm["value"]["emg0"])
