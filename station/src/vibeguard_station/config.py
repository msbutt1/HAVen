"""Station test configuration. Hardware-dependent values are assumptions, and say so.

The station is a vibration-perception PROTOTYPE. Its stimulus is a *motor
setting* (a command to the motor driver), not a measured physical amplitude.
The setting range below is an assumed placeholder until Collins confirms the
driver; ``setting_range_source`` records that in every saved result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

MODES = ("full", "preview")
STIMULUS_UNITS = "motor_setting"
CATCH_SETTING = 0  # setting 0 = motor off; used for catch trials (no stimulus)


@dataclass(frozen=True)
class StationConfig:
    mode: str = "full"
    # Staircase. The motor setting range is ASSUMED, not a measured scale.
    start_setting: int = 60
    step: int = 5
    min_setting: int = 1
    max_setting: int = 100
    reversals_to_stop: int = 8
    reversals_averaged: int = 6
    max_trials: int = 60  # all trials: stimulus, catch and missing
    # Timing (seconds on the engine clock, except stimulus_ms).
    stimulus_ms: int = 1000
    response_window_s: float = 4.0
    inter_trial_s: float = 1.0
    # Catch trials (no stimulus). They never move the staircase.
    catch_probability: float = 1 / 6
    catch_min_stimulus_trials: int = 2  # no catch before this many stimulus trials
    # Early-stop guards: end clearly instead of looping forever.
    max_consecutive_missing: int = 3
    max_boundary_repeats: int = 3
    setting_range_source: str = "assumed_default_pending_hardware"

    @classmethod
    def full(cls, **overrides) -> "StationConfig":
        return cls(**overrides)

    @classmethod
    def preview(cls, **overrides) -> "StationConfig":
        """Short judging version. NOT equivalent to a full test."""
        base = dict(step=10, reversals_to_stop=4, reversals_averaged=2,
                    max_trials=20, catch_probability=0.0)
        base.update(overrides)
        return cls(mode="preview", **base)

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if not 0 < self.min_setting < self.max_setting:
            raise ValueError("need 0 < min_setting < max_setting (0 is reserved for 'motor off')")
        if not self.min_setting <= self.start_setting <= self.max_setting:
            raise ValueError("start_setting must lie within [min_setting, max_setting]")
        if self.step < 1:
            raise ValueError("step must be >= 1")
        # The label comes from ``mode``, so the protocol must match it: a short run can never
        # carry the full-test label, and a preview must stay shorter than the full protocol.
        if self.mode == "full" and (self.reversals_to_stop, self.reversals_averaged) != (8, 6):
            raise ValueError("a full test uses 8 reversals and averages the last 6")
        if self.mode == "preview" and self.reversals_to_stop >= 8:
            raise ValueError("a preview must use fewer than 8 reversals")
        if self.stimulus_ms / 1000.0 > self.response_window_s:
            raise ValueError("stimulus_ms must fit inside response_window_s")
        if not 1 <= self.reversals_averaged <= self.reversals_to_stop:
            raise ValueError("need 1 <= reversals_averaged <= reversals_to_stop")
        if self.max_trials < self.reversals_to_stop + 1:
            raise ValueError("max_trials is too small to reach reversals_to_stop")
        if self.stimulus_ms < 1:
            raise ValueError("stimulus_ms must be >= 1")
        if not self.response_window_s > 0:
            raise ValueError("response_window_s must be > 0")
        if self.inter_trial_s < 0:
            raise ValueError("inter_trial_s must be >= 0")
        if not 0 <= self.catch_probability < 1:
            raise ValueError("catch_probability must be in [0, 1)")
        if self.catch_min_stimulus_trials < 0:
            raise ValueError("catch_min_stimulus_trials must be >= 0")
        if self.max_consecutive_missing < 1 or self.max_boundary_repeats < 1:
            raise ValueError("max_consecutive_missing and max_boundary_repeats must be >= 1")

    def describe(self) -> dict:
        return asdict(self)
