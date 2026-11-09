"""Offline end-to-end reference demo (SIMULATED DATA ONLY).

Wires together the existing pieces; no second implementation:
simulator -> recorded telemetry NDJSON -> Processor (with scripted calibration
commands) -> derived NDJSON -> compact CSV/text summaries. ``replay_demo``
re-processes the recording as provenance=replay and checks that the derived
output matches the original apart from provenance.

The demo supplies the simulator's own synthetic accelerometer scale as the
IMU conversion, cited as such. It is not a sensor datasheet value; real
hardware needs its own confirmed conversion.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from . import __version__, contract
from .processing import ProcessingConfig, Processor, StreamConfig, to_ndjson_line
from .replay import device_time_us, to_replay
from .simulator import DEMO_TIMELINE, SIM_COUNTS_PER_G, SimConfig, Simulation

DEMO_DURATION_S = 30.0
G = 9.80665
SIM_CONVERSION_SOURCE = ("simulator synthetic scale 4096 counts per g "
                         "(SIMULATED; not a sensor datasheet value)")
# Scripted calibration commands, in device seconds of boot 0. A command is issued
# just before the first batch whose first sample is at or after its time.
CALIBRATION_SCHEDULE = ((0.5, "relaxed"), (4.5, "squeeze"))
SUMMARY_STEP_S = 0.5


def demo_processing_config() -> ProcessingConfig:
    conv = {a: (G / SIM_COUNTS_PER_G, 0.0) for a in ("ax", "ay", "az")}
    return ProcessingConfig(imu=StreamConfig(window_s=0.5, conversion=conv,
                                             conversion_source=SIM_CONVERSION_SOURCE))


def _first_sample_s(rec: dict) -> float | None:
    t = rec.get("timing")
    if not t:
        return None
    return (t["t0_us"] if t["mode"] == "uniform" else t["t_us"][0]) / 1e6


def process_records(records: Iterable[dict], config: ProcessingConfig,
                    schedule=CALIBRATION_SCHEDULE) -> tuple[Processor, list[dict]]:
    """Run records through one Processor, issuing scripted calibration commands."""
    proc = Processor(config)
    pending = list(schedule)
    first_boot = None
    out: list[dict] = []
    for rec in records:
        first_boot = first_boot or rec["boot_id"]
        t = _first_sample_s(rec)
        while pending and t is not None and rec["boot_id"] == first_boot and t >= pending[0][0]:
            out += proc.calibration_start(pending.pop(0)[1])
        out += proc.feed(rec)
    out += proc.finish()
    return proc, out


# -- summaries -------------------------------------------------------------------------

CSV_COLUMNS = (
    "t_s", "emg_envelope_counts_rms", "emg_envelope_status", "activation_normalized",
    "activation_display", "activation_status", "calibration_state", "vibration_combined_m_s2",
    "vibration_status", "score_status", "score_rate_au_h", "score_cumulative_au", "notes",
)


def _latest_by_time(derived: list[dict]):
    emg, imu, score = [], [], []
    for o in derived:
        if o["type"] == "stream_features" and o["t_us"] is not None:
            (emg if o["stream"] == "emg" else imu).append(o)
        elif o["type"] == "experimental_score" and o["t_us"] is not None:
            score.append(o)
    return emg, imu, score


def _at(seq: list[dict], t_us: float, cursor: list[int]) -> dict | None:
    i = cursor[0]
    while i + 1 < len(seq) and seq[i + 1]["t_us"] <= t_us:
        i += 1
    cursor[0] = i
    return seq[i] if seq and seq[i]["t_us"] <= t_us else None


def summary_rows(derived: list[dict], step_s: float = SUMMARY_STEP_S) -> list[dict]:
    """One row per ``step_s`` of device time (boot 0 timebase; the demo has no reboot)."""
    emg, imu, score = _latest_by_time(derived)
    if not emg:
        return []
    end = max(o["t_us"] for o in emg + imu)
    events = [o for o in derived if o["type"] == "stream_event" and o["t_us"] is not None]
    rows, ce, ci, cs = [], [0], [0], [0]
    t = step_s * 1e6
    prev_t = 0.0
    while t <= end + 1:
        e, i, s = _at(emg, t, ce), _at(imu, t, ci), _at(score, t, cs)
        notes = sorted({
            (f"{ev['event']}:{ev['stream']}" if ev["stream"] else ev["event"])
            + (f"={ev['detail'].get('state')}" if "state" in ev["detail"] else "")
            for ev in events if prev_t < ev["t_us"] <= t})
        act = e["features"]["emg_activation"] if e else None
        vib = i["features"]["vibration"] if i else None
        rows.append({
            "t_s": round(t / 1e6, 2),
            "emg_envelope_counts_rms": (act["envelope"]["value"] or {}).get("emg0") if act else None,
            "emg_envelope_status": act["envelope"]["status"] if act else None,
            "activation_normalized": (act["normalized"]["value"] or {}).get("emg0") if act else None,
            "activation_display": (act["normalized"]["display"] or {}).get("emg0") if act else None,
            "activation_status": act["normalized"]["status"] if act else None,
            "calibration_state": act["calibration"]["state"] if act else None,
            "vibration_combined_m_s2": vib["physical"]["combined_rms"] if vib else None,
            "vibration_status": vib["physical"]["status"] if vib else None,
            "score_status": s["status"] if s else None,
            "score_rate_au_h": s["rate"]["value"] if s else None,
            "score_cumulative_au": s["cumulative"]["value"] if s else None,
            "notes": " ".join(notes + (["score:" + ",".join(s["reasons"])] if s and s["reasons"] else [])),
        })
        prev_t = t
        t += step_s * 1e6
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r[k] is None else r[k]) for k in CSV_COLUMNS})


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 4) if xs else None


def phase_table(derived: list[dict]) -> list[dict]:
    """Per scripted phase: medians of usable values and score accumulated in the phase."""
    emg, imu, score = _latest_by_time(derived)
    out = []
    for start, end, label in DEMO_TIMELINE:
        lo, hi = start * 1e6, end * 1e6
        es = [o for o in emg if lo < o["t_us"] <= hi]
        vs = [o for o in imu if lo < o["t_us"] <= hi]
        ss = [o for o in score if lo < o["t_us"] <= hi]
        before = [o for o in score if o["t_us"] <= lo]
        acts = [o["features"]["emg_activation"] for o in es]
        out.append({
            "phase_s": f"{start:g}-{end:g}", "label": label,
            "envelope_median": _median([(a["envelope"]["value"] or {}).get("emg0") for a in acts
                                        if a["envelope"]["status"] in ("ok", "degraded")]),
            "activation_median": _median([(a["normalized"]["value"] or {}).get("emg0") for a in acts
                                          if a["normalized"]["status"] in ("ok", "degraded")]),
            "vibration_median_m_s2": _median([o["features"]["vibration"]["physical"]["combined_rms"]
                                              for o in vs if o["features"]["vibration"]["physical"]["status"]
                                              in ("ok", "degraded")]),
            "score_accumulated_au": round(ss[-1]["cumulative"]["value"] - (before[-1]["cumulative"]["value"]
                                                                           if before else 0.0), 6) if ss else None,
            "score_valid_s": round(ss[-1]["valid_duration_s"] - (before[-1]["valid_duration_s"] if before else 0.0), 3)
            if ss else None,
        })
    return out


def text_summary(manifest: dict, derived: list[dict], phases: list[dict]) -> str:
    events = [o for o in derived if o["type"] == "stream_event"]
    final = [o for o in derived if o["type"] == "experimental_score"][-1]
    lines = [
        "HAVen offline demo -- SIMULATED DATA ONLY (synthetic surrogate signals)",
        f"provenance: {manifest['provenance']}  recording_id: {manifest['recording_id']}  "
        f"seed: {manifest['seed']}  duration: {manifest['duration_s']} s",
        "Activation is a relative proxy (not force/%MVC). Vibration is unweighted, within "
        "simulated sensor bandwidth. The score is EXPERIMENTAL, in arbitrary units.",
        "",
        "Phases (medians of usable values):",
        f"  {'phase':<9}{'envelope':>10}{'activation':>12}{'vib m/s^2':>11}{'score au':>12}{'valid s':>9}  label",
    ]
    for p in phases:
        f = lambda v, w: f"{'-' if v is None else v:>{w}}"  # noqa: E731
        lines.append(f"  {p['phase_s']:<9}{f(p['envelope_median'], 10)}{f(p['activation_median'], 12)}"
                     f"{f(p['vibration_median_m_s2'], 11)}{f(p['score_accumulated_au'], 12)}"
                     f"{f(p['score_valid_s'], 9)}  {p['label']}")
    lines += ["", "Events (device time):"]
    for e in events:
        t = "   -   " if e["t_us"] is None else f"{e['t_us'] / 1e6:7.3f}"
        d = e["detail"]
        brief = {k: d[k] for k in ("state", "reason", "missing_samples", "missing_records", "cause",
                                   "calibration_id") if k in d and d[k] is not None}
        lines.append(f"  {t}s  {e['event']:<24}{e['stream'] or '':<5} {brief if brief else ''}")
    lines += ["", "Final experimental score:",
              f"  cumulative {final['cumulative']['value']} au over {final['valid_duration_s']} s valid "
              f"({final['degraded_duration_s']} s degraded); excluded {final['excluded_duration_s']} s",
              f"  excluded by reason: {final['excluded_by_reason']}"]
    return "\n".join(lines) + "\n"


# -- run / replay -------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_ndjson(path: Path, records: Iterable[dict], validate: bool) -> int:
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in records:
            fh.write(contract.serialize_record(r) if validate else to_ndjson_line(r))
            n += 1
    return n


def run_demo(out_dir: Path, seed: int = 1, run_id: str | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    sim = Simulation(SimConfig(scenario="demo", duration_s=DEMO_DURATION_S, seed=seed), run_id=run_id)
    telemetry = [r.record for r in sim.records() if not r.lost]  # what a host would have received
    cfg = demo_processing_config()
    proc, derived = process_records(telemetry, cfg)
    paths = {name: out_dir / name for name in
             ("telemetry.ndjson", "derived.ndjson", "summary.csv", "summary.txt", "manifest.json")}
    _write_ndjson(paths["telemetry.ndjson"], telemetry, validate=True)
    _write_ndjson(paths["derived.ndjson"], derived, validate=False)
    write_csv(summary_rows(derived), paths["summary.csv"])
    phases = phase_table(derived)
    manifest = {
        "schema": "vibeguard.demo_run", "version": 1, "simulated": True,
        "provenance": "simulated", "recording_id": sim.recording_id, "identity": sim.identity,
        "seed": seed, "duration_s": DEMO_DURATION_S, "generator_version": __version__,
        "sim_config": asdict(sim.config), "sim_summary": sim.summary.to_dict(),
        "processing_config": cfg.to_dict(),
        "calibration_schedule": [{"device_time_s": t, "phase": ph, "boot": 0}
                                 for t, ph in CALIBRATION_SCHEDULE],
        "calibration_record": proc.calibration_record(),
        "phases": phases,
        "final_score": [o for o in derived if o["type"] == "experimental_score"][-1],
    }
    paths["summary.txt"].write_text(text_summary(manifest, derived, phases), encoding="utf-8")
    manifest["files"] = {n: _sha256(p) for n, p in paths.items() if n != "manifest.json"}
    paths["manifest.json"].write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"paths": paths, "manifest": manifest, "derived": derived, "telemetry": telemetry}


def _strip_provenance(o):
    if isinstance(o, dict):
        return {k: ("<calibration_id>" if k == "calibration_id" and v else _strip_provenance(v))
                for k, v in o.items() if k != "provenance"}
    if isinstance(o, list):
        return [_strip_provenance(x) for x in o]
    return o


def replay_demo(out_dir: Path) -> dict:
    """Re-process the recorded telemetry as provenance=replay; compare with the original."""
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    with (out_dir / "telemetry.ndjson").open("rb") as fh:
        recorded = [rec for _, rec, issues in contract.iter_ndjson(fh) if not issues]
    replayed = [to_replay(r) for r in recorded]
    schedule = [(c["device_time_s"], c["phase"]) for c in manifest["calibration_schedule"]]
    _, derived = process_records(replayed, demo_processing_config(), schedule)
    original = [json.loads(line) for line in (out_dir / "derived.ndjson").read_text().splitlines()]
    _write_ndjson(out_dir / "replay_derived.ndjson", derived, validate=False)
    write_csv(summary_rows(derived), out_dir / "replay_summary.csv")
    provs = {json.dumps(o["provenance"], sort_keys=True) for o in derived if o.get("provenance")}
    return {
        "records": len(replayed),
        "matches_original": _strip_provenance(derived) == _strip_provenance(original),
        "replay_provenance": sorted(provs),
        "device_time_span_s": (device_time_us(recorded[-1]) or 0) / 1e6,
        "recording_id": manifest["recording_id"],
    }
