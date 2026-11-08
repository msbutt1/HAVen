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
        return {"tool": tool, "motion": motion, "dominant_hz": dom}

    # -- state machine and time accounting ---------------------------------
    def reset_session(self) -> None:
        self.state = "no_tool"
        self.session_tool_s = 0.0
        self.session_observed_s = 0.0
        self.bouts = 0
        self.bout_s = 0.0
        self.longest_bout_s = 0.0
        self._cand_s = 0.0
        self._hist: deque[tuple[float, bool]] = deque()   # (dt, active) over off_hold_s
        self._unobserved_s = 0.0
        self._last_t: float | None = None
        self._grip: float | None = None

    def on_boot_change(self) -> None:
        self._last_t = None                       # device time restarts: never subtract across boots

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
        if f is None:
            self._unobserved_s += dt
            if self.state == "tool" and self._unobserved_s >= cfg.off_hold_s:
                self._end_bout()
            self._cand_s = 0.0
            return self._record("unknown", None)

        self._unobserved_s = 0.0
        self.session_observed_s += dt
        ratio_ok = f["tool"] >= cfg.tool_to_motion_min * f["motion"]
        candidate = f["tool"] >= cfg.on_threshold_ms2 and ratio_ok
        # "active" = tool vibration present (lower threshold, same ratio test, so
        # knocks during vigorous arm movement do not keep a bout alive)
        active = f["tool"] >= cfg.off_threshold_ms2 and ratio_ok
        if self.state == "no_tool":
            self._cand_s = self._cand_s + dt if candidate else 0.0
            if candidate and self._cand_s >= cfg.on_hold_s:
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
        return self._record(self.state, f, candidate)

    def _end_bout(self) -> None:
        self.state = "no_tool"
        self.longest_bout_s = max(self.longest_bout_s, self.bout_s)
        self.bout_s = 0.0
        self._hist.clear()
        self._cand_s = 0.0

    def _record(self, state: str, f: dict | None, candidate: bool = False) -> dict:
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
                "on_hold_s": cfg.on_hold_s, "off_hold_s": cfg.off_hold_s,
                "continuous_warn_s": cfg.continuous_warn_s,
                "session_warn_s": cfg.session_warn_s,
                "conversion_source": self._conv_source,
            },
        }
