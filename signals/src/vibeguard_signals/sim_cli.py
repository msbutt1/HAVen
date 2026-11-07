"""vg-sim: deterministic SIMULATED wearable emitting device telemetry contract v1 NDJSON.

Telemetry goes to stdout (or --output); diagnostics go to stderr. Output is
paced in wall time by default (--speed scales it); --fast emits without
waiting. Pacing never changes record contents: the same seed and config give
byte-identical output in either mode.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import fields

from . import contract
from .simulator import SCENARIOS, SimConfig, Simulation

_DEFAULTS = SimConfig()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vg-sim", description=__doc__.splitlines()[0])
    p.add_argument("--scenario", default=_DEFAULTS.scenario, help="scenario name (see --list)")
    p.add_argument("--list", action="store_true", help="list scenarios and exit")
    p.add_argument("--seed", type=int, default=_DEFAULTS.seed)
    p.add_argument("--duration", type=float, default=_DEFAULTS.duration_s,
                   help="simulated run length in seconds (finite; max 3600)")
    p.add_argument("--emg-rate", type=float, default=_DEFAULTS.emg_rate_hz,
                   help="synthetic EMG sample rate, Hz")
    p.add_argument("--imu-rate", type=float, default=_DEFAULTS.imu_rate_hz,
                   help="synthetic IMU sample rate, Hz")
    p.add_argument("--batch-ms", type=float, default=_DEFAULTS.batch_ms)
    p.add_argument("--device-id", default=_DEFAULTS.device_id)
    p.add_argument("--run-id", default=None,
                   help="fix the run identity (recording_id/boot_id) for reproducible fixtures; "
                        "default is a fresh random identity per run")
    p.add_argument("--vibration-hz", type=float, default=None,
                   help="override the scenario's synthetic vibration frequency")
    p.add_argument("-o", "--output", default="-", help="output file (default '-' = stdout)")
    pace = p.add_mutually_exclusive_group()
    pace.add_argument("--fast", action="store_true", help="emit as fast as possible (no wall pacing)")
    pace.add_argument("--speed", type=float, default=1.0,
                      help="wall-clock pacing factor (2 = twice real time); does not alter device time")
    p.add_argument("--summary-json", default=None,
                   help="also write the run summary (counts of injected faults) to this file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        for s in SCENARIOS.values():
            print(f"{s.name:<22}{s.description}")
        return 0
    if args.speed <= 0:
        print("vg-sim: --speed must be > 0", file=sys.stderr)
        return 2
    cfg = SimConfig(scenario=args.scenario, seed=args.seed, duration_s=args.duration,
                    emg_rate_hz=args.emg_rate, imu_rate_hz=args.imu_rate, batch_ms=args.batch_ms,
                    device_id=args.device_id, vibration_hz=args.vibration_hz)
    try:
        sim = Simulation(cfg, run_id=args.run_id)
    except ValueError as exc:
        print(f"vg-sim: {exc}", file=sys.stderr)
        return 2

    pacing = "fast (no wall pacing)" if args.fast else f"wall-paced x{args.speed:g}"
    print(f"vg-sim: SIMULATED data, recording_id={sim.recording_id}, scenario={cfg.scenario}, "
          f"identity={sim.identity}, seed={cfg.seed}, duration={cfg.duration_s:g}s, emg={cfg.emg_rate_hz:g}Hz, "
          f"imu={cfg.imu_rate_hz:g}Hz, {pacing}", file=sys.stderr)

    out = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="\n")
    start = time.monotonic()
    try:
        for r in sim.records():
            if r.lost:
                continue
            if not args.fast:
                delay = start + r.sim_time_us / 1e6 / args.speed - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            out.write(contract.serialize_record(r.record))
            if not args.fast:
                out.flush()
    except BrokenPipeError:
        return 0
    except contract.ContractViolation as exc:  # defensive: should be unreachable
        print(f"vg-sim: internal error, simulator produced an invalid record ({exc}); "
              "output is incomplete", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("vg-sim: interrupted; output is truncated", file=sys.stderr)
        return 130
    finally:
        if out is not sys.stdout:
            out.close()

    s = sim.summary
    print(f"vg-sim: {s.records_emitted}/{s.records_generated} records emitted "
          f"({s.records_lost} lost in simulated transport {s.lost_by_type or ''}), "
          f"boots={len(s.boots)}", file=sys.stderr)
    for name in ("emg", "imu"):
        st = getattr(s, name)
        detail = ", ".join(f"{f.name}={getattr(st, f.name)}" for f in fields(st))
        print(f"vg-sim:   {name}: {detail}", file=sys.stderr)
    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as fh:
            json.dump({"config": vars(args), **s.to_dict()}, fh, indent=2)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
