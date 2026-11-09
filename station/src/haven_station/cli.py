"""``vg-station``: station test runner.

    vg-station run --mode preview --seed 1 --out out/station   # instant SIMULATED session
    vg-station examples --out contract/examples                # regenerate example payloads
    vg-station ports                                           # list serial ports
    vg-station selftest --port COM5                            # first check after wiring
    vg-station live --port COM5 --mode preview                 # real-time test on the controller
    vg-station live --simulate --mode preview                  # same path, no hardware

``run`` always uses the simulated adapter. ``live`` and ``selftest`` talk to the controller
(or, with --simulate, a simulated one); simulated runs are labelled simulated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import SimulatedSubject
from .config import StationConfig
from .examples import build_examples
from .simulation import run_simulated
from .store import ResultStore, result_json


def _ndjson(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":"), allow_nan=False, ensure_ascii=False) + "\n")


def _summary(result: dict) -> str:
    th = result["threshold"]
    lines = [
        f"session   {result['session_id']}  [{result['provenance']['source'].upper()}]  mode={result['mode']}",
        f"state     {result['state']}  outcome={result['outcome']}",
        f"label     {result['result_label']}",
        "threshold " + (f"{th['value']} {th['units']} ({th['method']})" if th else "none"),
        f"reversals {result['reversals']['count']}/{result['reversals']['required']}  levels={result['reversals']['levels']}",
        f"trials    {result['trial_counts']}  catch={result['catch_trials']}",
        "limits    " + ", ".join(x["code"] for x in result["quality"]["limitations"]),
    ]
    return "\n".join(lines)


def cmd_run(args) -> int:
    import math
    for name in ("true_threshold", "miss_rate", "false_positive_rate"):
        if not math.isfinite(getattr(args, name)):
            print(f"error: --{name.replace('_', '-')} must be a finite number", file=sys.stderr)
            return 2
    cfg = StationConfig.preview() if args.mode == "preview" else StationConfig.full()
    subject = SimulatedSubject(args.true_threshold, miss_rate=args.miss_rate,
                               false_positive_rate=args.false_positive_rate, seed=args.seed)
    out = Path(args.out)
    store = ResultStore(out)
    session_id = args.session_id or f"sim-{args.mode}-seed{args.seed}"
    detail = {"seed": args.seed, "true_threshold": args.true_threshold, "miss_rate": args.miss_rate,
              "false_positive_rate": args.false_positive_rate}
    engine, _ = run_simulated(subject, cfg, seed=args.seed, session_id=session_id, sink=store,
                              abort_after_trials=args.abort_after, detail=detail)
    out.mkdir(parents=True, exist_ok=True)
    _ndjson(out / f"{session_id}.trials.ndjson", engine.result["trials"])
    _ndjson(out / f"{session_id}.events.ndjson", engine.event_log)
    print(_summary(engine.result))
    if not engine.saved:
        print(f"NOT SAVED  {engine.save_error}", file=sys.stderr)
        return 1
    print(f"saved     {store.path_for(session_id)}" + ("  (already existed; not overwritten)" if engine.already_saved else ""))
    return 0 if engine.result["state"] in ("completed", "aborted") else 1


def cmd_examples(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    examples = build_examples()
    for name, obj in examples.items():
        (out / f"{name}.json").write_text(result_json(obj), encoding="utf-8", newline="\n")
    print(f"wrote {len(examples)} example payloads to {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vg-station", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one simulated session and save its result")
    run.add_argument("--mode", choices=("full", "preview"), default="full")
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--true-threshold", type=float, default=35.0, help="simulated person's threshold (motor setting)")
    run.add_argument("--miss-rate", type=float, default=0.0, help="fraction of trials with no response")
    run.add_argument("--false-positive-rate", type=float, default=0.0, help="P(felt) on catch trials")
    run.add_argument("--abort-after", type=int, default=None, help="abort once N trials have completed")
    run.add_argument("--session-id", default=None)
    run.add_argument("--out", default="out/station")
    run.set_defaults(func=cmd_run)
    from .hardware_cli import cmd_live, cmd_ports, cmd_selftest
    sub.add_parser("ports", help="list serial ports").set_defaults(func=cmd_ports)
    for name, fn, helptext in (("selftest", cmd_selftest, "check wiring: hello, stop, motor pulses, buttons"),
                               ("live", cmd_live, "run a real-time test on the controller")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--port", default=None, help="serial port, e.g. COM5")
        sp.add_argument("--baud", type=int, default=115200)
        sp.add_argument("--simulate", action="store_true", help="use a simulated controller (no hardware)")
        if name == "selftest":
            sp.add_argument("--no-motor", action="store_true", help="skip the motor pulses")
            sp.add_argument("--button-wait", type=float, default=15.0, help="seconds to wait for each button")
        else:
            sp.add_argument("--mode", choices=("full", "preview"), default="preview")
            sp.add_argument("--seed", type=int, default=1)
            sp.add_argument("--session-id", default=None)
            sp.add_argument("--results-dir", default="results")
        sp.set_defaults(func=fn)
    ex = sub.add_parser("examples", help="regenerate contract/examples/*.json")
    ex.add_argument("--out", default="contract/examples")
    ex.set_defaults(func=cmd_examples)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
