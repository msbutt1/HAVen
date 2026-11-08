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
    # channel -> (scale, offset) into the channel's physical unit (accel: m/s^2).
    # None = UNKNOWN: physical-unit features stay unavailable. Must cite its source.
    conversion: Mapping[str, tuple[float, float]] | None = None
    conversion_source: str | None = None
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
        if self.conversion is not None:
            if not self.conversion_source:
                raise ValueError(f"{name}.conversion requires conversion_source (where it came from)")
            for ch, pair in self.conversion.items():
                if len(pair) != 2 or not pair[0] or any(not isinstance(v, (int, float)) for v in pair):
                    raise ValueError(f"{name}.conversion[{ch}] must be (non-zero scale, offset)")
        if self.buffer_rate_ceiling_factor < 1:
            raise ValueError(f"{name}.buffer_rate_ceiling_factor must be >= 1")


OFFSET_MODES = ("ema", "fixed", "none")


@dataclass(frozen=True)
class EmgFilterConfig:
    """Causal Butterworth band-pass, applied as second-order sections.

    No default is silently adapted to the sample rate: if ``high_hz`` exceeds
    ``nyquist_headroom * fs / 2`` the EMG stream reports ``filter_config_invalid``
    instead of clamping. A 500 Hz EMG rate therefore needs e.g. high_hz <= 225.
    """
    low_hz: float = 20.0
    high_hz: float = 450.0
    order: int = 2  # per edge; band-pass order is 2 * order
    nyquist_headroom: float = 0.9

    def check(self, rate_hz: float) -> str | None:
        """Return None if valid at ``rate_hz``, else a human-readable reason."""
        nyq = rate_hz / 2
        if not 1 <= self.order <= 4:
            return "order must be 1..4"
        if not 0 < self.low_hz < self.high_hz:
            return f"need 0 < low_hz ({self.low_hz:g}) < high_hz ({self.high_hz:g})"
        if self.high_hz > self.nyquist_headroom * nyq:
            return (f"high_hz {self.high_hz:g} exceeds {self.nyquist_headroom:g} x Nyquist "
                    f"({self.nyquist_headroom * nyq:g} Hz at {rate_hz:g} Hz sampling)")
        return None


@dataclass(frozen=True)
class EmgProcessingConfig:
    """EMG envelope pipeline: offset removal -> band-pass -> moving RMS (all causal)."""
    offset_mode: str = "ema"  # "ema": slow running mean; "fixed": subtract fixed_offset; "none"
    offset_tau_s: float = 1.0
    fixed_offset: float | None = None
    bandpass: EmgFilterConfig | None = field(default_factory=EmgFilterConfig)  # None = no band-pass
    envelope_window_s: float = 0.1
    settle_s: float = 0.1  # filter output discarded after every reset before the envelope counts

    def validate(self) -> None:
        if self.offset_mode not in OFFSET_MODES:
            raise ValueError(f"emg_processing.offset_mode must be one of {OFFSET_MODES}")
        if self.offset_mode == "fixed" and self.fixed_offset is None:
            raise ValueError("emg_processing.fixed_offset is required when offset_mode='fixed'")
        if not 0.05 <= self.offset_tau_s <= 60:
            raise ValueError("emg_processing.offset_tau_s must be in [0.05, 60] s")
        if not 0.01 <= self.envelope_window_s <= 2:
            raise ValueError("emg_processing.envelope_window_s must be in [0.01, 2] s")
        if not 0 <= self.settle_s <= 5:
            raise ValueError("emg_processing.settle_s must be in [0, 5] s")
        if self.bandpass is not None and not 0 < self.bandpass.nyquist_headroom <= 1:
            raise ValueError("emg_processing.bandpass.nyquist_headroom must be in (0, 1]")


@dataclass(frozen=True)
class CalibrationConfig:
    """Relaxed/squeeze capture and reference estimation."""
    relaxed_s: float = 3.0
    squeeze_s: float = 3.0
    discard_s: float = 0.5  # start of each capture excluded (wearer settling)
    min_updates: int = 10  # envelope updates required after discard
    max_clipped_fraction: float = 0.0  # any clipped sample in a capture rejects it by default
    min_ratio: float = 2.0  # squeeze reference must be >= min_ratio x relaxed reference
    min_separation: float = 1.0  # and exceed relaxed by this many envelope units
    survives_reboot: bool = False  # conservative: device reboot invalidates calibration

    def validate(self) -> None:
        for name in ("relaxed_s", "squeeze_s"):
            if not 0.5 <= getattr(self, name) <= 60:
                raise ValueError(f"calibration.{name} must be in [0.5, 60] s")
        if not 0 <= self.discard_s < min(self.relaxed_s, self.squeeze_s):
            raise ValueError("calibration.discard_s must be >= 0 and shorter than each capture")
        if self.min_updates < 3:
            raise ValueError("calibration.min_updates must be >= 3")
        if not 0 <= self.max_clipped_fraction < 1:
            raise ValueError("calibration.max_clipped_fraction must be in [0, 1)")
        if not self.min_ratio > 1:
            raise ValueError("calibration.min_ratio must be > 1")
        if not self.min_separation > 0:
            raise ValueError("calibration.min_separation must be > 0")


@dataclass(frozen=True)
class VibrationConfig:
    """Accelerometer vibration features: gravity/DC removal by causal high-pass, then RMS.

    Unweighted (no ISO 5349 frequency weighting). The upper band edge is the
    sensor's own bandwidth (internal filter and ODR, unknown until hardware is
    confirmed) unless ``lowpass_hz`` is set.
    """
    highpass_hz: float = 5.0
    lowpass_hz: float | None = None
    order: int = 2
    nyquist_headroom: float = 0.9
    window_s: float = 1.0
    settle_s: float = 0.25

    def check(self, rate_hz: float) -> str | None:
        nyq = rate_hz / 2
        if not 1 <= self.order <= 4:
            return "order must be 1..4"
        if not 0 < self.highpass_hz < self.nyquist_headroom * nyq:
            return (f"highpass_hz {self.highpass_hz:g} must be in (0, {self.nyquist_headroom * nyq:g}) "
                    f"at {rate_hz:g} Hz sampling")
        if self.lowpass_hz is not None and not self.highpass_hz < self.lowpass_hz <= self.nyquist_headroom * nyq:
            return (f"lowpass_hz {self.lowpass_hz:g} must be in ({self.highpass_hz:g}, "
                    f"{self.nyquist_headroom * nyq:g}] at {rate_hz:g} Hz sampling")
        return None

    def validate(self) -> None:
        if not 0.1 <= self.window_s <= 10:
            raise ValueError("vibration.window_s must be in [0.1, 10] s")
        if not 0 <= self.settle_s <= 5:
            raise ValueError("vibration.settle_s must be in [0, 5] s")
        if not 0 < self.nyquist_headroom <= 1:
            raise ValueError("vibration.nyquist_headroom must be in (0, 1]")


@dataclass(frozen=True)
class ToolUseConfig:
    """Tool-use detection from accelerometer band energy (see tool_use.py).

    Power tools vibrate continuously at tens to hundreds of Hz; normal arm use
    is mostly below ~10 Hz and brief knocks last well under a second. The
    thresholds below are PROVISIONAL team choices, to be tuned on the real
    tools and mounting. They are not regulatory exposure values.
    """
    tool_band_low_hz: float = 20.0     # 4th-order high-pass: tool vibration band
    motion_band_hz: tuple[float, float] = (0.5, 10.0)   # arm movement band
    order: int = 4
    window_s: float = 0.5
    on_threshold_ms2: float = 1.0      # tool-band RMS to start counting as "tool"
    off_threshold_ms2: float = 0.6     # tool-band RMS below which use has paused
    tool_to_motion_min: float = 0.5    # tool-band RMS must be >= this x motion-band RMS
    on_hold_s: float = 2.0             # sustained this long before "tool" (rejects knocks)
    off_hold_s: float = 3.0            # quiet this long before "no_tool" (bridges short pauses)
    max_step_s: float = 0.5            # longer update gaps count as unobserved time
    # Warnings (team-chosen defaults, not regulatory limits):
    continuous_warn_s: float = 20 * 60     # one bout of tool use longer than this
    session_warn_s: float = 2 * 3600       # total tool time this session
    session_notice_fraction: float = 0.8   # early notice at this fraction of session_warn_s
    grip_activation_min: float = 0.10      # normalised activation counted as "gripping"

    def check(self, rate_hz: float) -> str | None:
        nyq = rate_hz / 2
        if not 0 < self.tool_band_low_hz < 0.9 * nyq:
            return f"tool_band_low_hz {self.tool_band_low_hz:g} must be below 0.9 x Nyquist ({0.9 * nyq:g} Hz)"
        lo, hi = self.motion_band_hz
        if not 0 < lo < hi < 0.9 * nyq:
            return f"motion_band_hz {self.motion_band_hz} invalid at {rate_hz:g} Hz"
        return None

    def validate(self) -> None:
        if not (1 <= self.order <= 8 and 0.1 <= self.window_s <= 5):
            raise ValueError("tool_use: order 1..8 and window_s 0.1..5 s required")
        if not (0 < self.off_threshold_ms2 <= self.on_threshold_ms2):
            raise ValueError("tool_use: 0 < off_threshold_ms2 <= on_threshold_ms2 required")
        if not (self.on_hold_s >= 0 and self.off_hold_s >= 0 and self.max_step_s > 0):
            raise ValueError("tool_use: hold times >= 0 and max_step_s > 0 required")
        if not (self.continuous_warn_s > 0 and self.session_warn_s > 0
                and 0 < self.session_notice_fraction < 1):
            raise ValueError("tool_use: warning limits must be > 0, notice fraction in (0, 1)")
        if self.motion_band_hz[1] >= self.tool_band_low_hz:
            raise ValueError("tool_use: motion band must lie below the tool band")


ACTIVATION_MODES = ("required", "ignored")


@dataclass(frozen=True)
class ScoreConfig:
    """EXPERIMENTAL score. rate [au/h] = k_per_hour * (V / v_ref)^exponent * (1 + activation_weight * a),
    V = combined dynamic accel RMS in m/s^2, a = clamp(normalised activation, 0, activation_cap).
    Arbitrary units; an algorithm demonstration, not a dose or exposure standard."""
    k_per_hour: float = 1.0
    v_ref: float = 1.0  # m/s^2
    exponent: float = 2.0
    activation_weight: float = 1.0
    activation_cap: float = 2.0
    activation_mode: str = "required"  # "ignored": vibration-only (a = 0), declared in output
    vibration_fresh_s: float = 0.25
    activation_fresh_s: float = 0.25

    def validate(self) -> None:
        if not (self.k_per_hour > 0 and self.v_ref > 0 and 0 < self.exponent <= 4):
            raise ValueError("score: k_per_hour > 0, v_ref > 0, 0 < exponent <= 4 required")
        if not (self.activation_weight >= 0 and self.activation_cap > 0):
            raise ValueError("score: activation_weight >= 0 and activation_cap > 0 required")
        if self.activation_mode not in ACTIVATION_MODES:
            raise ValueError(f"score.activation_mode must be one of {ACTIVATION_MODES}")
        if not (0 < self.vibration_fresh_s <= 5 and 0 < self.activation_fresh_s <= 5):
            raise ValueError("score freshness limits must be in (0, 5] s")


@dataclass(frozen=True)
class ProcessingConfig:
    emg: StreamConfig = field(default_factory=lambda: StreamConfig(window_s=0.1))
    imu: StreamConfig = field(default_factory=lambda: StreamConfig(window_s=0.5))
    emg_processing: EmgProcessingConfig = field(default_factory=EmgProcessingConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    vibration: VibrationConfig = field(default_factory=VibrationConfig)
    score: ScoreConfig = field(default_factory=ScoreConfig)
    tool_use: ToolUseConfig = field(default_factory=ToolUseConfig)
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
        self.emg_processing.validate()
        self.calibration.validate()
        self.vibration.validate()
        self.score.validate()
        self.tool_use.validate()
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
