"""Placeholder window features (algorithm layer; no I/O, no state).

``window_stats`` is deliberately simple: per-channel mean and AC RMS (standard
deviation about the window mean) in the input units. It exists so the
pipeline, availability, and quality semantics can be exercised before the
real EMG envelope (Prompt 4) and vibration features (Prompt 5) exist. It is
not a muscle-activation or vibration-exposure measure.
"""

from __future__ import annotations

import math
from typing import Iterable

from .window import WindowSample

DECIMALS = 4


def _r(x: float) -> float:
    return round(x, DECIMALS)


def window_stats(samples: Iterable[WindowSample], channels: list[str]) -> dict:
    sums = [0.0] * len(channels)
    sq = [0.0] * len(channels)
    counts = [0] * len(channels)
    rows = list(samples)
    for s in rows:
        for c, v in enumerate(s.values):
            if v is not None:
                sums[c] += v
                counts[c] += 1
    means = [sums[c] / counts[c] if counts[c] else None for c in range(len(channels))]
    for s in rows:
        for c, v in enumerate(s.values):
            if v is not None:
                sq[c] += (v - means[c]) ** 2
    return {
        "mean": {ch: (_r(means[c]) if counts[c] else None) for c, ch in enumerate(channels)},
        "ac_rms": {ch: (_r(math.sqrt(sq[c] / counts[c])) if counts[c] else None)
                   for c, ch in enumerate(channels)},
        "valid_values": {ch: counts[c] for c, ch in enumerate(channels)},
    }


def measured_rate_hz(samples: list[WindowSample]) -> float | None:
    """Rate from timestamps of all acquired rows (including rows with nulls)."""
    if len(samples) < 2:
        return None
    span = samples[-1].t_us - samples[0].t_us
    return _r((len(samples) - 1) * 1e6 / span) if span > 0 else None
