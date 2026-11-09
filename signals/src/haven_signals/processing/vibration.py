"""Causal streaming vibration features from the accelerometer stream.

Quantities are kept distinct:

* ``static_mean`` -- mean raw acceleration over the window, per axis. Includes
  gravity and orientation. This is NOT vibration.
* ``dynamic_rms`` -- per-axis RMS of the high-passed acceleration over the
  window. The causal Butterworth high-pass (``highpass_hz``, default 5 Hz)
  removes gravity, orientation, and slow arm motion.
* ``combined_rms`` -- root-sum-of-squares of the per-axis dynamic RMS
  values: the RMS magnitude of the dynamic acceleration vector.

Passband: ``highpass_hz`` up to ``lowpass_hz`` if set, otherwise up to the
sensor's own bandwidth (internal filter and ODR; unknown until the IMU and
its configuration are confirmed, and at most Nyquist of the actual rate).
The features are unweighted: no ISO 5349 frequency weighting is applied or
implied. A bandwidth-limited hobby IMU cannot provide a standards-compliant
hand-arm vibration measurement.

Units: features are computed in input units (``raw_counts`` or ``m/s^2``).
Physical values (m/s^2) are reported only when the batch already carries
m/s^2 (device-declared conversion) or the configuration provides a
conversion with a cited source. Otherwise ``physical`` is unavailable
(``no_conversion``); nothing is guessed.

State resets on every stream reset (gap, timing break, rate change or
mismatch, reboot, disconnect, stale, provenance change) and on any null row.
Missing samples are never imputed. Clipped samples (saturated accelerometer,
which underestimates vibration) make the result ``degraded``.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.signal import butter, sosfilt

from .config import VibrationConfig

ACCEL = ("ax", "ay", "az")
WEIGHTING = "none (unweighted; not ISO 5349 frequency-weighted)"
LABEL = "measured_vibration_within_sensor_bandwidth"


class VibrationPipeline:
    def __init__(self, cfg: VibrationConfig, conversion: dict | None, conversion_source: str | None):
        self.cfg = cfg
        self._cfg_conversion = conversion
        self._cfg_conversion_source = conversion_source
        self.rate: float | None = None
        self.channels: list[str] | None = None
        self.units: dict[str, str] | None = None
        self.config_error: str | None = None
        self._idx: list[int] = []
        self._axes: list[str] = []
        self._sos = None
        self._n_win = self._n_settle = 0
        self.reset("no_data")

    def configure(self, rate: float, channels: list[str], units: dict[str, str]) -> bool:
        if (rate, channels, units) == (self.rate, self.channels, self.units):
            return False
        self.rate, self.channels, self.units = rate, list(channels), dict(units)
        self._idx = [i for i, ch in enumerate(channels) if ch in ACCEL]
        self._axes = [channels[i] for i in self._idx]
        self.config_error = None
        self._sos = None
        if not self._axes:
            self.config_error = "no accelerometer channels (ax/ay/az) in the IMU stream"
        else:
            self.config_error = self.cfg.check(rate)
        if self.config_error is None:
            if self.cfg.lowpass_hz is None:
                self._sos = butter(self.cfg.order, self.cfg.highpass_hz, btype="highpass",
                                   fs=rate, output="sos")
            else:
                self._sos = butter(self.cfg.order, [self.cfg.highpass_hz, self.cfg.lowpass_hz],
                                   btype="bandpass", fs=rate, output="sos")
        self._n_win = max(1, round(self.cfg.window_s * rate))
        self._n_settle = round(self.cfg.settle_s * rate)
        self.reset("configured")
        return True

    def reset(self, reason: str) -> None:
        n = len(self._axes)
        self._x0: np.ndarray | None = None
        self._zi: np.ndarray | None = None
        self._since = 0
        self._dyn = np.empty((0, n))
        self._raw = np.empty((0, n))
        self._clip = np.empty(0, dtype=bool)
        self.reset_reason = reason

    def process(self, rows: list[list], clipped: list[bool | None], device_clipped: bool) -> None:
        if self.config_error is not None or self.rate is None:
            return
        seg, seg_clip = [], []
        for row, c in zip(rows, clipped):
            vals = [row[i] for i in self._idx]
            if any(v is None for v in vals):
                if seg:
                    self._segment(seg, seg_clip)
                    seg, seg_clip = [], []
                self.reset("missing_values")
                continue
            seg.append(vals)
            seg_clip.append(bool(c) or device_clipped)
        if seg:
            self._segment(seg, seg_clip)

    def _segment(self, rows: list[list], clipped: list[bool]) -> None:
        x = np.asarray(rows, dtype=float)
        clip = np.asarray(clipped, dtype=bool)
        if self._x0 is None:
            self._x0 = x[0].copy()  # remove the initial static level so the high-pass starts quietly
        if self._zi is None:
            self._zi = np.zeros((self._sos.shape[0], 2, x.shape[1]))  # type: ignore[union-attr]
        dyn, self._zi = sosfilt(self._sos, x - self._x0, axis=0, zi=self._zi)
        n = len(x)
        before = self._since
        self._since += n
        first = max(0, self._n_settle - before)
        if first < n:
            self._dyn = np.concatenate([self._dyn, dyn[first:]])[-self._n_win:]
            self._raw = np.concatenate([self._raw, x[first:]])[-self._n_win:]
            self._clip = np.concatenate([self._clip, clip[first:]])[-self._n_win:]

    def _physical(self) -> tuple[list[float] | None, str | None, str]:
        """(scale per axis, source, reason-if-unavailable)."""
        units = [self.units[a] for a in self._axes]  # type: ignore[index]
        if all(u == "m/s^2" for u in units):
            return [1.0] * len(units), "device_declared_conversion", ""
        conv = self._cfg_conversion
        if all(u == "raw_counts" for u in units) and conv and all(a in conv for a in self._axes):
            return [float(conv[a][0]) for a in self._axes], self._cfg_conversion_source, ""
        return None, None, "no_conversion"

    def state(self) -> dict:
        base = {"label": LABEL, "weighting": WEIGHTING, "window_s": self.cfg.window_s,
                "passband_hz": {"low": self.cfg.highpass_hz,
                                "high": self.cfg.lowpass_hz,
                                "high_note": None if self.cfg.lowpass_hz else
                                "sensor-limited (internal filter/ODR unknown); at most Nyquist "
                                + (f"{self.rate / 2:g} Hz" if self.rate else "of the actual rate")}}
        empty = {"static_mean": None, "dynamic_rms": None, "combined_rms": None,
                 "physical": {"status": "unavailable", "reasons": [], "units": "m/s^2",
                              "dynamic_rms": None, "combined_rms": None, "conversion_source": None}}
        if self.config_error is not None:
            return {**base, "status": "unavailable", "reasons": ["vibration_config_invalid"],
                    "detail": self.config_error, "units": None, **empty}
        units = {a: self.units[a] for a in self._axes} if self.units else None  # type: ignore[index]
        if self.rate is None or self._since == 0:
            reason = self.reset_reason if self.reset_reason not in ("configured",) else "no_data"
            out = {**base, "status": "unavailable", "reasons": [reason], "units": units, **empty}
            out["physical"]["reasons"] = [reason]
            return out
        if len(self._dyn) < self._n_win:
            reasons = ["warming_up"]
            if self.reset_reason not in ("no_data", "configured"):
                reasons.append(self.reset_reason)
            out = {**base, "status": "warming_up", "reasons": reasons, "units": units, **empty}
            out["physical"]["status"] = "warming_up"
            out["physical"]["reasons"] = list(reasons)
            return out

        rms = np.sqrt(np.mean(self._dyn ** 2, axis=0))
        mean = np.mean(self._raw, axis=0)
        combined = float(math.sqrt(float(np.sum(rms ** 2))))
        reasons = ["clipped"] if self._clip.any() else []
        status = "degraded" if reasons else "ok"
        r = lambda v: round(float(v), 4)  # noqa: E731
        out = {**base, "status": status, "reasons": reasons, "units": units,
               "static_mean": {a: r(m) for a, m in zip(self._axes, mean)},
               "dynamic_rms": {a: r(v) for a, v in zip(self._axes, rms)},
               "combined_rms": r(combined)}
        scale, source, why = self._physical()
        if scale is None:
            out["physical"] = {**empty["physical"], "reasons": [why]}
        else:
            prms = [abs(s) * v for s, v in zip(scale, rms)]
            out["physical"] = {"status": status, "reasons": list(reasons), "units": "m/s^2",
                               "dynamic_rms": {a: r(v) for a, v in zip(self._axes, prms)},
                               "combined_rms": r(math.sqrt(sum(v * v for v in prms))),
                               "conversion_source": source}
        return out
