"""Offline replay of recorded device telemetry (algorithm verification only).

Production recording/replay belongs to the backend (Abduljabbar). This module
re-emits a recorded contract-v1 NDJSON stream with
``provenance = {"source": "replay", "original_source": <recorded source>,
"recording_id": <recorded recording_id>}``. Everything else is unchanged:
device_id, boot_id, seq, sample indices, and device timestamps, so the
recording time stays the timebase. Optional wall-clock pacing (``speed``) only
changes when records are written; it is declared by the caller and never
alters any record.
"""

from __future__ import annotations

import copy
from typing import Iterable, Iterator

from . import contract


class ReplayError(ValueError):
    pass


def to_replay(record: dict, recording_id: str | None = None) -> dict:
    """Return a replay copy of one valid record. Already-replayed records pass through."""
    prov = record["provenance"]
    if prov["source"] == "replay":
        return copy.deepcopy(record)
    rid = prov.get("recording_id") or recording_id
    if not rid:
        raise ReplayError("record has no recording_id; pass one explicitly so the replay keeps "
                          "a source identity")
    out = copy.deepcopy(record)
    out["provenance"] = {"source": "replay", "original_source": prov["source"], "recording_id": rid}
    if "detail" in prov:
        out["provenance"]["detail"] = copy.deepcopy(prov["detail"])
    contract.require_valid(out)
    return out


def device_time_us(record: dict) -> float | None:
    """Device time at which a record's content ends (batch: last sample)."""
    if "t_us" in record:
        return float(record["t_us"])
    t = record.get("timing")
    if not t:
        return None
    if t["mode"] == "explicit":
        return float(t["t_us"][-1])
    return t["t0_us"] + (len(record["samples"]) - 1) * t["interval_us"]


def replay_lines(lines: Iterable[bytes | str], recording_id: str | None = None,
                 skipped: list | None = None) -> Iterator[tuple[float, dict]]:
    """Yield (replay_clock_s, replay_record) for each valid input frame.

    ``replay_clock_s`` is a continuous pacing clock built from device time.
    Device time restarts at every boot, so each boot is appended after the
    previous one. Invalid frames are skipped and appended to ``skipped`` as
    (line_number, issues).
    """
    offset = 0.0
    boot = None
    boot_t0 = boot_last = 0.0
    for lineno, rec, issues in contract.iter_ndjson(lines):
        if issues:
            if skipped is not None:
                skipped.append((lineno, issues))
            continue
        t = device_time_us(rec)
        if rec["boot_id"] != boot:
            if boot is not None:
                offset += (boot_last - boot_t0) / 1e6
            boot = rec["boot_id"]
            boot_t0 = boot_last = t if t is not None else 0.0
        if t is not None:
            boot_last = max(boot_last, t)
        clock = offset + ((t if t is not None else boot_last) - boot_t0) / 1e6
        yield clock, to_replay(rec, recording_id)
