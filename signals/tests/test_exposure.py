"""Daily exposure estimate: A(8), HSE points, time left, grip gating, years-to-risk, intervals.

Synthetic inputs only (engine level) plus one end-to-end run through the Processor on the
simulator's tool-vs-arm scenario.
"""

from __future__ import annotations

import math

import pytest

from vibeguard_signals.demo import demo_processing_config
from vibeguard_signals.processing import Processor
from vibeguard_signals.processing.config import ExposureConfig
from vibeguard_signals.processing.exposure import (ExposureEngine, a8_from_energy, hours_until,
                                                   limits_reference, points_from_energy, wh_gain,
                                                   years_to_10pct_vwf)
from vibeguard_signals.simulator import SimConfig, Simulation

VIB = lambda v, status="ok": {"physical": {"status": status, "combined_rms": v if status in ("ok", "degraded") else None,  # noqa: E731
                                           "reasons": [] if status == "ok" else [status]},
                              "reasons": []}
ACT = lambda a, status="ok": {"status": status, "value": {"emg0": a} if status in ("ok", "degraded") else None,  # noqa: E731
                              "reasons": [] if status == "ok" else ["uncalibrated"]}
TOOL = lambda on=True, hz=120.0, status="ok": {"status": status, "state": ("tool" if on else "no_tool")  # noqa: E731
                                               if status == "ok" else "unknown", "dominant_hz": hz,
                                               "reasons": [] if status == "ok" else ["warming_up"]}


def drive(e, duration_s, step_s=0.05, v=2.5, a=0.5, tool=True, t0=0.0, act_status="ok", hz=120.0):
    t = t0
    while t <= t0 + duration_s + 1e-9:
        e.update_vibration(t * 1e6, VIB(v))
        e.update_tool(t * 1e6, TOOL(tool, hz))
        e.update_activation(t * 1e6, ACT(a, act_status))
        t += step_s
    return e


def snap(e):
    return e.snapshot(None)


# -- formulas ---------------------------------------------------------------------------------

@pytest.mark.parametrize("a, hours, points", [(2.5, 8, 100), (5.0, 8, 400), (10.0, 2, 400), (6.0, 1, 72)])
def test_hse_points_and_a8_match_the_regulations(a, hours, points):
    e2 = a * a * hours * 3600
    assert points_from_energy(e2) == pytest.approx(points)
    assert a8_from_energy(e2) == pytest.approx(a * math.sqrt(hours / 8))
    assert 100 * (a8_from_energy(e2) / 2.5) ** 2 == pytest.approx(points)   # both point formulas agree


def test_multi_tool_day_is_the_iso_multi_operation_sum():
    e = ExposureEngine(ExposureConfig())
    drive(e, 1800, v=4.0)                       # 30 min at 4 m/s^2
    drive(e, 3600, v=2.0, t0=1800.05)           # then 1 h at 2 m/s^2
    expected = math.sqrt((4.0 ** 2 * 1800 + 2.0 ** 2 * 3600) / 28800)
    assert snap(e)["daily"]["a8_ms2"] == pytest.approx(expected, rel=1e-4)


def test_time_left_at_current_intensity():
    assert hours_until(0, 100, 6.0) == pytest.approx(100 / 72)              # ~1.39 h
    assert hours_until(0, 400, 6.0) == pytest.approx(400 / 72)              # ~5.56 h
    assert hours_until(150, 100, 6.0) == 0.0                                # already past
    assert hours_until(50, 100, None) is None                               # no exposure now
    e = drive(ExposureEngine(ExposureConfig()), 3600, v=5.0)                # 50 points so far
    cur = snap(e)["current"]
    assert cur["points_per_hour"] == pytest.approx(50.0)
    assert cur["hours_to_eav"] == pytest.approx(1.0, rel=1e-3)
    assert cur["hours_to_limit"] == pytest.approx(7.0, rel=1e-3)


@pytest.mark.parametrize("a8, years", [(2.5, 12.0), (3.0, 9.9), (5.0, 5.8)])
def test_years_to_risk_matches_iso_annex_c(a8, years):
    assert years_to_10pct_vwf(a8) == pytest.approx(years, abs=0.06)


def test_years_to_risk_is_not_shown_for_low_exposure():
    assert years_to_10pct_vwf(0.9) is None and years_to_10pct_vwf(None) is None
    e = drive(ExposureEngine(ExposureConfig()), 60, v=2.5)                  # tiny A(8)
    assert snap(e)["years_to_10pct_vwf"]["value"] is None


def test_wh_gain_matches_iso_5349_shape():
    assert wh_gain(8) == pytest.approx(0.88, abs=0.02)
    assert wh_gain(125) == pytest.approx(0.128, abs=0.005)
    assert wh_gain(160) == pytest.approx(0.10, abs=0.005)
    assert wh_gain(1) < 0.05                                                # band-limited


def test_optional_wh_weighting_scales_by_the_dominant_frequency():
    plain = drive(ExposureEngine(ExposureConfig()), 600, v=4.0, hz=160.0)
    weighted = drive(ExposureEngine(ExposureConfig(weighting="wh_dominant_frequency")), 600, v=4.0, hz=160.0)
    ratio = snap(weighted)["daily"]["a8_ms2"] / snap(plain)["daily"]["a8_ms2"]
    assert ratio == pytest.approx(wh_gain(160.0), rel=1e-4)   # outputs rounded to 6 dp
    assert "unweighted" in snap(plain)["weighting"] and "W_h" in snap(weighted)["weighting"]


# -- what counts as exposure ------------------------------------------------------------------

def test_arm_movement_without_a_tool_is_not_exposure():
    e = drive(ExposureEngine(ExposureConfig()), 600, v=6.0, tool=False)
    s = snap(e)
    assert s["daily"]["points"] == 0 and s["by_gate"]["tool_on"]["contact_s"] == 0
    assert s["no_tool_s"] == pytest.approx(600, abs=0.1)


def test_relaxed_hand_is_excluded_from_gripped_but_counted_as_tool_on():
    e = drive(ExposureEngine(ExposureConfig()), 600, v=3.0, a=0.05)        # tool buzzing, hand relaxed
    s = snap(e)
    assert s["by_gate"]["tool_on"]["contact_s"] == pytest.approx(600, abs=0.1)
    assert s["by_gate"]["gripped"]["contact_s"] == 0 and s["daily"]["points"] == 0
    assert s["not_gripping_s"] == pytest.approx(600, abs=0.1)


def test_unknown_grip_still_counts_and_is_reported():
    e = drive(ExposureEngine(ExposureConfig()), 600, v=3.0, act_status="unavailable")
    s = snap(e)
    assert s["by_gate"]["gripped"]["contact_s"] == pytest.approx(600, abs=0.1)
    assert s["grip_unknown_s"] == pytest.approx(600, abs=0.1)


def test_without_the_grip_gate_the_headline_is_tool_on():
    e = drive(ExposureEngine(ExposureConfig(grip_gate=False)), 600, v=3.0, a=0.05)
    s = snap(e)
    assert s["gate"] == "tool_on" and s["daily"]["contact_s"] == pytest.approx(600, abs=0.1)


def test_grip_during_contact_reports_apdf_percentiles():
    e = ExposureEngine(ExposureConfig())
    drive(e, 90, a=0.2)
    drive(e, 9.9, a=0.8, t0=90.05)
    g = snap(e)["grip_during_contact"]
    assert g["p10"] == pytest.approx(0.2, abs=0.011) and g["p50"] == pytest.approx(0.2, abs=0.011)
    assert g["p90"] == pytest.approx(0.2, abs=0.011) or g["p90"] == pytest.approx(0.8, abs=0.011)
    assert g["mean"] == pytest.approx((0.2 * 90 + 0.8 * 9.9) / 99.9, rel=0.01)
    assert "shoulder/neck" in g["reference"]["note"]


# -- limits and status ------------------------------------------------------------------------

def test_levels_and_personal_limit():
    e = drive(ExposureEngine(ExposureConfig()), 3600 * 2.5, step_s=0.2, v=5.0)   # 125 points
    s = snap(e)
    assert s["daily"]["points"] == pytest.approx(125, rel=1e-3) and s["level"] == "above_eav"
    e.set_personal_limit(100)                                               # lowered after symptoms
    s = snap(e)
    assert s["level"] == "above_limit" and s["limits"]["personal_limit_lowered"]
    assert s["daily"]["percent_of_limit"] == pytest.approx(125, rel=1e-3)
    assert s["current"]["hours_to_limit"] == 0.0


@pytest.mark.parametrize("bad", [0, -5, 401, float("nan"), True, "100"])
def test_personal_limit_can_only_be_lowered(bad):
    with pytest.raises(ValueError):
        ExposureEngine(ExposureConfig()).set_personal_limit(bad)


@pytest.mark.parametrize("kw", [dict(eav_ms2=5.0, elv_ms2=2.5), dict(personal_limit_points=500),
                                dict(weighting="iso"), dict(years_exp=1.0), dict(t0_s=0),
                                dict(grip_activation_min=2), dict(tool_fresh_s=0)])
def test_invalid_exposure_config_is_rejected(kw):
    with pytest.raises(ValueError):
        ExposureConfig(**kw).validate()


def test_limits_reference_has_values_formulas_and_sources():
    ref = limits_reference()
    assert ref["limits"]["exposure_action_value"] == {**ref["limits"]["exposure_action_value"],
                                                      "a8_ms2": 2.5, "points": 100.0}
    assert ref["worked_example"]["points_per_hour"] == 72.0
    assert ref["worked_example"]["hours_to_action_value"] == pytest.approx(1.39)
    assert "2002/44/EC" in ref["sources"]["eav_elv"] and ref["limitations"]


# -- interval semantics (shared with score.py) -------------------------------------------------

def test_duplicates_and_reordered_updates_never_double_count():
    e = drive(ExposureEngine(ExposureConfig()), 60)
    before = snap(e)["daily"]["energy_m2s3"]
    for t in (30.0, 10.0, 60.0):
        e.update_vibration(t * 1e6, VIB(9.0))
        e.update_tool(t * 1e6, TOOL())
    assert snap(e)["daily"]["energy_m2s3"] == before


def test_stale_gaps_are_excluded():
    e = drive(ExposureEngine(ExposureConfig()), 10)
    contact = e.tool_on.contact_s
    e.update_vibration(13e6, VIB(2.5))
    e.update_tool(13e6, TOOL())
    e.update_activation(13e6, ACT(0.5))
    assert e.tool_on.contact_s - contact == pytest.approx(0.25)
    assert sum(snap(e)["excluded_by_reason"].values()) == pytest.approx(2.75, abs=0.06)


def test_tool_detector_warming_up_excludes_time_with_its_reason():
    e = ExposureEngine(ExposureConfig())
    t = 0.0
    while t <= 5.0:
        e.update_vibration(t * 1e6, VIB(3.0))
        e.update_tool(t * 1e6, TOOL(status="unavailable"))
        t += 0.05
    s = snap(e)
    assert s["daily"]["contact_s"] == 0 and "tool_warming_up" in s["excluded_by_reason"]
    assert s["status"] == "paused"


def test_boot_change_flushes_and_keeps_the_day():
    e = drive(ExposureEngine(ExposureConfig()), 100)
    e.on_boot_change()
    drive(e, 100, t0=0.0)                                                   # device time restarts
    s = snap(e)
    assert s["boot_segments"] == 2 and s["daily"]["contact_s"] == pytest.approx(200, abs=0.2)


def test_reset_accumulation_starts_a_new_day():
    e = drive(ExposureEngine(ExposureConfig()), 100)
    e.reset_accumulation()
    assert snap(e)["daily"]["points"] == 0 and snap(e)["accumulation"] == 2


# -- end to end through the processor -----------------------------------------------------------

def run_sim(proc=None):
    sim = Simulation(SimConfig(scenario="tool_vs_arm", duration_s=60.0, seed=1), run_id="exposure-test")
    proc = proc or Processor(demo_processing_config())
    out = []
    for r in sim.records():
        if not r.lost:
            out += proc.feed(r.record)
    return proc, out + proc.finish()


def test_processor_emits_exposure_and_counts_only_tool_time():
    proc, out = run_sim()
    exp = [o for o in out if o["type"] == "exposure"]
    assert exp and exp[-1]["trigger"] == "end_of_input"
    last = exp[-1]
    tool_s = [o for o in out if o["type"] == "tool_use" and o["t_us"] is not None][-1]["session_tool_s"]
    assert 0 < last["by_gate"]["tool_on"]["contact_s"] <= tool_s + 3.0      # arm phases excluded
    assert last["by_gate"]["tool_on"]["points"] > 0
    assert last["label"] == "daily_vibration_exposure_estimate" and "not a medical device" in last["label_note"]


def test_processor_new_shift_resets_exposure():
    proc, out = run_sim()
    assert proc.exposure.tool_on.e2_s > 0
    ev = proc.tool_use_reset_session()[0]
    assert ev["detail"]["exposure_points"] >= 0
    assert proc.exposure.tool_on.e2_s == 0 and proc.exposure.accumulation == 2


def test_processor_personal_limit():
    proc = Processor(demo_processing_config())
    proc.set_personal_exposure_limit(150)
    assert proc.exposure.snapshot(None)["limits"]["personal_limit_points"] == 150
    with pytest.raises(ValueError):
        proc.set_personal_exposure_limit(1000)
