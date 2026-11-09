"""Bounded, time-based sample window (algorithm-independent storage)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class WindowSample:
    t_us: float  # device time
    values: tuple[int | float | None, ...]  # None = missing value
    clipped: bool | None  # None = clipping unknown (no raw limits)


class SampleWindow:
    """Holds samples newer than ``length_us`` before the latest sample.

    The deque has a hard ``capacity``. If samples arrive faster than the
    capacity assumes, the oldest are discarded and ``overflow_count`` grows,
    so memory stays bounded whatever the input rate.
    """

    def __init__(self, length_us: float, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.length_us = length_us
        self.capacity = capacity
        self._buf: deque[WindowSample] = deque()
        self.overflow_count = 0
        self.max_occupancy = 0

    def clear(self) -> None:
        self._buf.clear()

    def append(self, sample: WindowSample) -> bool:
        """Append; returns True if an old sample had to be discarded for capacity."""
        overflow = len(self._buf) >= self.capacity
        if overflow:
            self._buf.popleft()
            self.overflow_count += 1
        self._buf.append(sample)
        self.max_occupancy = max(self.max_occupancy, len(self._buf))
        return overflow

    def evict_older_than(self, t_end_us: float) -> None:
        cut = t_end_us - self.length_us
        while self._buf and self._buf[0].t_us <= cut:
            self._buf.popleft()

    def __len__(self) -> int:
        return len(self._buf)

    def __iter__(self) -> Iterator[WindowSample]:
        return iter(self._buf)
