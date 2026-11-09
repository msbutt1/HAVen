"""Staircase rule: pure logic, no clock, no hardware, no I/O.

Prototype protocol, not a validated medical procedure::

    felt      -> next stimulus weaker   (setting - step)
    not felt  -> next stimulus stronger (setting + step)

A *reversal* is a change of direction between consecutive responses. Its level
is the setting of the trial whose response flipped the direction (the peak or
valley). The first response only sets the direction. Catch trials and missing
responses never reach this class, so they cannot move the staircase.

Stop after ``reversals_to_stop`` reversals (full: 8). Threshold = mean of the
last ``reversals_averaged`` reversal levels (full: 6), in motor setting units.
At the ends of the setting range the level is clamped. Repeated responses that
cannot move further (felt at the lowest setting, not felt at the highest) end the
test with no threshold instead of looping.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import StationConfig


@dataclass(frozen=True)
class StepOutcome:
    direction: str  # "down" after felt, "up" after not felt
    reversal: bool
    reversal_level: int | None
    next_setting: int
    at_boundary: bool  # the response could not move the level any further


class Staircase:
    def __init__(self, cfg: StationConfig):
        cfg.validate()
        self.cfg = cfg
        self.setting = cfg.start_setting
        self.direction: str | None = None
        self.reversal_levels: list[int] = []
        self.boundary_repeats = 0
        self.boundary_kind: str | None = None  # "floor" | "ceiling"

    def record(self, felt: bool) -> StepOutcome:
        """Apply a received response to the trial at ``self.setting``."""
        level = self.setting
        direction = "down" if felt else "up"
        reversal = self.direction is not None and direction != self.direction
        if reversal:
            self.reversal_levels.append(level)
        self.direction = direction
        step = -self.cfg.step if felt else self.cfg.step
        nxt = min(self.cfg.max_setting, max(self.cfg.min_setting, level + step))
        at_boundary = nxt == level
        kind = None
        if at_boundary:
            kind = "floor" if felt else "ceiling"
        if kind is not None and kind == self.boundary_kind:
            self.boundary_repeats += 1
        elif kind is not None:
            self.boundary_repeats = 1
        else:
            self.boundary_repeats = 0
        self.boundary_kind = kind
        self.setting = nxt
        return StepOutcome(direction, reversal, level if reversal else None, nxt, at_boundary)

    @property
    def reversals(self) -> int:
        return len(self.reversal_levels)

    def stop_reason(self) -> str | None:
        """``threshold_estimated``, ``floor_reached``, ``ceiling_reached`` or None."""
        if self.reversals >= self.cfg.reversals_to_stop:
            return "threshold_estimated"
        if self.boundary_repeats >= self.cfg.max_boundary_repeats:
            return f"{self.boundary_kind}_reached"
        return None

    def threshold(self) -> float | None:
        """Mean of the last ``reversals_averaged`` reversal levels, once enough exist."""
        n = self.cfg.reversals_averaged
        if self.reversals < self.cfg.reversals_to_stop or self.reversals < n:
            return None
        used = self.reversal_levels[-n:]
        return round(sum(used) / len(used), 4)

    def levels_used(self) -> list[int]:
        return list(self.reversal_levels[-self.cfg.reversals_averaged:])
