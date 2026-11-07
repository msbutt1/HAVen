"""Processing configuration. Unknown hardware values stay explicitly ``None``.

``None`` never means zero or a default guess. It means *unresolved*, and the
processor records where every value it actually used came from (``config``,
``device_hello``, ``batch_declared``, or ``unknown``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Mapping

CLOCKS = ("device", "host")


@dataclass(frozen=True)
class StreamConfig:
    window_s: float
    warmup_s: float | None = None  # None -> equal to window_s
    min_coverage: float = 0.9  # fraction of expected samples that must be valid
    # Hardware facts. None = UNKNOWN (not yet confirmed by Collins / measurement).
    expected_rate_hz: float | None = None  # None -> use device-declared nominal rate
    raw_limits: tuple[int, int] | None = None  # None -> device hello limits, else unknown
    use_device_raw_limits: bool = True
    conversion: Mapping[str, tuple[float, float]] | None = None  # channel -> (scale, offset)
    # Bound on buffer capacity: samples kept <= window_s * ceiling + one max batch.
    buffer_rate_ceiling_factor: float = 2.0

    @property
    def effective_warmup_s(self) -> float:
        return self.window_s if self.warmup_s is None else self.warmup_s

    def validate(self, name: str) -> None:
        if not 0 < self.window_s <= 10:
            raise ValueError(f"{name}.window_s must be in (0, 10] s")
        if self.warmup_s is not None and not 0 <= self.warmup_s <= 60:
            raise ValueError(f"{name}.warmup_s must be in [0, 60] s")
        if not 0 < self.min_coverage <= 1:
            raise ValueError(f"{name}.min_coverage must be in (0, 1]")
        if self.expected_rate_hz is not None and not self.expected_rate_hz > 0:
            raise ValueError(f"{name}.expected_rate_hz must be > 0 or None (unknown)")
        if self.raw_limits is not None and not self.raw_limits[0] < self.raw_limits[1]:
            raise ValueError(f"{name}.raw_limits must be (min, max) with min < max, or None")
        if self.buffer_rate_ceiling_factor < 1:
            raise ValueError(f"{name}.buffer_rate_ceiling_factor must be >= 1")


@dataclass(frozen=True)
class ProcessingConfig:
    emg: StreamConfig = field(default_factory=lambda: StreamConfig(window_s=0.1))
    imu: StreamConfig = field(default_factory=lambda: StreamConfig(window_s=0.5))
    # A stream with no new data for this long is stale; its features become unavailable.
    stale_after_s: float = 0.25
    # "device": staleness judged against the newest device time seen on any stream
    #           (deterministic; for files, replay, tests).
    # "host":   staleness judged against caller-supplied host receive times + poll().
    clock: str = "device"
    # Allowed inter-batch timing error, as a fraction of one sample interval.
    continuity_tolerance: float = 0.5
    recent_seq_memory: int = 256
    retired_boot_memory: int = 8

    def validate(self) -> None:
        self.emg.validate("emg")
        self.imu.validate("imu")
        if self.clock not in CLOCKS:
            raise ValueError(f"clock must be one of {CLOCKS}")
        if not self.stale_after_s > 0:
            raise ValueError("stale_after_s must be > 0")
        if not 0 < self.continuity_tolerance < 1:
            raise ValueError("continuity_tolerance must be in (0, 1)")
        if self.recent_seq_memory < 1 or self.retired_boot_memory < 1:
            raise ValueError("memories must be >= 1")

    def stream(self, name: str) -> StreamConfig:
        return self.emg if name == "emg" else self.imu

    def to_dict(self) -> dict:
        return asdict(self)
