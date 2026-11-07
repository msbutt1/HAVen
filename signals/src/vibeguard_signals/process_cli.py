"""vg-process: run contract-v1 NDJSON through the host reference processor.

Reads NDJSON (files or '-' for stdin), writes derived NDJSON
(stream_features / stream_event) to stdout or --output, and prints a summary
to stderr. Needs no hardware and no backend.

--clock device (default): deterministic; staleness judged on device time.
--clock host: for live input. A reader thread stamps each line with its host
receive time; the main loop also calls poll() every --poll-interval seconds
while input is silent, so total silence becomes visible before EOF. Output is
flushed after every write.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time

from . import contract
from .processing import ProcessingConfig, Processor, to_ndjson_line

_EOF = object()


def _open(name: str):
    return sys.stdin.buffer if name == "-" else open(name, "rb")


def _feed_line(proc: Processor, name: str, lineno: int, line: bytes, rx: float | None) -> list[dict]:
    record, issues = contract.parse_line(line)
    if record is None:
        return proc.feed_invalid_frame(issues, f"{name}:{lineno}")
    return proc.feed(record, rx_time_s=rx)


def _run_device(proc: Processor, inputs: list[str], emit) -> None:
    for name in inputs:
        fh = _open(name)
        try:
            for lineno, line in enumerate(fh, start=1):
                emit(_feed_line(proc, name, lineno, line, None))
        finally:
            if fh is not sys.stdin.buffer:
                fh.close()


def _run_host(proc: Processor, inputs: list[str], emit, poll_interval: float) -> None:
    q: queue.Queue = queue.Queue(maxsize=1024)
    failure: list[BaseException] = []

    def reader() -> None:
        try:
            for name in inputs:
                fh = _open(name)
                try:
                    for lineno, line in enumerate(fh, start=1):
                        q.put((name, lineno, line, time.monotonic()))
                finally:
                    if fh is not sys.stdin.buffer:
                        fh.close()
        except BaseException as exc:  # surfaced in the main thread
            failure.append(exc)
        finally:
            q.put(_EOF)

    threading.Thread(target=reader, name="vg-process-reader", daemon=True).start()
    while True:
        try:
            item = q.get(timeout=poll_interval)
        except queue.Empty:
            emit(proc.poll(time.monotonic()))
            continue
        if item is _EOF:
            break
        name, lineno, line, rx = item
        emit(_feed_line(proc, name, lineno, line, rx) + proc.poll(time.monotonic()))
    if failure:
        raise failure[0]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vg-process", description=__doc__.splitlines()[0])
    p.add_argument("inputs", nargs="+", help="NDJSON telemetry files, or '-' for stdin")
    p.add_argument("-o", "--output", default="-", help="derived NDJSON output (default stdout)")
    p.add_argument("--clock", choices=("device", "host"), default="device",
                   help="staleness clock: device time (deterministic, default) or host receive time")
    p.add_argument("--poll-interval", type=float, default=0.05,
                   help="host clock: seconds between staleness polls while input is silent")
    p.add_argument("--summary-json", default=None, help="write the processing summary here")
    args = p.parse_args(argv)
    if not 0 < args.poll_interval <= 5:
        print("vg-process: --poll-interval must be in (0, 5] s", file=sys.stderr)
        return 2

    proc = Processor(ProcessingConfig(clock=args.clock))
    out = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8", newline="\n")
    live = args.clock == "host"
    n_out = 0

    def emit(records: list[dict]) -> None:
        nonlocal n_out
        for r in records:
            out.write(to_ndjson_line(r))
        n_out += len(records)
        if live and records:
            out.flush()

    try:
        if live:
            _run_host(proc, args.inputs, emit, args.poll_interval)
        else:
            _run_device(proc, args.inputs, emit)
        emit(proc.finish())
        out.flush()
    except BrokenPipeError:
        return 0
    except OSError as exc:
        print(f"vg-process: {exc}", file=sys.stderr)
        return 2
    finally:
        if out is not sys.stdout:
            out.close()

    summary = proc.summary.to_dict()
    print(f"vg-process: {summary['records_accepted']}/{summary['records_in']} input records "
          f"accepted, {n_out} output records, clock={args.clock}", file=sys.stderr)
    print(f"vg-process:   events: {summary['events']}", file=sys.stderr)
    for s, c in summary["feature_status"].items():
        print(f"vg-process:   {s} feature status: {c}  buffer max/capacity: "
              f"{summary['buffer_max_occupancy'].get(s)}/{summary['buffer_capacity'].get(s)}",
              file=sys.stderr)
    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
