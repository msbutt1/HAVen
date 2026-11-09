"""Causal streaming EMG envelope: offset removal -> band-pass -> moving RMS.

Pipeline (per channel, all causal, state carried across batches):

1. Offset removal. ``ema``: subtract a slow running mean (time constant
   ``offset_tau_s``), initialised to the first sample after a reset so the
   ADC mid-scale offset does not kick the band-pass. ``fixed``: subtract a
   configured value. ``none``: rely on the band-pass alone.
2. Butterworth band-pass as second-order sections (``scipy.signal.butter``
   with ``output='sos'``, filtered with ``sosfilt`` and carried ``zi``). The
   cutoffs are validated against the actual stream rate; an invalid pair
   makes the stream unavailable (``filter_config_invalid``) rather than being
   silently clamped.
3. Moving RMS over ``envelope_window_s`` (default 100 ms, a common choice for
   surface-EMG envelopes) of filtered samples. The first ``settle_s`` of
   filter output after every reset is excluded from the window.

Filter and envelope state reset on every discontinuity the processor reports
(gap, timing break, rate change or mismatch, reboot, disconnect, stale,
provenance change) and on any null sample. Missing samples are never imputed.
The envelope is warming_up until ``settle_s + envelope_window_s`` of
contiguous valid samples have been processed since the last reset.

This is a relative signal-processing proxy in input units (``<unit>_rms``),
not force and not %MVC.

INPUT ASSUMPTION: the EMG front end delivers RAW (unrectified, unsmoothed)
EMG. If the confirmed module outputs an already rectified/smoothed envelope,
this band-pass + RMS path is wrong for that signal. Confirming the output type
is a hardware bring-up requirement; no alternative pipeline is implemented.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy.signal import butter, lfilter, sosfilt

from .config import EmgProcessingConfig

INPUT_ASSUMPTION = "raw_emg"


@dataclass(frozen=True)
class EnvelopeState:
    status: str  # ok | degraded | warming_up | unavailable
    reasons: tuple[str, ...]
    values: dict[str, float] | None
    units: dict[str, str] | None
    samples_total: int  # samples processed since the pipeline was created
    clipped_total: int  # of which clipped (host-detected or device-flagged)
    detail: str | None = None


class EmgPipeline:
    def __init__(self, cfg: EmgProcessingConfig):
        self.cfg = cfg
        self.rate: float | None = None
        self.channels: list[str] | None = None
        self.units: dict[str, str] | None = None
        self.config_error: str | None = None
        self.signature: str | None = None
        self.samples_total = 0
        self.clipped_total = 0
        self._sos = None
        self._n_env = self._n_settle = 0
        self._alpha = 0.0
        self.reset_listener = None  # called with the reason on every reset (calibration capture)
        self.reset("no_data")

    # -- configuration --------------------------------------------------------

    def configure(self, rate: float, channels: list[str], units: dict[str, str]) -> bool:
        """(Re)configure for a stream; returns True if anything changed (state reset)."""
        if (rate, channels, units) == (self.rate, self.channels, self.units):
            return False
        self.rate, self.channels, self.units = rate, list(channels), dict(units)
        bp = self.cfg.bandpass
        self.config_error = bp.check(rate) if bp is not None else None
        self._sos = None
        if bp is not None and self.config_error is None:
            self._sos = butter(bp.order, [bp.low_hz, bp.high_hz], btype="bandpass",
                               fs=rate, output="sos")
        self._n_env = max(1, round(self.cfg.envelope_window_s * rate))
        self._n_settle = round(self.cfg.settle_s * rate)
        self._alpha = math.exp(-1.0 / (rate * self.cfg.offset_tau_s))
        ident = {"rate_hz": rate, "channels": self.channels, "units": self.units,
                 "emg_processing": asdict(self.cfg)}
        self.signature = hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:16]
        self.reset("configured")
        return True

    def describe(self) -> dict:
        """Effective configuration, for calibration records and documentation."""
        return {"input_assumption": INPUT_ASSUMPTION,
                "rate_hz": self.rate, "channels": self.channels, "units": self.units,
                "envelope_samples": self._n_env, "settle_samples": self._n_settle,
                "emg_processing": asdict(self.cfg)}

    # -- state ----------------------------------------------------------------

    def reset(self, reason: str) -> None:
        nch = len(self.channels) if self.channels else 0
        self._ema: np.ndarray | None = None
        self._zi: np.ndarray | None = None
        self._since = 0
        self._tail = np.empty((0, nch))
        self._tail_clip = np.empty(0, dtype=bool)
        self.reset_reason = reason
        if getattr(self, "reset_listener", None) is not None:
            self.reset_listener(reason)

    def process(self, rows: list[list], clipped: list[bool | None], device_clipped: bool) -> None:
        """Feed one batch of contiguous rows (None values = missing)."""
        if self.config_error is not None or self.rate is None:
            return
        seg: list[list] = []
        seg_clip: list[bool] = []
        for row, c in zip(rows, clipped):
            if any(v is None for v in row):
                if seg:
                    self._segment(seg, seg_clip)
                    seg, seg_clip = [], []
                self.reset("missing_values")  # never filter across a hole
                continue
            seg.append(row)
            seg_clip.append(bool(c) or device_clipped)
        if seg:
            self._segment(seg, seg_clip)

    def _segment(self, rows: list[list], clipped: list[bool]) -> None:
        x = np.asarray(rows, dtype=float)
        clip = np.asarray(clipped, dtype=bool)
        n = len(x)
        self.samples_total += n
        self.clipped_total += int(clip.sum())

        mode = self.cfg.offset_mode
        if mode == "ema":
            a = self._alpha
            if self._ema is None:
                self._ema = x[0].copy()
            mean, _ = lfilter([1 - a], [1, -a], x, axis=0, zi=(a * self._ema)[None, :])
            self._ema = mean[-1]
            centred = x - mean
        elif mode == "fixed":
            centred = x - float(self.cfg.fixed_offset)  # type: ignore[arg-type]
        else:
            centred = x

        if self._sos is not None:
            if self._zi is None:
                self._zi = np.zeros((self._sos.shape[0], 2, x.shape[1]))
            filtered, self._zi = sosfilt(self._sos, centred, axis=0, zi=self._zi)
        else:
            filtered = centred

        before = self._since
        self._since += n
        first_settled = max(0, self._n_settle - before)  # index of first post-settle sample
        if first_settled < n:
            self._tail = np.concatenate([self._tail, filtered[first_settled:]])[-self._n_env:]
            self._tail_clip = np.concatenate([self._tail_clip, clip[first_settled:]])[-self._n_env:]

    def state(self) -> EnvelopeState:
        units = {ch: f"{u}_rms" for ch, u in self.units.items()} if self.units else None
        base = dict(units=units, samples_total=self.samples_total, clipped_total=self.clipped_total)
        if self.config_error is not None:
            return EnvelopeState("unavailable", ("filter_config_invalid",), None,
                                 detail=self.config_error, **base)
        if self.rate is None or (self._since == 0 and self.reset_reason in ("no_data", "configured")):
            return EnvelopeState("unavailable", ("no_data",), None, **base)
        if len(self._tail) < self._n_env:
            if self._since == 0:
                return EnvelopeState("unavailable", (self.reset_reason,), None, **base)
            reasons = ["warming_up"]
            if self.reset_reason not in ("no_data", "configured"):
                reasons.append(self.reset_reason)
            return EnvelopeState("warming_up", tuple(reasons), None, **base)
        rms = np.sqrt(np.mean(self._tail ** 2, axis=0))
        values = {ch: round(float(v), 4) for ch, v in zip(self.channels, rms)}  # type: ignore[arg-type]
        reasons = ("clipped",) if self._tail_clip.any() else ()
        return EnvelopeState("degraded" if reasons else "ok", reasons, values, **base)
