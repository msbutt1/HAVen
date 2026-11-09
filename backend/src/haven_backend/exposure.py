"""Daily vibration exposure on the backend: per-session summary, daily totals, personal limit.

The processor's ``exposure`` record (haven_signals.processing.exposure) is cumulative per
accumulation (a shift; a new shift or a provenance change starts a new one). Here:

* ``session_exposure_summary`` -- exposure attributable to one recording = end - start of the
  same accumulation (like ``session_score_summary``). Stored under ``counters["exposure"]`` of the
  wearable session row, so the SQLite schema does not change and old sessions still load.
* ``daily_exposure`` -- per local calendar day and data source: energy sum(a^2 dt) of that day's
  sessions added up, then A(8), HSE points, % of the personal limit; plus a rolling 7-day mean
  A(8) and the ISO 5349-1 Annex C years-to-risk group estimate. Live, replay and simulated data
  are never added together.
* ``PersonalLimit`` -- the per-worker limit in HSE points (default 400 = limit value; it can only
  be lowered), saved in ``<data_dir>/exposure_settings.json`` and applied to every processor.

All values are ESTIMATES from a wrist-worn IMU (monitoring, not ISO 5349-2 measurement).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from haven_signals.processing.config import ExposureConfig
from haven_signals.processing.exposure import (EAV_POINTS, ELV_POINTS, a8_from_energy, limits_reference,
                                                   points_from_energy, years_to_10pct_vwf)

GATES = ("tool_on", "gripped")
LABEL = "daily vibration exposure ESTIMATE (ISO 5349-1 A(8) from a wrist IMU; not a measurement, not a diagnosis)"


def _view(rec: dict | None) -> dict | None:
    if not rec or "by_gate" not in rec:
        return None
    return {"accumulation": rec.get("accumulation"), "gate": rec.get("gate"),
            "by_gate": {g: {"energy_m2s3": float(rec["by_gate"][g]["energy_m2s3"]),
                            "contact_s": float(rec["by_gate"][g]["contact_s"])} for g in GATES},
            "grip_unknown_s": float(rec.get("grip_unknown_s") or 0.0),
            "grip_during_contact": rec.get("grip_during_contact"),
            "personal_limit_points": (rec.get("limits") or {}).get("personal_limit_points")}


def session_exposure_summary(start: dict | None, first: dict | None, end: dict | None,
                             mixed_sources: bool = False) -> dict:
    """Exposure attributable to one session = end - start of the same accumulation."""
    e = _view(end)
    out: dict[str, Any] = {"label": LABEL, "status": "unavailable", "reason": None,
                           "gate": None, "by_gate": None, "energy_m2s3": None, "contact_s": None,
                           "points": None, "a8_ms2": None, "grip_during_contact": None}
    if e is None:
        out["reason"] = "no_exposure_records_during_session"
        return out
    s = _view(start)
    if s is None:
        f = _view(first)
        zero = {g: {"energy_m2s3": 0.0, "contact_s": 0.0} for g in GATES}
        s = {"accumulation": f["accumulation"] if f else e["accumulation"], "by_gate": zero, "grip_unknown_s": 0.0}
    if s["accumulation"] != e["accumulation"]:
        out["reason"] = "exposure_reset_during_session (new shift or source change)"
        return out
    if mixed_sources:
        out["reason"] = "session_mixes_data_sources"
        return out
    by_gate = {}
    for g in GATES:
        en = max(0.0, e["by_gate"][g]["energy_m2s3"] - s["by_gate"][g]["energy_m2s3"])
        by_gate[g] = {"energy_m2s3": round(en, 6),
                      "contact_s": round(max(0.0, e["by_gate"][g]["contact_s"] - s["by_gate"][g]["contact_s"]), 3),
                      "points": round(points_from_energy(en), 4), "a8_ms2": round(a8_from_energy(en), 4)}
    head = e["gate"] if e["gate"] in GATES else "gripped"
    out.update({"status": "ok", "gate": head, "by_gate": by_gate, **{k: by_gate[head][k] for k in
                ("energy_m2s3", "contact_s", "points", "a8_ms2")},
                "grip_unknown_s": round(max(0.0, e["grip_unknown_s"] - s["grip_unknown_s"]), 3),
                "grip_during_contact": e["grip_during_contact"],
                "note": "grip_during_contact covers the whole accumulation (shift), not only this session"})
    return out


def _local_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return None


def daily_exposure(sessions: list[dict], personal_limit: float, days: int = 14,
                   today: str | None = None, cfg: ExposureConfig | None = None) -> dict:
    """Per-day, per-source exposure from wearable session rows (newest day first)."""
    c = cfg or ExposureConfig()
    acc: dict[tuple[str, str], dict] = {}
    for row in sessions:
        ex = ((row.get("counters") or {}).get("exposure") or {})
        if ex.get("status") != "ok":
            continue
        day = _local_date(row.get("started_at"))
        if day is None:
            continue
        src = row.get("source") or "unknown"
        d = acc.setdefault((day, src), {"date": day, "source": src, "sessions": 0,
                                        "by_gate": {g: {"energy_m2s3": 0.0, "contact_s": 0.0} for g in GATES}})
        d["sessions"] += 1
        for g in GATES:
            d["by_gate"][g]["energy_m2s3"] += float(ex["by_gate"][g]["energy_m2s3"])
            d["by_gate"][g]["contact_s"] += float(ex["by_gate"][g]["contact_s"])
    gate = "gripped" if c.grip_gate else "tool_on"
    rows = []
    for d in acc.values():
        for g in GATES:
            en = d["by_gate"][g]["energy_m2s3"]
            d["by_gate"][g].update(energy_m2s3=round(en, 6), contact_s=round(d["by_gate"][g]["contact_s"], 3),
                                   points=round(points_from_energy(en), 4), a8_ms2=round(a8_from_energy(en, c.t0_s), 4))
        h = d["by_gate"][gate]
        pts = h["points"]
        d.update(gate=gate, points=pts, a8_ms2=h["a8_ms2"], contact_s=h["contact_s"],
                 percent_of_limit=round(100.0 * pts / personal_limit, 2),
                 level="above_limit" if pts >= personal_limit else "above_eav" if pts >= EAV_POINTS else "below_eav")
        rows.append(d)
    rows.sort(key=lambda r: (r["date"], r["source"]), reverse=True)
    end = datetime.fromisoformat(today).date() if today else datetime.now().astimezone().date()
    keep = {(end - timedelta(days=i)).isoformat() for i in range(days)}
    rows = [r for r in rows if r["date"] in keep]
    rolling = {}
    week = {(end - timedelta(days=i)).isoformat() for i in range(7)}
    for src in sorted({r["source"] for r in rows}):
        a8s = [r["a8_ms2"] for r in rows if r["source"] == src and r["date"] in week]
        if not a8s:
            continue
        mean_a8 = math.sqrt(sum(a * a for a in a8s) / len(a8s))  # energy mean over exposed days
        yrs = years_to_10pct_vwf(mean_a8, c)
        rolling[src] = {"days_with_exposure": len(a8s), "a8_ms2": round(mean_a8, 4),
                        "years_to_10pct_vwf": None if yrs is None else round(yrs, 1)}
    return {"label": LABEL, "gate": gate, "personal_limit_points": personal_limit,
            "eav_points": EAV_POINTS, "elv_points": ELV_POINTS, "days": rows,
            "rolling_7_day": rolling,
            "notes": {"day": "local calendar day of the session start; sources are never added together",
                      "rolling": "energy mean A(8) over the exposed days of the last 7",
                      "years": "ISO 5349-1 Annex C group estimate (10 % of a group); not shown below 1 m/s^2"}}


class PersonalLimit:
    """The per-worker daily limit in HSE points, persisted in the data directory."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "exposure_settings.json"
        self.points = ELV_POINTS
        self.updated_at: str | None = None
        try:
            saved = json.loads(self.path.read_text())
            self.points = self.check(saved.get("personal_limit_points", ELV_POINTS))
            self.updated_at = saved.get("updated_at")
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def check(points: Any) -> float:
        if (isinstance(points, bool) or not isinstance(points, (int, float)) or not math.isfinite(points)
                or not 0 < points <= ELV_POINTS):
            raise ValueError("personal_limit_points must be a number in (0, 400]; it can only be lowered")
        return float(points)

    def set(self, points: Any, now_iso: str) -> dict:
        self.points = self.check(points)
        self.updated_at = now_iso
        tmp = self.path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self.view(), indent=2) + "\n")
        tmp.replace(self.path)
        return self.view()

    def view(self) -> dict:
        return {"personal_limit_points": self.points, "updated_at": self.updated_at,
                "lowered": self.points < ELV_POINTS,
                "equivalent_a8_ms2": round(2.5 * math.sqrt(self.points / EAV_POINTS), 3),
                "note": "100 points = action value (A(8) 2.5 m/s^2), 400 = limit value (5.0 m/s^2)"}


def limits_info() -> dict:
    return limits_reference()
