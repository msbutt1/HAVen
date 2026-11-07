"""Labelled SIMULATED history for the dashboard's longitudinal view.

Every row created here has ``source = "simulated"`` and ``simulated_history = true``.
Dates are synthetic (spread over past days). Wearable sessions are produced by
the real simulator + processor, so their files replay like any other session;
station sessions get synthetic felt/not-felt responses. None of it is a measurement.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from vibeguard_signals import contract
from vibeguard_signals.demo import demo_processing_config, process_records
from vibeguard_signals.simulator import SimConfig, Simulation

from .adapters import new_id
from .runtime import SessionRecorder, session_score_summary
from .service import Backend, station_summary

# (scenario, relaxed capture at, squeeze capture at) in device seconds for a 30 s run: each
# squeeze capture falls inside that scenario's stronger-activation phase, so calibration succeeds.
SCENARIOS = (("demo", 0.5, 4.5), ("happy_path", 0.5, 6.5), ("strong_activation", 0.5, 10.0))


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seed_history(backend: Backend, days: int = 14, seed: int = 7, duration_s: float = 30.0) -> dict:
    """Create one simulated wearable session (and one station session) per day."""
    rng = random.Random(seed)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    created = {"wearable_sessions": [], "station_sessions": []}
    config = demo_processing_config()
    for d in range(days, 0, -1):
        start = now - timedelta(days=d, hours=rng.randint(0, 6))
        scenario, t_relaxed, t_squeeze = SCENARIOS[d % len(SCENARIOS)]
        scale = duration_s / 30.0
        schedule = ((t_relaxed * scale, "relaxed"), (t_squeeze * scale, "squeeze"))
        sim = Simulation(SimConfig(scenario=scenario, seed=seed * 1000 + d, duration_s=duration_s),
                         run_id=f"hist{seed}-{d}")
        sim_records = [r for r in sim.records() if not r.lost]
        records = [r.record for r in sim_records]
        proc, derived = process_records(records, config, schedule)  # already ends with finish()
        sid = new_id("ws")
        rec = SessionRecorder(backend.data_dir / "sessions" / sid)
        for r in sim_records:  # receive time follows the simulated emission time
            rec.raw(contract.serialize_record(r.record).encode(),
                    _iso(start + timedelta(microseconds=r.sim_time_us)), r.sim_time_us / 1e6)
        for out in derived:
            rec.derived(out)
        for t_s, phase in schedule:
            # same schedule as the demo; recorded so replays recalibrate at the same point
            after = None
            for r in records:
                t = r.get("timing")
                first = None if t is None else (t["t0_us"] if t["mode"] == "uniform" else t["t_us"][0])
                if first is not None and first / 1e6 >= t_s:
                    break
                after = r
            rec.command({"action": phase, "at": None, "scheduled": False, "after_boot_id": after and after["boot_id"],
                         "after_seq": after and after["seq"]})
        files = rec.close()
        scores = [o for o in derived if o["type"] == "experimental_score"]
        boots = sorted({r["boot_id"] for r in records})
        backend.store.create_wearable_session({
            "session_id": sid, "runtime": "simulated-history", "device_id": records[0]["device_id"],
            "state": "recording", "label": f"SIMULATED history day -{d} ({scenario})",
            "source": "simulated", "recording_id": sim.recording_id, "simulated_history": True,
            "started_at": _iso(start), "dir": str(rec.dir), "boot_ids": boots})
        row = backend.store.update_wearable_session(
            sid, state="completed", end_reason="simulated_history", ended_at=_iso(start + timedelta(seconds=duration_s)),
            calibration_id=(proc.calibration_record() or {}).get("calibration_id"),
            score=session_score_summary(None, scores[0] if scores else None, scores[-1] if scores else None),
            counters={"frames": len(records), "derived_records": len(derived), "simulated": True},
            files=files)
        (rec.dir / "manifest.json").write_text(json.dumps(
            {"session": row, "processing_config": config.to_dict(), "simulated_history": True,
             "scenario": scenario}, indent=2, default=str) + "\n")
        created["wearable_sessions"].append(sid)

        st = backend.store.create_station_session({
            "station_session_id": new_id("ss"), "source": "simulated", "simulated_history": True,
            "label": f"SIMULATED perception test day -{d}", "wearable_session_id": sid,
            "protocol": {"kind": "simulated_staircase_preview", "note": "synthetic responses"},
            "started_at": _iso(start + timedelta(minutes=5))})
        setting = 160
        for i in range(10):
            is_catch = i in (3, 7)
            felt = rng.random() < (0.1 if is_catch else min(0.95, max(0.05, (setting - 60) / 120)))
            backend.store.add_station_trial({
                "trial_id": new_id("tr"), "station_session_id": st["station_session_id"], "trial_index": i,
                "stimulus": {"motor_setting": 0 if is_catch else setting, "units": "pwm_duty_0_255",
                             "note": "setting, not a measured amplitude"},
                "is_catch": is_catch, "response": "felt" if felt else "not_felt",
                "presented_at": _iso(start + timedelta(minutes=5, seconds=6 * i)),
                "responded_at": _iso(start + timedelta(minutes=5, seconds=6 * i + 1)),
                "response_latency_ms": round(rng.uniform(350, 900), 1)})
            if not is_catch:
                setting = max(40, setting - 20) if felt else min(255, setting + 20)
        backend.store.finish_station_session(
            st["station_session_id"], "completed", "simulated_history",
            station_summary(backend.store.station_trials(st["station_session_id"])),
            ended_at=_iso(start + timedelta(minutes=6)))
        created["station_sessions"].append(st["station_session_id"])
    return created
