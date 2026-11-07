"""vg-replay: re-emit a recorded telemetry NDJSON file as REPLAY (offline verification).

Device fields and timestamps are unchanged; provenance becomes
source=replay with the original source and recording_id preserved. Pacing is
declared on stderr: --fast (default, no waiting) or --speed X (X times the
recording's device-time rate). Pacing never changes record contents.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import contract
from .replay import ReplayError, replay_lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vg-replay", description=__doc__.splitlines()[0])
    p.add_argument("input", help="recorded contract-v1 NDJSON file ('-' for stdin)")
    p.add_argument("-o", "--output", default="-", help="replayed NDJSON (default stdout)")
    pace = p.add_mutually_exclusive_group()
    pace.add_argument("--fast", action="store_true", help="no wall-clock pacing (default)")
    pace.add_argument("--speed", type=float, default=None,
                      help="wall-clock pacing factor relative to recording time (1 = as recorded)")
    p.add_argument("--recording-id", default=None,
                   help="identity to use if the recording's records carry none")
    args = p.parse_args(argv)
    if args.speed is not None and args.speed <= 0:
        print("vg-replay: --speed must be > 0", file=sys.stderr)
        return 2
    pacing = "none (--fast)" if args.speed is None else f"x{args.speed:g} of recording time"
    print(f"vg-replay: REPLAY of {args.input}; wall-clock pacing: {pacing}; "
          "device timestamps and identities unchanged", file=sys.stderr)

    src = sys.stdin.buffer if args.input == "-" else open(args.input, "rb")
    out = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="\n")
    skipped: list = []
    n = 0
    start = time.monotonic()
    try:
        for clock, rec in replay_lines(src, args.recording_id, skipped):
            if args.speed is not None:
                delay = start + clock / args.speed - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            out.write(contract.serialize_record(rec))
            if args.speed is not None:
                out.flush()
            n += 1
    except ReplayError as exc:
        print(f"vg-replay: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    finally:
        if src is not sys.stdin.buffer:
            src.close()
        if out is not sys.stdout:
            out.close()
    for lineno, issues in skipped:
        print(f"vg-replay: skipped invalid line {lineno}: {issues[0]}", file=sys.stderr)
    print(f"vg-replay: {n} records replayed, {len(skipped)} invalid lines skipped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
