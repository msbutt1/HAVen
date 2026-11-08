"""Validator for VibeGuard station protocol v1 (PROPOSED). See STATION_PROTOCOL.md.

Separate from the wearable telemetry contract (signals/contract): station
commands and events never go through the telemetry validator.

    python station_protocol.py FILE.ndjson      # validate events (default) ...
    python station_protocol.py --commands FILE  # ... or host commands
Exit status 0 iff every line is valid. Stdlib only.
"""

from __future__ import annotations

import json
import math
import re
import sys

CMD_SCHEMA = "vibeguard.station_command"
EV_SCHEMA = "vibeguard.station_event"
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MAX_COMMAND_BYTES = 512

OUTCOMES = ("in_progress", "completed", "aborted", "link_timeout", "fault", "rejected")
REJECT_REASONS = ("busy", "fault", "motor_disabled", "level_out_of_range",
                  "duration_out_of_range", "request_id_reused")
ERROR_REASONS = ("malformed", "oversized", "wrong_schema", "unsupported_version",
                 "unknown_type", "invalid_request_id", "unknown_field")
STATES = ("idle", "active", "fault", "disabled")

COMMON_EV = {"schema", "version", "type", "device_id", "boot_id", "seq", "t_us", "provenance"}
EV_FIELDS = {
    "hello": {"firmware", "driver", "limits", "state"},
    "status": {"state", "motor", "active_request_id", "fault_detail", "counters"},
    "ack": {"request_id", "command", "result", "reason", "outcome"},
    "stimulus_started": {"request_id", "level", "duration_ms"},
    "stimulus_ended": {"request_id", "outcome", "requested_ms", "on_ms"},
    "error": {"reason", "detail", "request_id"},
}
EV_OPTIONAL = {"ack": {"outcome"}}


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _dup_check(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def parse(line: str):
    return json.loads(line, object_pairs_hook=_dup_check,
                      parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))


def validate_command(cmd) -> list[str]:
    """Host -> device. Mirrors the firmware's checks (firmware is authoritative)."""
    p: list[str] = []
    if not isinstance(cmd, dict):
        return ["command must be a JSON object"]
    if cmd.get("schema") != CMD_SCHEMA:
        p.append(f"schema must be {CMD_SCHEMA!r}")
    if not (_int(cmd.get("version")) and cmd.get("version") == 1):
        p.append("version must be 1")
    t = cmd.get("type")
    allowed = {"schema", "version", "type", "request_id"}
    if t == "stimulus":
        allowed |= {"level", "duration_ms"}
        for k in ("level", "duration_ms"):
            if not _int(cmd.get(k)):
                p.append(f"{k} must be an integer")
    elif t not in ("abort", "status"):
        p.append("type must be stimulus, abort or status")
    if not (isinstance(cmd.get("request_id"), str) and ID_RE.match(cmd["request_id"])):
        p.append("request_id must be 1-64 chars of [A-Za-z0-9._:-]")
    for k, v in cmd.items():
        if k not in allowed:
            p.append(f"unknown field {k!r}")
        if isinstance(v, (dict, list)):
            p.append(f"{k}: nested values not allowed")
        if isinstance(v, str) and "\\" in json.dumps(v)[1:-1]:
            p.append(f"{k}: escapes not supported")
    if len(json.dumps(cmd, separators=(",", ":"))) + 1 > MAX_COMMAND_BYTES:
        p.append(f"command exceeds {MAX_COMMAND_BYTES} bytes")
    return p


def validate_event(ev) -> list[str]:
    """Device -> host."""
    p: list[str] = []
    if not isinstance(ev, dict):
        return ["event must be a JSON object"]
    t = ev.get("type")
    if ev.get("schema") != EV_SCHEMA or ev.get("version") != 1 or t not in EV_FIELDS:
        return [f"bad schema/version/type: {ev.get('schema')!r} {ev.get('version')!r} {t!r}"]
    required = COMMON_EV | (EV_FIELDS[t] - EV_OPTIONAL.get(t, set()))
    allowed = COMMON_EV | EV_FIELDS[t]
    p += [f"missing {k!r}" for k in sorted(required - set(ev))]
    p += [f"unknown field {k!r}" for k in sorted(set(ev) - allowed)]
    for k in ("device_id", "boot_id"):
        if not (isinstance(ev.get(k), str) and ID_RE.match(ev[k])):
            p.append(f"{k} invalid")
    for k in ("seq", "t_us"):
        if not (_int(ev.get(k)) and 0 <= ev[k] <= 2**53 - 1):
            p.append(f"{k} must be an integer in [0, 2^53-1]")
    prov = ev.get("provenance")
    if not (isinstance(prov, dict) and set(prov) == {"source"} and prov["source"] in ("live", "simulated")):
        p.append("provenance must be {source: live|simulated}")
    rid = ev.get("request_id")
    if "request_id" in ev and not (rid is None and t == "error") and not (isinstance(rid, str) and ID_RE.match(rid)):
        p.append("request_id invalid")
    if t == "hello":
        lim = ev.get("limits", {})
        if not (isinstance(lim, dict) and lim.get("level_min") == 1 and lim.get("level_max") == 100
                and _int(lim.get("max_duration_ms")) and 1 <= lim["max_duration_ms"] <= 2000
                and _int(lim.get("heartbeat_timeout_ms")) and lim.get("command_max_bytes") == MAX_COMMAND_BYTES):
            p.append("limits invalid")
        drv = ev.get("driver", {})
        if not (isinstance(drv, dict) and {"model", "actuator", "drive", "full_scale_v"} <= set(drv)
                and _num(drv["full_scale_v"])):
            p.append("driver invalid")
        if ev.get("state") not in STATES:
            p.append("state invalid")
    elif t == "status":
        if ev.get("state") not in STATES or ev.get("motor") not in ("on", "off"):
            p.append("state/motor invalid")
        if (ev.get("motor") == "on") != (ev.get("state") == "active"):
            p.append("motor must be on exactly when state is active")
        c = ev.get("counters")
        if not (isinstance(c, dict) and all(_int(v) and v >= 0 for v in c.values())):
            p.append("counters invalid")
    elif t == "ack":
        if ev.get("command") not in ("stimulus", "abort", "status"):
            p.append("command invalid")
        res = ev.get("result")
        if res not in ("accepted", "rejected", "duplicate"):
            p.append("result invalid")
        if res == "rejected" and ev.get("reason") not in REJECT_REASONS:
            p.append("rejected ack needs a known reason")
        if res != "rejected" and ev.get("reason") is not None:
            p.append("reason must be null unless rejected")
        if (res == "duplicate") != ("outcome" in ev):
            p.append("outcome present exactly for duplicate acks")
        if "outcome" in ev and ev["outcome"] not in OUTCOMES:
            p.append("outcome invalid")
    elif t == "stimulus_started":
        if not (_int(ev.get("level")) and 1 <= ev["level"] <= 100):
            p.append("level must be 1..100")
        if not (_int(ev.get("duration_ms")) and 1 <= ev["duration_ms"] <= 2000):
            p.append("duration_ms must be 1..2000")
    elif t == "stimulus_ended":
        if ev.get("outcome") not in ("completed", "aborted", "link_timeout", "fault"):
            p.append("outcome invalid")
        if not (_num(ev.get("on_ms")) and ev["on_ms"] >= 0):
            p.append("on_ms invalid")
        elif ev.get("outcome") == "completed" and _int(ev.get("requested_ms")) \
                and ev["on_ms"] + 1e-9 < ev["requested_ms"]:
            p.append("completed stimulus shorter than requested")
    elif t == "error":
        if ev.get("reason") not in ERROR_REASONS:
            p.append("error reason invalid")
        if not (isinstance(ev.get("detail"), str) and len(ev["detail"]) <= 200):
            p.append("detail invalid")
    return p


def check_sequence(events: list[dict]) -> list[str]:
    """Cross-record rules: seq strictly +1 per boot from 0; started/ended pairing."""
    p: list[str] = []
    last: dict[str, int] = {}
    active: dict[str, str | None] = {}
    for i, ev in enumerate(events):
        b = ev.get("boot_id")
        if b not in last:
            if ev.get("seq") != 0:
                p.append(f"event {i}: boot {b} does not start at seq 0")
        elif ev.get("seq") != last[b] + 1:
            p.append(f"event {i}: seq {ev.get('seq')} after {last[b]} (gap or repeat)")
        last[b] = ev.get("seq", -1)
        if ev.get("type") == "stimulus_started":
            if active.get(b):
                p.append(f"event {i}: started while {active[b]} still active")
            active[b] = ev["request_id"]
        elif ev.get("type") == "stimulus_ended":
            if active.get(b) != ev["request_id"] and ev.get("outcome") != "fault":
                p.append(f"event {i}: ended {ev['request_id']} but active was {active.get(b)}")
            active[b] = None
    return p


def main(argv: list[str]) -> int:
    commands = "--commands" in argv
    paths = [a for a in argv if a != "--commands"]
    bad = total = 0
    events = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                total += 1
                try:
                    obj = parse(line)
                except ValueError as e:
                    bad += 1
                    print(f"{path}:{n}: not valid JSON: {e}")
                    continue
                issues = validate_command(obj) if commands else validate_event(obj)
                if not commands:
                    events.append(obj)
                if issues:
                    bad += 1
                    print(f"{path}:{n}: " + "; ".join(issues))
    seq_issues = [] if commands else check_sequence(events)
    for s in seq_issues:
        print(s)
    print(f"station_protocol: {total - bad}/{total} {'commands' if commands else 'events'} valid"
          + ("" if commands else f", {len(seq_issues)} sequence issues"))
    return 0 if bad == 0 and not seq_issues else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
