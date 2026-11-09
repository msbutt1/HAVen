"""Tool-use detection and tool-time tracking from the wearable accelerometer.

Separates TOOL USE from NORMAL ARM USE by where the vibration energy is:

* tool band   -- causal 4th-order high-pass at ``tool_band_low_hz`` (20 Hz):
                 power tools vibrate continuously at tens to hundreds of Hz.
* motion band -- causal band-pass ``motion_band_hz`` (0.5-10 Hz): reaching,
                 swinging, lifting, shaking.

A window is a tool CANDIDATE when the tool-band RMS (m/s^2, vector magnitude)
is at least ``on_threshold_ms2`` and at least ``tool_to_motion_min`` x the
motion-band RMS. The state changes to ``tool`` only after candidates persist
for ``on_hold_s`` (brief knocks/taps never qualify) and back to ``no_tool``
after the tool band stays below ``off_threshold_ms2`` for ``off_hold_s``
(short pauses inside a job do not split it): a bout ends when less than 30 %
of the last ``off_hold_s`` had tool vibration (``off_threshold_ms2`` and the
same tool-vs-motion ratio).

Time accounting (device clock, per update):
* ``session_tool_s``     -- time with tool vibration while in a tool bout, plus
                            the on-hold time that confirmed the bout
                            (back-filled when the bout starts).
* ``bout_s``             -- elapsed time of the current bout, pauses included.
* ``session_observed_s`` -- time the detector had valid data; unavailable
                            time is never counted as either tool or no-tool.

The session persists across device reboots and stream gaps (same Processor),
until ``reset_session()`` (e.g. a new shift).

Warnings are team-chosen thresholds (config), NOT regulatory exposure limits
and not a vibration dose. The detector reports what it sees; tuning on the
real tools is pending. Supporting evidence: latest normalised EMG activation
(grip) when calibrated -- reported, not required.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np
from scipy.signal import butter, sosfilt

from .config import ToolUseConfig

ACCEL = ("ax", "ay", "az")
LABEL = "tool_use_detection_by_vibration_band"
NOTE = ("tool vs normal arm use from accelerometer band energy; thresholds are provisional "
        "team settings, not regulatory limits; not a vibration dose")


class ToolUseDetector:
    def __init__(self, cfg: ToolUseConfig, conversion, conversion_source):
        self.cfg = cfg
        self._conv = conversion
        self._conv_source = conversion_source
        self.rate: float | None = None
        self.units: dict[str, str] | None = None
        self._idx: list[int] = []
        self.config_error: str | None = None
        self._sos_tool = self._sos_motion = None
        self._scale: np.ndarray | None = None
        self._n_win = 0
        self._cal_init()
        self.reset_session()
        self.reset("no_data")

    # -- configuration ------------------------------------------------------
    def configure(self, rate: float, channels: list[str], units: dict[str, str]) -> None:
        if (rate, units) == (self.rate, self.units) and self._idx:
            return
        self.rate, self.units = rate, dict(units)
        self._idx = [i for i, ch in enumerate(channels) if ch in ACCEL]
        axes = [channels[i] for i in self._idx]
        self.config_error = None if axes else "no accelerometer channels"
        if self.config_error is None:
            self.config_error = self.cfg.check(rate)
        self._scale = None
        if self.config_error is None:
            if all(units[a] == "m/s^2" for a in axes):
                self._scale = np.ones(len(axes))
            elif self._conv and all(units[a] == "raw_counts" and a in self._conv for a in axes):
                self._scale = np.array([abs(float(self._conv[a][0])) for a in axes])
            else:
                self.config_error = "no m/s^2 conversion (thresholds are physical)"
        if self.config_error is None:
            self._sos_tool = butter(self.cfg.order, self.cfg.tool_band_low_hz, btype="highpass",
                                    fs=rate, output="sos")
            self._sos_motion = butter(2, list(self.cfg.motion_band_hz), btype="bandpass",
                                      fs=rate, output="sos")
            self._n_win = max(8, round(self.cfg.window_s * rate))
        self.reset("configured")

    # -- signal path -------------------------------------------------------
    def reset(self, reason: str) -> None:
        """Stream reset: filters and windows restart; the session continues."""
        self._zt = self._zm = None
        self._x0 = None
        self._tool = np.empty((0, len(self._idx)))
        self._motion = np.empty((0, len(self._idx)))
        self.reset_reason = reason

    def process(self, rows: list[list]) -> None:
        if self.config_error is not None or self.rate is None:
            return
        seg = []
        for row in rows:
            vals = [row[i] for i in self._idx]
            if any(v is None for v in vals):
                if seg:
                    self._segment(seg)
                    seg = []
                self.reset("missing_values")
                continue
            seg.append(vals)
        if seg:
            self._segment(seg)

    def _segment(self, rows: list[list]) -> None:
        x = np.asarray(rows, dtype=float) * self._scale            # m/s^2
        if self._x0 is None:
            self._x0 = x[0].copy()
        if self._zt is None:
            self._zt = np.zeros((self._sos_tool.shape[0], 2, x.shape[1]))
            self._zm = np.zeros((self._sos_motion.shape[0], 2, x.shape[1]))
        xt, self._zt = sosfilt(self._sos_tool, x - self._x0, axis=0, zi=self._zt)
        xm, self._zm = sosfilt(self._sos_motion, x - self._x0, axis=0, zi=self._zm)
        n = self._n_win
        self._tool = np.concatenate([self._tool, xt])[-2 * n:]
        self._motion = np.concatenate([self._motion, xm])[-2 * n:]

    def _features(self) -> dict | None:
        """Band RMS over the last window, after one window of filter settling."""
        n = self._n_win
        if self.rate is None or n == 0 or len(self._tool) < 2 * n:
            return None
        t = self._tool[-n:]
        m = self._motion[-n:]
        tool = float(math.sqrt(float(np.sum(np.mean(t ** 2, axis=0)))))
        motion = float(math.sqrt(float(np.sum(np.mean(m ** 2, axis=0)))))
        spec = np.sum(np.abs(np.fft.rfft(t * np.hanning(n)[:, None], axis=0)) ** 2, axis=1)
        freqs = np.fft.rfftfreq(n, 1.0 / self.rate)
        dom = float(freqs[int(np.argmax(spec[1:])) + 1]) if len(spec) > 1 else None
        # Spectral concentration: share of tool-band power in the strongest 10 % of
        # frequency bins. A motor or a percussive tool puts its power in a few lines
        # (fundamental + harmonics); a handshake jolt or a knock is broadband.
        band = spec[freqs >= self.cfg.tool_band_low_hz]
        total = float(np.sum(band))
        k = max(3, int(round(0.1 * len(band))))
        conc = float(np.sum(np.sort(band)[-k:]) / total) if total > 0 and len(band) else 0.0
        return {"tool": tool, "motion": motion, "dominant_hz": dom, "conc": conc}

    # -- state machine and time accounting ---------------------------------
    # -- per-wearer tool calibration -----------------------------------------
    # Three captures: STILL (noise floor), MOVEMENT (handshakes, waving, shaking,
    # tapping: everything that must NOT count) and TOOL (the tool running in the
    # hand). Thresholds are placed between the movement and tool distributions
    # for each feature (tool-band RMS, tool/motion ratio, spectral concentration),
    # then the combined rule is re-checked on the captured windows. It is
    # accepted only if it keeps most tool windows and rejects movement, including
    # any run of movement candidates long enough to start a bout (on_hold_s).
    # The calibration belongs to the mounting and the tool, so it survives device
    # reboots; tool_calibration_reset() restores the defaults.
    PHASES = ("still", "movement", "tool")

    def _cal_init(self) -> None:
        self.default_cfg = self.cfg
        self.cal_state = "uncalibrated"
        self.cal_source = "default"
        self.cal_captures: dict[str, list[tuple]] = {}
        self.cal_result: dict | None = None
        self._cal_phase: str | None = None
        self._cal_elapsed = 0.0
        self.events: list[tuple[str, dict]] = []

    def _cal_duration(self, phase: str) -> float:
        return {"still": self.cfg.cal_still_s, "movement": self.cfg.cal_movement_s,
                "tool": self.cfg.cal_tool_s}[phase]

    def _cal_emit(self, **detail) -> None:
        self.events.append(("tool_calibration_state", {"state": self.cal_state, **detail}))

    def calibration_start(self, phase: str) -> None:
        if phase not in self.PHASES:
            self.events.append(("tool_calibration_rejected", {"reason": "unknown_phase",
                                                              "phase": phase}))
            return
        if phase == "tool" and "movement" not in self.cal_captures:
            self.events.append(("tool_calibration_rejected",
                                {"reason": "movement_capture_required_first", "phase": phase}))
            return
        self._cal_phase = phase
        self._cal_elapsed = 0.0
        self.cal_captures.pop(phase, None)
        self._cal_buf: list[tuple] = []
        self.cal_state = f"capturing_{phase}"
        self._cal_emit(phase=phase, duration_s=self._cal_duration(phase))

    def calibration_cancel(self) -> None:
        if self.cal_state.startswith("capturing"):
            self._cal_phase = None
            self.cal_state = "cancelled"
            self._cal_emit()

    def calibration_reset(self) -> None:
        """Back to the default thresholds; captures discarded."""
        self._cal_phase = None
        self.cfg = self.default_cfg
        self.cal_state, self.cal_source = "uncalibrated", "default"
        self.cal_captures, self.cal_result = {}, None
        self._cal_emit(reason="reset_to_defaults")

    def calibration_left_s(self) -> float | None:
        if not self.cal_state.startswith("capturing") or self._cal_phase is None:
            return None
        return max(0.0, self._cal_duration(self._cal_phase) - self._cal_elapsed)

    def _cal_fail(self, reason: str, **detail) -> None:
        self._cal_phase = None
        self.cal_state = "failed"
        self._cal_emit(reason=reason, **detail)

    def _cal_observe(self, f: dict | None, dt: float) -> None:
        phase = self._cal_phase
        if phase is None:
            return
        if f is None:
            if self._cal_elapsed > 0:
                self._cal_fail("interrupted_data_gap", phase=phase)
            return
        self._cal_elapsed += dt
        if self._cal_elapsed > self.cfg.cal_discard_s:
            self._cal_buf.append((dt, f["tool"], f["motion"], f["conc"], f["dominant_hz"]))
        if self._cal_elapsed < self._cal_duration(phase):
            return
        self.cal_captures[phase] = self._cal_buf
        self._cal_phase = None
        if len(self._cal_buf) < 10:
            self._cal_fail("insufficient_data", phase=phase)
            return
        summ = _summary(self._cal_buf)
        if phase == "tool":
            self._cal_solve()
        else:
            self.cal_state = f"{phase}_captured"
            self._cal_emit(phase=phase, summary=summ)

    def _cal_solve(self) -> None:
        cfg = self.cfg
        mv = np.array([r[1:4] for r in self.cal_captures["movement"]])     # tool, motion, conc
        tl = np.array([r[1:4] for r in self.cal_captures["tool"]])
        st = (np.array([r[1:4] for r in self.cal_captures["still"]])
              if "still" in self.cal_captures else None)
        summaries = {p: _summary(v) for p, v in self.cal_captures.items()}
        # still: median/p90 so one bump during the still capture does not spoil the baseline
        floor = 0.3 if st is None else max(0.3, 3.0 * float(np.percentile(st[:, 0], 90)))
        # Sanity: the captures must contain what they claim.
        if float(np.median(tl[:, 0])) < max(floor, 0.5):
            return self._cal_fail("no_tool_vibration_detected", summaries=summaries)
        if st is not None and float(np.median(mv[:, 1])) < max(0.3, 3 * float(np.median(st[:, 1]))):
            return self._cal_fail("no_movement_detected", summaries=summaries)

        feats = {
            "tool_band": (mv[:, 0], tl[:, 0]),
            "ratio": (mv[:, 0] / np.maximum(mv[:, 1], 1e-6), tl[:, 0] / np.maximum(tl[:, 1], 1e-6)),
            "concentration": (mv[:, 2], tl[:, 2]),
        }
        thr, separates = {}, {}
        for name, (m, t) in feats.items():
            m_hi, t_lo = float(np.percentile(m, 95)), float(np.percentile(t, 15))
            separates[name] = t_lo > m_hi
            if separates[name]:     # between the two: geometric for RMS/ratio, linear for 0..1
                thr[name] = (m_hi + t_lo) / 2 if name == "concentration" else math.sqrt(m_hi * t_lo)
            else:                   # does not separate: keep it loose so it never blocks the tool
                thr[name] = 0.8 * float(np.percentile(t, 10))
        on = max(thr["tool_band"], floor)
        new = _replace(cfg, on_threshold_ms2=round(on, 3),
                       off_threshold_ms2=round(max(min(0.6 * on, on), min(floor, on)), 3),
                       tool_to_motion_min=round(thr["ratio"], 3),
                       concentration_min=round(min(max(thr["concentration"], 0.0), 1.0), 3))

        def passes(a: np.ndarray) -> np.ndarray:
            return ((a[:, 0] >= new.on_threshold_ms2)
                    & (a[:, 0] >= new.tool_to_motion_min * a[:, 1])
                    & (a[:, 2] >= new.concentration_min))
        mp, tp = passes(mv), passes(tl)
        # Replay the movement capture through the on-hold + steadiness rule:
        # would any stretch of it have started a bout?
        longest, false_bouts = 0.0, 0
        run, doms = 0.0, deque()
        for ok, row in zip(mp, self.cal_captures["movement"]):
            d, dom = row[0], row[4]
            if not ok:
                run, doms = 0.0, deque()
                continue
            run += d
            doms.append((d, dom))
            while doms and sum(x for x, _ in doms) - doms[0][0] >= cfg.on_hold_s:
                doms.popleft()
            longest = max(longest, run)
            hz = [h for _, h in doms if h is not None]
            med = float(np.median(hz)) if hz else 0.0
            tol = max(cfg.steady_tol_hz, 0.1 * med)
            steady = sum(abs(h - med) <= tol for h in hz) / len(hz) if hz else 0.0
            if run >= cfg.on_hold_s and steady >= cfg.steady_min:
                false_bouts += 1
                run, doms = 0.0, deque()
        evaluation = {
            "tool_windows_accepted": round(float(tp.mean()), 3),
            "movement_windows_accepted": round(float(mp.mean()), 3),
            "movement_longest_candidate_run_s": round(longest, 2),
            "movement_false_bouts": false_bouts,
            "on_hold_s": cfg.on_hold_s,
            "separates": separates,
        }
        proposed = {"on_threshold_ms2": new.on_threshold_ms2,
                    "off_threshold_ms2": new.off_threshold_ms2,
                    "tool_to_motion_min": new.tool_to_motion_min,
                    "concentration_min": new.concentration_min}
        self.cal_result = {"summaries": summaries, "evaluation": evaluation, "thresholds": proposed}
        if evaluation["tool_windows_accepted"] < cfg.cal_tool_pass_min:
            return self._cal_fail("tool_not_reliably_detected", **self.cal_result)
        if evaluation["movement_windows_accepted"] > cfg.cal_movement_pass_max or false_bouts:
            return self._cal_fail("movement_looks_like_tool", **self.cal_result)
        self.cfg = new
        self.cal_state, self.cal_source = "calibrated", "calibrated"
        self._cal_emit(**self.cal_result)

    def calibration_record(self) -> dict | None:
        return None if self.cal_source != "calibrated" else dict(self.cal_result or {})

    def load_calibration(self, record: dict) -> None:
        th = (record or {}).get("thresholds") or {}
        keys = ("on_threshold_ms2", "off_threshold_ms2", "tool_to_motion_min", "concentration_min")
        if not all(isinstance(th.get(k), (int, float)) for k in keys):
            self.events.append(("tool_calibration_rejected", {"reason": "invalid_record"}))
            return
        new = _replace(self.default_cfg, **{k: float(th[k]) for k in keys})
        new.validate()
        self.cfg, self.cal_result = new, dict(record)
        self.cal_state, self.cal_source = "calibrated", "calibrated"
        self._cal_emit(reason="loaded", **self.cal_result)

    def reset_session(self) -> None:
        self.state = "no_tool"
        self.session_tool_s = 0.0
        self.session_observed_s = 0.0
        self.bouts = 0
        self.bout_s = 0.0
        self.longest_bout_s = 0.0
        self._cand_s = 0.0
        self._doms: deque[tuple[float, float | None]] = deque()
        self._hist: deque[tuple[float, bool]] = deque()   # (dt, active) over off_hold_s
        self._unobserved_s = 0.0
        self._last_t: float | None = None
        self._grip: float | None = None

    def on_boot_change(self) -> None:
        self._last_t = None                       # device time restarts: never subtract across boots
        if self.cal_state.startswith("capturing"):
            self._cal_fail("interrupted_boot_change")

    def update_grip(self, normalized: dict | None) -> None:
        v = None
        if normalized and normalized.get("status") in ("ok", "degraded") and normalized.get("value"):
            vals = [x for x in normalized["value"].values() if x is not None]
            v = max(vals) if vals else None
        self._grip = v

    def update(self, t_us: float | None) -> dict:
        cfg = self.cfg
        dt = 0.0
        if t_us is not None:
            if self._last_t is not None and t_us > self._last_t:
                dt = (t_us - self._last_t) / 1e6
            self._last_t = t_us
        f = self._features() if self.config_error is None and self.rate is not None else None
        if dt > cfg.max_step_s:                   # long gap: unobserved, not tool time
            self._unobserved_s += dt
            dt = 0.0
        if self.cal_state.startswith("capturing"):
            self._cal_observe(f, dt)
        if f is None:
            self._unobserved_s += dt
            if self.state == "tool" and self._unobserved_s >= cfg.off_hold_s:
                self._end_bout()
            self._cand_s = 0.0
            return self._record("unknown", None)

        self._unobserved_s = 0.0
        self.session_observed_s += dt
        ratio_ok = (f["tool"] >= cfg.tool_to_motion_min * f["motion"]
                    and f["conc"] >= cfg.concentration_min)
        candidate = f["tool"] >= cfg.on_threshold_ms2 and ratio_ok
        # "active" = tool vibration present (lower threshold, same ratio and
        # concentration tests, so
        # knocks during vigorous arm movement do not keep a bout alive)
        active = f["tool"] >= cfg.off_threshold_ms2 and ratio_ok
        steady = None
        if self.state == "no_tool":
            self._cand_s = self._cand_s + dt if candidate else 0.0
            if candidate:
                self._doms.append((dt, f["dominant_hz"]))
                while self._doms and sum(d for d, _ in self._doms) - self._doms[0][0] >= cfg.on_hold_s:
                    self._doms.popleft()
            else:
                self._doms.clear()
            steady = self._steadiness()
            if candidate and self._cand_s >= cfg.on_hold_s and (steady or 0.0) >= cfg.steady_min:
                self.state = "tool"
                self.bouts += 1
                self.bout_s = self._cand_s
                self.session_tool_s += self._cand_s   # back-fill the confirming time
                self._hist.clear()
        else:
            self.bout_s += dt
            if active:
                self.session_tool_s += dt             # only real tool vibration counts
            self._hist.append((dt, active))
            span = sum(d for d, _ in self._hist)
            while self._hist and span - self._hist[0][0] >= cfg.off_hold_s:
                span -= self._hist.popleft()[0]
            on = sum(d for d, a in self._hist if a)
            if span >= cfg.off_hold_s and on < 0.3 * span:
                self._end_bout()
        self.longest_bout_s = max(self.longest_bout_s, self.bout_s if self.state == "tool" else 0.0)
        return self._record(self.state, f, candidate, steady)

    def _steadiness(self) -> float | None:
        """Share of the recent candidate windows whose dominant frequency is within
        tolerance of their median: ~1 for a motor or tool, low for irregular motion."""
        doms = [h for _, h in self._doms if h is not None]
        if not doms:
            return None
        med = float(np.median(doms))
        tol = max(self.cfg.steady_tol_hz, 0.1 * med)
        return sum(abs(h - med) <= tol for h in doms) / len(doms)

    def _end_bout(self) -> None:
        self.state = "no_tool"
        self.longest_bout_s = max(self.longest_bout_s, self.bout_s)
        self.bout_s = 0.0
        self._hist.clear()
        self._doms.clear()
        self._cand_s = 0.0

    def _record(self, state: str, f: dict | None, candidate: bool = False,
                steady: float | None = None) -> dict:
        cfg = self.cfg
        warnings = []
        if self.state == "tool" and self.bout_s >= cfg.continuous_warn_s:
            warnings.append("continuous_tool_use_long")
        if self.session_tool_s >= cfg.session_warn_s:
            warnings.append("session_tool_time_high")
        elif self.session_tool_s >= cfg.session_notice_fraction * cfg.session_warn_s:
            warnings.append("session_tool_time_approaching")
        r = lambda v, d=3: None if v is None else round(float(v), d)  # noqa: E731
        reasons = []
        if f is None:
            if self.config_error:
                reasons.append("tool_use_config_invalid")
            elif self.rate is None:
                reasons.append("no_data")
            else:
                reasons.append("warming_up" if self.reset_reason == "configured"
                               else self.reset_reason)
        grip = None if self._grip is None else self._grip >= cfg.grip_activation_min
        return {
            "label": LABEL, "label_note": NOTE,
            "state": state,
            "status": "unavailable" if f is None else "ok",
            "reasons": reasons,
            "candidate": bool(candidate),
            "tool_band_rms_ms2": r(f and f["tool"]),
            "motion_band_rms_ms2": r(f and f["motion"]),
            "dominant_hz": r(f and f["dominant_hz"], 1),
            "spectral_concentration": r(f and f["conc"]),
            "steadiness": r(steady),
            "calibration": {"state": self.cal_state, "source": self.cal_source,
                            "capture_left_s": r(self.calibration_left_s(), 1)},
            "grip_activation": r(self._grip),
            "gripping": grip,
            "bout_s": r(self.bout_s if self.state == "tool" else 0.0, 2),
            "bouts": self.bouts,
            "longest_bout_s": r(max(self.longest_bout_s, self.bout_s if self.state == "tool" else 0.0), 2),
            "session_tool_s": r(self.session_tool_s, 2),
            "session_observed_s": r(self.session_observed_s, 2),
            "warnings": warnings,
            "thresholds": {
                "tool_band_low_hz": cfg.tool_band_low_hz,
                "motion_band_hz": list(cfg.motion_band_hz),
                "on_threshold_ms2": cfg.on_threshold_ms2,
                "off_threshold_ms2": cfg.off_threshold_ms2,
                "tool_to_motion_min": cfg.tool_to_motion_min,
                "concentration_min": cfg.concentration_min,
                "steady_min": cfg.steady_min, "steady_tol_hz": cfg.steady_tol_hz,
                "source": self.cal_source,
                "on_hold_s": cfg.on_hold_s, "off_hold_s": cfg.off_hold_s,
                "continuous_warn_s": cfg.continuous_warn_s,
                "session_warn_s": cfg.session_warn_s,
                "conversion_source": self._conv_source,
            },
        }


def _replace(cfg: ToolUseConfig, **kw) -> ToolUseConfig:
    import dataclasses
    new = dataclasses.replace(cfg, **kw)
    new.validate()
    return new


def _summary(rows) -> dict:
    """Per-capture numbers shown to the user (median and 95th percentile)."""
    a = np.array([r[1:4] for r in rows], dtype=float)
    if a.size == 0:
        return {}
    ratio = a[:, 0] / np.maximum(a[:, 1], 1e-6)
    q = lambda v, p: round(float(np.percentile(v, p)), 3)  # noqa: E731
    return {"windows": int(len(a)),
            "tool_band_ms2": {"median": q(a[:, 0], 50), "p95": q(a[:, 0], 95)},
            "motion_band_ms2": {"median": q(a[:, 1], 50), "p95": q(a[:, 1], 95)},
            "ratio": {"median": q(ratio, 50), "p95": q(ratio, 95)},
            "concentration": {"median": q(a[:, 2], 50), "p95": q(a[:, 2], 95)}}
