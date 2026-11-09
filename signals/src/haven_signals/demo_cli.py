"""vg-demo: offline end-to-end reference demo (SIMULATED DATA ONLY).

  vg-demo run    [--out DIR] [--seed N] [--run-id ID]   simulate, process, save outputs
  vg-demo replay DIR                                    re-process DIR/telemetry.ndjson as replay

Outputs in DIR: telemetry.ndjson (simulated device stream as received),
derived.ndjson (processor output), summary.csv (one row per 0.5 s),
summary.txt (phases, events, final score), manifest.json (config, seed,
calibration schedule, file hashes). Replay adds replay_derived.ndjson and
replay_summary.csv.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .demo import replay_demo, run_demo

DEFAULT_OUT = Path("out/demo")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vg-demo", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="simulate, process, and save the scripted 30 s demo")
    r.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output directory (default {DEFAULT_OUT})")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--run-id", default=None, help="fixed run identity (default: random per run)")
    r.add_argument("--quiet", action="store_true", help="do not print summary.txt")
    rp = sub.add_parser("replay", help="re-process a saved demo recording as provenance=replay")
    rp.add_argument("dir", type=Path)
    args = p.parse_args(argv)

    try:
        if args.cmd == "run":
            res = run_demo(args.out, seed=args.seed, run_id=args.run_id)
            if not args.quiet:
                print(res["paths"]["summary.txt"].read_text(encoding="utf-8"))
            print(f"vg-demo: SIMULATED run saved to {args.out}/ "
                  f"({len(res['telemetry'])} telemetry records, {len(res['derived'])} derived records)",
                  file=sys.stderr)
        else:
            res = replay_demo(args.dir)
            print(f"vg-demo: replayed {res['records']} records of recording {res['recording_id']} "
                  f"(device-time span {res['device_time_span_s']:.3f} s, no wall pacing)")
            print(f"vg-demo: replay provenance: {', '.join(res['replay_provenance'])}")
            print(f"vg-demo: derived output matches the original apart from provenance: "
                  f"{'YES' if res['matches_original'] else 'NO'}")
            print(f"vg-demo: wrote {args.dir}/replay_derived.ndjson and {args.dir}/replay_summary.csv")
            return 0 if res["matches_original"] else 1
    except (OSError, ValueError) as exc:
        print(f"vg-demo: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
