"""Trial records and the session result object (pure data, no clock, no I/O).

Framing (kept in every saved result):
- the stimulus unit is ``motor_setting``: a command to the motor driver, never a
  measured physical amplitude (``measured_amplitude`` is null: the station has
  no accelerometer);
- this is a vibration-perception prototype, not a diagnostic test;
- a timeout is ``missing``, never ``not_felt``;
- a preview is labelled as such and is not equivalent to a full test.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .config import STIMULUS_UNITS, StationConfig
from .staircase import Staircase

TRIAL_SCHEMA = "vibeguard.station_trial"
RESULT_SCHEMA = "vibeguard.station_result"
EVENT_SCHEMA = "vibeguard.station_event"
VERSION = 1

FRAMING = ("Vibration-perception prototype. Not a diagnostic test and not validated for "
           "detecting nerve damage. Stimulus is a motor setting (a driver command), not a "
           "measured physical amplitude.")
LABEL_FULL = "vibration_perception_staircase_estimate_prototype"
LABEL_PREVIEW = "PREVIEW_short_run_not_equivalent_to_full_test"
METHOD = "mean_of_last_{n}_reversal_levels"

RESPONSES = ("felt", "not_felt")
RESPONSE_STATUSES = ("received", "missing", "aborted", "error")
TERMINAL_STATES = ("completed", "aborted", "error")
OUTCOMES = ("threshold_estimated", "floor_reached", "ceiling_reached", "max_trials_reached",
            "too_many_missing_responses", "aborted", "abort_stop_not_confirmed", "adapter_error")


@dataclass
class TrialRecord:
    trial_id: str
    index: int  # 1-based position in the session
    kind: str  # "stimulus" | "catch"
    stimulus_setting: int  # motor setting; 0 (motor off) for catch trials
    stimulus_ms: int
    command_t_s: float  # engine time (s since session start) the stimulus command was issued
    stimulus_started_t_s: float | None = None  # when the controller confirmed it, if it did
    response: str | None = None  # "felt" | "not_felt" | None
    response_status: str = "missing"  # received | missing | aborted | error
    response_source: str | None = None  # e.g. "button", "on_screen"
    response_t_s: float | None = None
    latency_ms: float | None = None  # response time - command time
    catch_outcome: str | None = None  # catch only: "pass" (not felt) | "fail" (felt) | None
    staircase: dict | None = None  # stimulus trials with a received response only
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema": TRIAL_SCHEMA, "version": VERSION,
            "trial_id": self.trial_id, "index": self.index, "kind": self.kind,
            "stimulus_setting": self.stimulus_setting, "stimulus_units": STIMULUS_UNITS,
            "stimulus_ms": self.stimulus_ms,
            "measured_amplitude": None,  # the station has no accelerometer: never measured
            "command_t_s": _r(self.command_t_s),
            "stimulus_started_t_s": _r(self.stimulus_started_t_s),
            "response": self.response, "response_status": self.response_status,
            "response_source": self.response_source, "response_t_s": _r(self.response_t_s),
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
            "catch_outcome": self.catch_outcome, "staircase": self.staircase, "note": self.note,
        }


def _r(v):
    return None if v is None else round(v, 4)


@dataclass
class Counters:
    late_responses_ignored: int = 0
    duplicate_responses_ignored: int = 0
    unsolicited_responses_ignored: int = 0
    stop_not_confirmed: bool = False
    extra: dict = field(default_factory=dict)


def _limitation(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def build_limitations(cfg: StationConfig, trials: list[TrialRecord], stair: Staircase,
                      provenance: dict, outcome: str, counters: Counters,
                      error: str | None) -> list[dict]:
    out = [
        _limitation("motor_setting_not_measured_amplitude",
                    "Stimulus is a motor setting; physical amplitude was not measured."),
        _limitation("contact_pressure_uncontrolled",
                    "Fingertip contact pressure was not measured or controlled."),
    ]
    if cfg.setting_range_source.startswith("assumed"):
        out.append(_limitation("setting_range_assumed",
                               "The motor setting range is an assumed placeholder, not a calibrated scale."))
    if cfg.mode == "preview":
        out.append(_limitation("preview_not_full_test",
                               "Short preview run; not equivalent to a full test."))
    src = provenance.get("source")
    if src == "simulated":
        out.append(_limitation("data_simulated", "Responses and stimulus were simulated, not a real person."))
    elif src == "replay":
        out.append(_limitation("data_replay", "Replay of an earlier recording, not a live test."))
    missing = sum(t.response_status == "missing" for t in trials)
    if missing:
        out.append(_limitation("missing_responses",
                               f"{missing} trial(s) had no response in the window; recorded as missing, "
                               "not as 'not felt'."))
    catches = [t for t in trials if t.kind == "catch"]
    fails = sum(t.catch_outcome == "fail" for t in catches)
    if not catches:
        out.append(_limitation("no_catch_trials", "No catch trials ran, so guessing cannot be assessed."))
    if fails:
        out.append(_limitation("catch_trial_false_positives",
                               f"'Felt' reported on {fails} of {len(catches)} catch trial(s) with no stimulus."))
    if counters.late_responses_ignored:
        out.append(_limitation("late_responses_ignored",
                               f"{counters.late_responses_ignored} response(s) arrived after the window and were ignored."))
    if counters.duplicate_responses_ignored:
        out.append(_limitation("duplicate_responses_ignored",
                               f"{counters.duplicate_responses_ignored} repeated response(s) for an already answered trial were ignored."))
    if counters.unsolicited_responses_ignored:
        out.append(_limitation("unsolicited_responses_ignored",
                               f"{counters.unsolicited_responses_ignored} response(s) arrived with no trial waiting and were ignored."))
    sources = sorted({t.response_source for t in trials if t.response_source})
    if any(s != "button" for s in sources):
        out.append(_limitation("non_button_response_source", f"Responses came from: {', '.join(sources)}."))
    levels = stair.levels_used()
    if len(levels) >= 2 and max(levels) - min(levels) > 4 * cfg.step:
        out.append(_limitation("unstable_staircase",
                               "Reversal levels used for the estimate spread widely; treat it with caution."))
    if outcome == "floor_reached":
        out.append(_limitation("floor_reached",
                               "Felt at the lowest motor setting repeatedly: threshold is below the tested range."))
    elif outcome == "ceiling_reached":
        out.append(_limitation("ceiling_reached",
                               "Not felt at the highest motor setting repeatedly: threshold is above the tested range."))
    elif outcome == "max_trials_reached":
        out.append(_limitation("max_trials_reached", "Trial limit reached before enough reversals."))
    elif outcome == "too_many_missing_responses":
        out.append(_limitation("too_many_missing_responses",
                               f"{cfg.max_consecutive_missing} responses in a row were missing; test ended."))
    elif outcome == "aborted":
        out.append(_limitation("aborted", "Test was aborted; no threshold was computed."))
    elif outcome == "abort_stop_not_confirmed":
        out.append(_limitation("abort_stop_not_confirmed",
                               "Abort was requested but the stimulus stop could not be confirmed. Check the motor."))
    elif outcome == "adapter_error":
        out.append(_limitation("adapter_error", error or "Adapter error."))
    if counters.stop_not_confirmed and outcome != "abort_stop_not_confirmed":
        out.append(_limitation("abort_stop_not_confirmed", "A stimulus stop was not confirmed."))
    return out


def build_result(*, session_id: str, cfg: StationConfig, state: str, outcome: str,
                 provenance: dict, trials: list[TrialRecord], stair: Staircase,
                 started_at: str | None, ended_at: str | None, duration_s: float,
                 counters: Counters, abort_reason: str | None = None,
                 error: str | None = None) -> dict:
    preview = cfg.mode == "preview"
    estimated = outcome == "threshold_estimated"
    threshold = None
    if estimated:
        levels = stair.levels_used()
        threshold = {
            "value": stair.threshold(), "units": STIMULUS_UNITS,
            "method": METHOD.format(n=cfg.reversals_averaged),
            "reversal_levels_used": levels,
            "reversal_spread": round(statistics.pstdev(levels), 4) if len(levels) > 1 else 0.0,
        }
    catches = [t for t in trials if t.kind == "catch"]
    stim = [t for t in trials if t.kind == "stimulus"]
    return {
        "schema": RESULT_SCHEMA, "version": VERSION,
        "session_id": session_id, "mode": cfg.mode, "is_preview": preview,
        "state": state, "outcome": outcome,
        "result_label": LABEL_PREVIEW if preview else LABEL_FULL,
        "framing": FRAMING,
        "provenance": provenance,
        "stimulus_units": STIMULUS_UNITS,
        "threshold": threshold,
        "reversals": {"count": stair.reversals, "levels": list(stair.reversal_levels),
                      "required": cfg.reversals_to_stop},
        "trial_counts": {
            "total": len(trials), "stimulus": len(stim), "catch": len(catches),
            "received": sum(t.response_status == "received" for t in trials),
            "missing": sum(t.response_status == "missing" for t in trials),
        },
        "catch_trials": {
            "trials": len(catches),
            "passed": sum(t.catch_outcome == "pass" for t in catches),
            "failed": sum(t.catch_outcome == "fail" for t in catches),
            "no_response": sum(t.response_status == "missing" for t in catches),
        },
        "quality": {"limitations": build_limitations(cfg, trials, stair, provenance, outcome,
                                                     counters, error)},
        "abort_reason": abort_reason, "error": error,
        "config": cfg.describe(),
        "started_at": started_at, "ended_at": ended_at, "duration_s": round(duration_s, 3),
        "trials": [t.to_dict() for t in trials],
    }
