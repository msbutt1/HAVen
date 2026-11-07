"""Validators for the PROPOSED station records and link messages.

Each function returns a list of ``"path: problem"`` strings (empty = valid).
If ``contract/STATION_CONTRACT.md`` and these checks disagree, the validator is
the reference and the disagreement is a bug.
"""

from __future__ import annotations

import math
import re

from .config import CATCH_SETTING, MODES, STIMULUS_UNITS
from .link import COMMAND_SCHEMA, LINK_SCHEMA
from .records import (EVENT_SCHEMA, LABEL_FULL, LABEL_PREVIEW, OUTCOMES, RESPONSE_STATUSES,
                      RESPONSES, RESULT_SCHEMA, TERMINAL_STATES, TRIAL_SCHEMA, VERSION)

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
SOURCES = ("live", "simulated", "replay")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_provenance(p, path="provenance") -> list[str]:
    if not isinstance(p, dict):
        return [f"{path}: must be an object"]
    src = p.get("source")
    if src not in SOURCES:
        return [f"{path}.source: must be one of {SOURCES}"]
    issues = []
    if src == "replay":
        if not (isinstance(p.get("recording_id"), str) and _ID.match(p["recording_id"])):
            issues.append(f"{path}.recording_id: required for replay")
        if p.get("original_source") not in ("live", "simulated"):
            issues.append(f"{path}.original_source: required for replay (live or simulated)")
    return issues


def validate_trial(t, path="trial") -> list[str]:
    if not isinstance(t, dict):
        return [f"{path}: must be an object"]
    p: list[str] = []
    if t.get("schema") != TRIAL_SCHEMA or t.get("version") != VERSION:
        p.append(f"{path}: schema/version must be {TRIAL_SCHEMA!r}/{VERSION}")
    if not (isinstance(t.get("trial_id"), str) and _ID.match(t["trial_id"])):
        p.append(f"{path}.trial_id: invalid")
    if not (_is_int(t.get("index")) and t["index"] >= 1):
        p.append(f"{path}.index: must be an integer >= 1")
    kind = t.get("kind")
    if kind not in ("stimulus", "catch"):
        p.append(f"{path}.kind: must be 'stimulus' or 'catch'")
    if not _is_int(t.get("stimulus_setting")) or t["stimulus_setting"] < 0:
        p.append(f"{path}.stimulus_setting: must be an integer >= 0")
    elif kind == "catch" and t["stimulus_setting"] != CATCH_SETTING:
        p.append(f"{path}.stimulus_setting: catch trials use setting {CATCH_SETTING}")
    elif kind == "stimulus" and t["stimulus_setting"] == CATCH_SETTING:
        p.append(f"{path}.stimulus_setting: stimulus trials cannot use setting {CATCH_SETTING}")
    if t.get("stimulus_units") != STIMULUS_UNITS:
        p.append(f"{path}.stimulus_units: must be {STIMULUS_UNITS!r}")
    if t.get("measured_amplitude") is not None:
        p.append(f"{path}.measured_amplitude: must be null (the station does not measure amplitude)")
    status, resp = t.get("response_status"), t.get("response")
    if status not in RESPONSE_STATUSES:
        p.append(f"{path}.response_status: must be one of {RESPONSE_STATUSES}")
    elif status == "received":
        if resp not in RESPONSES:
            p.append(f"{path}.response: must be one of {RESPONSES} when received")
        if not _is_num(t.get("response_t_s")) or not _is_num(t.get("latency_ms")):
            p.append(f"{path}: received responses need response_t_s and latency_ms")
    elif resp is not None:
        p.append(f"{path}.response: must be null unless status is 'received' (a timeout is not 'not_felt')")
    if kind == "catch" and status == "received":
        want = "fail" if resp == "felt" else "pass"
        if t.get("catch_outcome") != want:
            p.append(f"{path}.catch_outcome: must be {want!r}")
    return p


def validate_result(r) -> list[str]:
    if not isinstance(r, dict):
        return ["result: must be an object"]
    p: list[str] = []
    if r.get("schema") != RESULT_SCHEMA or r.get("version") != VERSION:
        p.append(f"schema/version must be {RESULT_SCHEMA!r}/{VERSION}")
    if not (isinstance(r.get("session_id"), str) and _ID.match(r["session_id"])):
        p.append("session_id: invalid")
    mode = r.get("mode")
    if mode not in MODES:
        p.append(f"mode: must be one of {MODES}")
    if r.get("is_preview") is not (mode == "preview"):
        p.append("is_preview: must equal (mode == 'preview')")
    want_label = LABEL_PREVIEW if mode == "preview" else LABEL_FULL
    if r.get("result_label") != want_label:
        p.append(f"result_label: must be {want_label!r}")
    if r.get("state") not in TERMINAL_STATES:
        p.append(f"state: must be one of {TERMINAL_STATES}")
    if r.get("outcome") not in OUTCOMES:
        p.append(f"outcome: must be one of {OUTCOMES}")
    if r.get("stimulus_units") != STIMULUS_UNITS:
        p.append(f"stimulus_units: must be {STIMULUS_UNITS!r}")
    p += validate_provenance(r.get("provenance"))
    th = r.get("threshold")
    if r.get("outcome") == "threshold_estimated":
        if r.get("state") != "completed":
            p.append("outcome threshold_estimated requires state completed")
        if not isinstance(th, dict) or not _is_num(th.get("value")) or th.get("units") != STIMULUS_UNITS:
            p.append(f"threshold: required as {{value, units: {STIMULUS_UNITS!r}, ...}} when estimated")
    elif th is not None:
        p.append("threshold: must be null unless outcome is threshold_estimated")
    trials = r.get("trials")
    if not isinstance(trials, list):
        p.append("trials: must be a list")
        trials = []
    for i, t in enumerate(trials):
        p += validate_trial(t, f"trials[{i}]")
    counts = r.get("trial_counts")
    if isinstance(counts, dict):
        if counts.get("total") != len(trials):
            p.append("trial_counts.total: does not match trials")
        missing = sum(isinstance(t, dict) and t.get("response_status") == "missing" for t in trials)
        if counts.get("missing") != missing:
            p.append("trial_counts.missing: does not match trials")
    else:
        p.append("trial_counts: must be an object")
    lims = (r.get("quality") or {}).get("limitations")
    if not isinstance(lims, list) or not all(isinstance(x, dict) and x.get("code") for x in lims):
        p.append("quality.limitations: must be a list of {code, message}")
    else:
        codes = {x["code"] for x in lims}
        need = {"motor_setting_not_measured_amplitude", "contact_pressure_uncontrolled"}
        if mode == "preview":
            need.add("preview_not_full_test")
        if isinstance(r.get("provenance"), dict) and r["provenance"].get("source") == "simulated":
            need.add("data_simulated")
        if any(isinstance(t, dict) and t.get("response_status") == "missing" for t in trials):
            need.add("missing_responses")
        catches = [t for t in trials if isinstance(t, dict) and t.get("kind") == "catch"]
        if not catches:
            need.add("no_catch_trials")
        if any(t.get("catch_outcome") == "fail" for t in catches):
            need.add("catch_trial_false_positives")
        for c in sorted(need - codes):
            p.append(f"quality.limitations: missing required limitation {c!r}")
    return p


def validate_event(e) -> list[str]:
    if not isinstance(e, dict):
        return ["event: must be an object"]
    p = []
    if e.get("schema") != EVENT_SCHEMA or e.get("version") != VERSION:
        p.append(f"schema/version must be {EVENT_SCHEMA!r}/{VERSION}")
    if not isinstance(e.get("type"), str):
        p.append("type: required")
    if not (isinstance(e.get("session_id"), str) and _ID.match(e["session_id"])):
        p.append("session_id: invalid")
    if not (_is_int(e.get("seq")) and e["seq"] >= 1):
        p.append("seq: must be an integer >= 1")
    if e.get("state") not in ("ready", "testing") + TERMINAL_STATES:
        p.append("state: invalid")
    p += validate_provenance(e.get("provenance"))
    return p


def validate_command(c) -> list[str]:
    """Host -> controller command."""
    if not isinstance(c, dict):
        return ["command: must be an object"]
    p = []
    if c.get("schema") != COMMAND_SCHEMA or c.get("version") != VERSION:
        p.append(f"schema/version must be {COMMAND_SCHEMA!r}/{VERSION}")
    if not (isinstance(c.get("command_id"), str) and _ID.match(c["command_id"])):
        p.append("command_id: invalid")
    kind = c.get("type")
    if kind == "deliver_stimulus":
        if not (isinstance(c.get("trial_id"), str) and _ID.match(c["trial_id"])):
            p.append("trial_id: invalid")
        if not (_is_int(c.get("setting")) and c["setting"] >= 0):
            p.append("setting: must be an integer >= 0")
        if not (_is_int(c.get("duration_ms")) and c["duration_ms"] >= 1):
            p.append("duration_ms: must be an integer >= 1")
    elif kind != "stop_stimulus":
        p.append("type: must be 'deliver_stimulus' or 'stop_stimulus'")
    return p


def validate_link_message(m) -> list[str]:
    """Controller -> host message."""
    if not isinstance(m, dict):
        return ["message: must be an object"]
    p = []
    if m.get("schema") != LINK_SCHEMA or m.get("version") != VERSION:
        p.append(f"schema/version must be {LINK_SCHEMA!r}/{VERSION}")
    kind = m.get("type")
    if kind == "ack":
        if not isinstance(m.get("command_id"), str):
            p.append("command_id: required")
        if m.get("status") not in ("accepted", "rejected", "completed"):
            p.append("status: must be accepted, rejected or completed")
    elif kind in ("stimulus_started", "stimulus_ended", "response"):
        if not (_is_int(m.get("device_t_us")) and m["device_t_us"] >= 0):
            p.append("device_t_us: must be an integer >= 0")
        if kind == "response" and m.get("response") not in RESPONSES:
            p.append(f"response: must be one of {RESPONSES}")
    elif kind == "error":
        if not isinstance(m.get("message"), str):
            p.append("message: required")
    else:
        p.append("type: unknown")
    return p
