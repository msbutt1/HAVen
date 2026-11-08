"""Daily hand-arm vibration exposure ESTIMATE: ISO 5349-1 A(8), EU/UK limits, HSE points.

Formulas (``ExposureConfig``)::

    a_hv   = sqrt(ax^2 + ay^2 + az^2)                 (vibration.physical.combined_rms)
    A(8)   = sqrt( sum(a_hv,i^2 * dt_i) / T0 ),  T0 = 8 h
    points = 2 * a^2 * hours  (HSE)  =  100 * (A(8) / 2.5)^2
    time to a points level at the current intensity = (level - points) / (2 * a_now^2) hours
    years  = 31.8 * A(8)^-1.06  (ISO 5349-1 Annex C: 10 % of a GROUP with vibration white finger)

The sum is the ISO 5349-1 multi-operation form evaluated per finalised sub-interval, so several
tools and pauses in one day add up energy-equivalently (harm scales with a^2 x time).

Limits (EU Directive 2002/44/EC; UK Control of Vibration at Work Regulations 2005, Schedule 1):
exposure action value A(8) = 2.5 m/s^2 (100 points), exposure limit value A(8) = 5.0 m/s^2
(400 points). ``personal_limit_points`` lets a supervisor or occupational health LOWER the limit
for one worker; it can never be raised above the limit value.

What counts as exposure time. A sub-interval is counted only while the tool detector
(tool_use.py) is in ``tool`` -- arm movement and knocks are not tool exposure. Two totals are kept:

* ``tool_on``  -- tool vibration detected (what a vibration-only watch would count).
* ``gripped``  -- ``tool_on`` minus the time the wearer was KNOWN not to be gripping
                  (calibrated activation below ``grip_activation_min``). Time with unknown grip
                  (EMG not calibrated, stale) is still counted, never dropped, and reported as
                  ``grip_unknown_s``: a monitor must not under-count because a sensor is missing.

With ``grip_gate`` the headline (``daily``) is ``gripped``, else ``tool_on``. Grip does not
multiply the dose: ISO 5349-1 section 4.3 says hand-tool coupling forces should be measured and
REPORTED, so grip is reported alongside (``grip_during_contact``: mean and the 10th/50th/90th
percentiles of activation during contact, the APDF levels of Jonsson 1982; that guideline was
developed mainly for shoulder/neck muscles and is shown as a reference only).

Honesty. a_hv here is measured at the WRIST by a hobby IMU (500 Hz, accel low-pass ~218 Hz) and
is unweighted unless ``weighting`` is ``wh_dominant_frequency``, which scales a_hv by the
ISO 5349-1 W_h gain at the tool's dominant frequency (a single-frequency approximation of the
real filter). It is a MONITORING estimate, not an ISO 5349-2 measurement: wrist readings differ
from the palm, unweighted readings over-state high-frequency tools, and the ISO band reaches
~1250 Hz. Below the action value is not "safe"; the action value is where risk must be managed.

Interval semantics follow score.py exactly (its ``_Track`` is reused): an update describes its
input over (previous update, t], at most the freshness limit back; time is finalised only once
vibration and tool state have both covered it; duplicates/reordering never double count; a boot
change flushes; a provenance change or a new shift starts a new accumulation.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import asdict, dataclass

from .config import ExposureConfig
from .score import USABLE, _block_key, _Input, _Track

LABEL = "daily_vibration_exposure_estimate"
LABEL_NOTE = ("ESTIMATE of ISO 5349-1 A(8) from a wrist-worn IMU (monitoring, not an ISO 5349-2 "
              "measurement); compared with the EU/UK action and limit values; below the action "
              "value is not 'safe'; not a medical device")
POINTS_PER_HOUR_PER_MS2_SQ = 2.0  # HSE: points = 2 * a^2 * hours
EAV_POINTS = 100.0
ELV_POINTS = 400.0
APDF_BINS = 200  # activation 0..2 (normalised) in steps of 0.01; bounded, never a growing list
APDF_MAX = 2.0
FORMULAS = {
    "a_hv": "sqrt(ax^2 + ay^2 + az^2)  (combined dynamic accel RMS, m/s^2)",
    "a8": "A(8) = sqrt(sum(a_hv^2 * dt) / T0), T0 = 8 h",
    "points": "points = 2 * a_hv^2 * hours = 100 * (A(8) / 2.5)^2",
    "time_left": "hours = (level_points - points) / (2 * a_now^2)",
    "years": "D_y = 31.8 * A(8)^-1.06 (ISO 5349-1 Annex C, 10 % of a group)",
}
SOURCES = {
    "eav_elv": "EU Directive 2002/44/EC, Art. 3; UK Control of Vibration at Work Regulations 2005, reg. 4",
    "a8": "ISO 5349-1:2001 section 5 and Annex A; UK SI 2005/1093 Schedule 1",
    "points": "HSE, Hand-arm vibration at work (L140) exposure points system",
    "years": "ISO 5349-1:2001 Annex C (group estimate; later research suggests it is conservative)",
    "grip_reporting": "ISO 5349-1:2001 section 4.3 (coupling forces should be measured and reported)",
    "apdf": "Jonsson 1982, Eur J Appl Physiol (APDF static/median/peak guidelines)",
    "monitoring": "HSE, Measurement and monitoring of HAV; ISO/TS 22270:2026 (hand/wrist/arm-worn monitoring)",
}
APDF_REFERENCE = {"p10_static_max": [0.02, 0.05], "p50_median_max": [0.10, 0.14],
                  "p90_peak_max": [0.50, 0.70],
                  "note": "reference guideline (Jonsson 1982), developed mainly for shoulder/neck"}


def wh_gain(f_hz: float) -> float:
    """|W_h(f)|: ISO 5349-1 / ISO 8041 hand-arm weighting (band-limiting x weighting filter).

    Band limits: 2nd-order Butterworth high-pass 6.31 Hz and low-pass 1258.9 Hz; weighting
    filter f3 = f4 = 15.915 Hz, Q4 = 0.64. About 0.88 at 8 Hz, 0.13 at 125 Hz, 0.10 at 160 Hz.
    """
    s = 2j * math.pi * f_hz
    w1, w2, w3 = 2 * math.pi * 6.31, 2 * math.pi * 1258.9, 2 * math.pi * 15.915
    q = 1 / math.sqrt(2)
    hp = s * s / (s * s + w1 / q * s + w1 * w1)
    lp = w2 * w2 / (s * s + w2 / q * s + w2 * w2)
    wt = (1 + s / w3) / (1 + s / (0.64 * w3) + (s / w3) ** 2)
    return abs(hp * lp * wt) if not cmath.isnan(hp * lp * wt) else 0.0


def a8_from_energy(e2_s: float, t0_s: float = 8 * 3600) -> float:
    """A(8) in m/s^2 from sum(a^2 * dt) in (m/s^2)^2 * s."""
    return math.sqrt(max(0.0, e2_s) / t0_s)


def points_from_energy(e2_s: float) -> float:
    """HSE points from sum(a^2 * dt): 2 * a^2 * hours."""
    return POINTS_PER_HOUR_PER_MS2_SQ * max(0.0, e2_s) / 3600.0


def years_to_10pct_vwf(a8: float | None, cfg: ExposureConfig | None = None) -> float | None:
    """ISO 5349-1 Annex C group estimate; None below the configured minimum (it diverges)."""
    c = cfg or ExposureConfig()
    if a8 is None or not math.isfinite(a8) or a8 < c.min_a8_for_years:
        return None
    return c.years_k * a8 ** c.years_exp


def hours_until(points: float, level: float, a_now: float | None) -> float | None:
    """Hours at a_now until ``points`` reaches ``level`` (0 if already there; None if no exposure now)."""
    if points >= level:
        return 0.0
    if a_now is None or not a_now > 0:
        return None
    return (level - points) / (POINTS_PER_HOUR_PER_MS2_SQ * a_now * a_now)


@dataclass(frozen=True)
class _ToolInput(_Input):
    tool: bool
    dominant_hz: float | None


class _Gate:
    """One accumulation (tool_on or gripped): energy and contact time."""

    def __init__(self) -> None:
        self.e2_s = 0.0
        self.contact_s = 0.0

    def add(self, a: float, dur: float) -> None:
        self.e2_s += a * a * dur
        self.contact_s += dur

    def view(self, cfg: ExposureConfig, limit: float) -> dict:
        r = lambda x: round(x, 6)  # noqa: E731
        a8 = a8_from_energy(self.e2_s, cfg.t0_s)
        pts = points_from_energy(self.e2_s)
        return {"a8_ms2": r(a8), "points": r(pts), "percent_of_limit": r(100.0 * pts / limit),
                "contact_s": r(self.contact_s), "energy_m2s3": r(self.e2_s)}


class ExposureEngine:
    def __init__(self, cfg: ExposureConfig):
        self.cfg = cfg
        self.personal_limit_points = cfg.personal_limit_points
        self.accumulation = 0
        self._vib = _Track("vibration", cfg.vibration_fresh_s)
        self._tool = _Track("tool", cfg.tool_fresh_s)
        self._act = _Track("activation", cfg.activation_fresh_s)
        self._new_accumulation()

    # -- lifecycle ----------------------------------------------------------------

    @property
    def _required(self) -> list[_Track]:
        return [self._vib, self._tool]

    @property
    def _all(self) -> list[_Track]:
        return [self._vib, self._tool, self._act]

    def _new_accumulation(self) -> None:
        self.accumulation += 1
        self.tool_on = _Gate()
        self.gripped = _Gate()
        self.grip_unknown_s = 0.0
        self.not_gripping_s = 0.0
        self.no_tool_s = 0.0
        self.degraded_s = 0.0
        self.excluded_s = 0.0
        self.excluded_by_reason: dict[str, float] = {}
        self._apdf = [0.0] * APDF_BINS
        self._act_sum = 0.0
        self._act_s = 0.0
        self.boot_segments = 1
        self._segment_reset()

    def _segment_reset(self) -> None:
        self.cursor: float | None = None
        self.newest: float | None = None
        self._finalised_any = False
        for tr in self._all:
            tr.reset()

    def flush(self, reason: str) -> None:
        if self.newest is not None:
            self._finalise(self.newest, uncovered=reason)

    def on_boot_change(self) -> None:
        self.flush("boot_change")
        self.boot_segments += 1
        self._segment_reset()

    def reset_accumulation(self) -> None:
        """New shift or provenance change: the daily totals start again from zero."""
        self._new_accumulation()

    def set_personal_limit(self, points: float) -> None:
        if not (isinstance(points, (int, float)) and not isinstance(points, bool)
                and math.isfinite(points) and 0 < points <= ELV_POINTS):
            raise ValueError("personal limit must be in (0, 400] points (it can only be lowered)")
        self.personal_limit_points = float(points)

    # -- updates --------------------------------------------------------------------

    def update_vibration(self, t_us: float | None, vib: dict) -> None:
        phys = vib["physical"]
        usable = phys["status"] in USABLE and phys["combined_rms"] is not None
        reasons = tuple(phys["reasons"]) or tuple(vib["reasons"])
        self._update(self._vib, t_us, _Input(t_us or 0.0, usable, phys["combined_rms"] if usable else None,
                                             phys["status"], reasons, phys["status"] == "degraded"))

    def update_tool(self, t_us: float | None, tool: dict) -> None:
        usable = tool.get("status") == "ok" and tool.get("state") in ("tool", "no_tool")
        self._update(self._tool, t_us, _ToolInput(
            t_us or 0.0, usable, None, tool.get("status", "unavailable"), tuple(tool.get("reasons") or ()),
            False, tool.get("state") == "tool", tool.get("dominant_hz")))

    def update_activation(self, t_us: float | None, norm: dict) -> None:
        usable = norm["status"] in USABLE and norm["value"] is not None
        value = max(norm["value"].values()) if usable else None
        self._update(self._act, t_us, _Input(t_us or 0.0, usable, value, norm["status"],
                                             tuple(norm["reasons"]), norm["status"] == "degraded"))

    def _update(self, track: _Track, t_us: float | None, inp: _Input) -> None:
        if t_us is None or not track.add(t_us, inp):
            return
        if track is not self._act:
            if self.cursor is None or (not self._finalised_any and t_us < self.cursor):
                self.cursor = t_us
        if self.cursor is None:
            return  # activation alone never starts the exposure clock
        self.newest = t_us if self.newest is None else max(self.newest, t_us)
        bound = min(tr.determined_until(self.newest) for tr in self._all)
        self._finalise(bound, uncovered="stale")

    # -- finalisation ------------------------------------------------------------------

    def _weighted(self, a: float, tool: _ToolInput) -> float:
        if self.cfg.weighting == "wh_dominant_frequency":
            if tool.dominant_hz is None or not tool.dominant_hz > 0:
                return a  # no frequency: unweighted (over-states, never under-states)
            return a * wh_gain(tool.dominant_hz)
        return a

    def _finalise(self, until: float, uncovered: str) -> None:
        if self.cursor is None or until <= self.cursor:
            return
        pts = sorted({self.cursor, until, *(p for tr in self._all for p in tr.boundaries(self.cursor, until))})
        for a, b in zip(pts, pts[1:]):
            dur = (b - a) / 1e6
            blocks, inputs = [], {}
            for tr in self._required:
                key, inp = tr.block_at(b, uncovered)
                if key:
                    blocks.append(key)
                inputs[tr.name] = inp
            if blocks:
                key = " & ".join(sorted(blocks))
                self.excluded_s += dur
                self.excluded_by_reason[key] = self.excluded_by_reason.get(key, 0.0) + dur
                continue
            v, tool = inputs["vibration"], inputs["tool"]
            if not tool.tool:  # type: ignore[union-attr]
                self.no_tool_s += dur
                continue
            acc = self._weighted(v.value, tool)  # type: ignore[union-attr,arg-type]
            self.tool_on.add(acc, dur)
            if v.degraded:  # type: ignore[union-attr]
                self.degraded_s += dur
            key, act = self._act.block_at(b, uncovered)
            grip = act.value if key is None and act is not None else None
            if grip is None:
                self.grip_unknown_s += dur
            else:
                self._apdf[min(APDF_BINS - 1, max(0, int(grip / APDF_MAX * APDF_BINS)))] += dur
                self._act_sum += grip * dur
                self._act_s += dur
                if grip < self.cfg.grip_activation_min:
                    self.not_gripping_s += dur
                    continue
            self.gripped.add(acc, dur)
        self.cursor = until
        self._finalised_any = True
        for tr in self._all:
            tr.prune(until)

    def pending_segments(self) -> int:
        return sum(len(tr.segs) for tr in self._all)

    # -- output -----------------------------------------------------------------------

    def _percentile(self, q: float) -> float | None:
        total = sum(self._apdf)
        if total <= 0:
            return None
        run = 0.0
        for i, w in enumerate(self._apdf):
            run += w
            if run >= q * total:
                return (i + 0.5) * APDF_MAX / APDF_BINS
        return APDF_MAX  # pragma: no cover

    def _current(self, now: float | None) -> tuple[list[str], float | None, dict]:
        """(reasons, a_now counted toward the headline or None, current-state facts)."""
        reasons: list[str] = []
        for tr in self._required:
            if tr.latest is None:
                reasons.append(f"{tr.name}_no_data")
            elif not tr.latest.usable:
                reasons.append(_block_key(tr.name, tr.latest))
            elif now is not None and now - tr.last_t > tr.fresh_us:  # type: ignore[operator]
                reasons.append(f"{tr.name}_stale")
        act = self._act.latest
        grip = (act.value if act is not None and act.usable and now is not None
                and now - self._act.last_t <= self._act.fresh_us else None)  # type: ignore[operator]
        tool_now = None if reasons else bool(self._tool.latest.tool)  # type: ignore[union-attr]
        a_hv = None if reasons else self._weighted(self._vib.latest.value, self._tool.latest)  # type: ignore[union-attr,arg-type]
        counting = bool(tool_now) and not (self.cfg.grip_gate and grip is not None
                                           and grip < self.cfg.grip_activation_min)
        facts = {"a_hv_ms2": None if a_hv is None else round(a_hv, 6), "tool_now": tool_now,
                 "grip_activation": None if grip is None else round(grip, 6),
                 "gripping": None if grip is None else grip >= self.cfg.grip_activation_min,
                 "counting": counting if not reasons else None}
        return reasons, (a_hv if counting else None), facts

    def snapshot(self, t_us: float | None) -> dict:
        now = t_us if t_us is not None else self.newest
        c = self.cfg
        limit = self.personal_limit_points
        reasons, a_now, facts = self._current(now)
        head = self.gripped if c.grip_gate else self.tool_on
        daily = head.view(c, limit)
        pts = points_from_energy(head.e2_s)
        pph = None if a_now is None else POINTS_PER_HOUR_PER_MS2_SQ * a_now * a_now
        r = lambda x: None if x is None else round(x, 6)  # noqa: E731
        level = "above_limit" if pts >= limit else "above_eav" if pts >= EAV_POINTS else "below_eav"
        a8 = a8_from_energy(head.e2_s, c.t0_s)
        mean_act = self._act_sum / self._act_s if self._act_s > 0 else None
        return {
            "label": LABEL, "label_note": LABEL_NOTE,
            "status": "accumulating" if not reasons else "paused",
            "reasons": reasons,
            "gate": "gripped" if c.grip_gate else "tool_on",
            "level": level,
            "daily": daily,
            "by_gate": {"tool_on": self.tool_on.view(c, limit), "gripped": self.gripped.view(c, limit)},
            "grip_unknown_s": r(self.grip_unknown_s),
            "not_gripping_s": r(self.not_gripping_s),
            "current": {**facts, "points_per_hour": r(pph),
                        "hours_to_eav": r(hours_until(pts, EAV_POINTS, a_now)),
                        "hours_to_limit": r(hours_until(pts, limit, a_now))},
            "limits": {"eav_ms2": c.eav_ms2, "elv_ms2": c.elv_ms2, "eav_points": EAV_POINTS,
                       "elv_points": ELV_POINTS, "personal_limit_points": limit,
                       "personal_limit_lowered": limit < ELV_POINTS},
            "grip_during_contact": {
                "seconds": r(self._act_s), "mean": r(mean_act),
                "p10": r(self._percentile(0.10)), "p50": r(self._percentile(0.50)),
                "p90": r(self._percentile(0.90)), "units": "normalised activation (0 relaxed, 1 calibration squeeze)",
                "reference": APDF_REFERENCE},
            "years_to_10pct_vwf": {
                "value": r(years_to_10pct_vwf(a8, c)),
                "basis": "this accumulation's A(8) as if repeated every working day",
                "note": ("ISO 5349-1 Annex C group estimate (10 % of exposed workers), not a diagnosis; "
                         f"not shown below A(8) {c.min_a8_for_years:g} m/s^2")},
            "weighting": ("ISO 5349-1 W_h gain at the dominant tool frequency (approximation)"
                          if c.weighting == "wh_dominant_frequency"
                          else "none (unweighted; over-states high-frequency tools vs ISO W_h)"),
            "measurement": "wrist-worn IMU monitoring estimate (ISO/TS 22270 style), not ISO 5349-2",
            "accumulation": self.accumulation,
            "boot_segments": self.boot_segments,
            "integrated_until_us": None if self.cursor is None else round(self.cursor),
            "no_tool_s": r(self.no_tool_s),
            "degraded_duration_s": r(self.degraded_s),
            "excluded_duration_s": r(self.excluded_s),
            "excluded_by_reason": {k: r(v) for k, v in sorted(self.excluded_by_reason.items())},
            "clock": "device_time (recording time for replay; independent of wall-clock pacing)",
            "formula": {**FORMULAS, "coefficients": asdict(c)},
        }


def limits_reference(cfg: ExposureConfig | None = None) -> dict:
    """Static description of the limits, formulas and sources (for an info page or API)."""
    c = cfg or ExposureConfig()
    return {
        "label": LABEL, "label_note": LABEL_NOTE,
        "limits": {
            "exposure_action_value": {"a8_ms2": c.eav_ms2, "points": EAV_POINTS,
                                      "meaning": "above this, the employer must act to reduce exposure"},
            "exposure_limit_value": {"a8_ms2": c.elv_ms2, "points": ELV_POINTS,
                                     "meaning": "must not be exceeded on any day"},
            "personal_limit_points_default": c.personal_limit_points,
        },
        "formulas": FORMULAS,
        "worked_example": {
            "tool": "grinder at a_hv = 6 m/s^2",
            "points_per_hour": POINTS_PER_HOUR_PER_MS2_SQ * 36,
            "hours_to_action_value": round(hours_until(0, EAV_POINTS, 6.0), 2),  # type: ignore[arg-type]
            "hours_to_limit_value": round(hours_until(0, ELV_POINTS, 6.0), 2),  # type: ignore[arg-type]
        },
        "years_to_10pct_vwf_examples": {str(a): round(years_to_10pct_vwf(a, c), 1)  # type: ignore[arg-type]
                                        for a in (2.5, 3.0, 5.0)},
        "apdf_reference": APDF_REFERENCE,
        "sources": SOURCES,
        "limitations": [
            "wrist-worn hobby IMU: a monitoring estimate, not an ISO 5349-2 measurement",
            "unweighted unless configured: over-states high-frequency tools compared with ISO W_h",
            "bandwidth about 5-218 Hz; the ISO band reaches about 1250 Hz",
            "below the action value is not risk-free; limits are group-based and individuals differ",
            "not a medical device; years-to-risk is a group estimate, not a diagnosis",
        ],
    }
