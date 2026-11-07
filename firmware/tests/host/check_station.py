"""Station protocol checks (ctest: station_protocol).

1. Every event from the C unit tests validates (station_protocol.py).
2. A real-time session with vg-fake-station: events validate, a stimulus lasts
   its requested time, a duplicate never restarts it, abort ends it promptly,
   and the documented example commands are valid.

usage: python check_station.py BUILD_DIR
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "contract"))
import station_protocol as sp  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAIL.append(msg)
        print("  FAIL", msg)


def cmd(type_: str, rid: str, **kw) -> str:
    c = {"schema": sp.CMD_SCHEMA, "version": 1, "type": type_, "request_id": rid, **kw}
    check(not sp.validate_command(c), f"example command invalid: {c}")
    return json.dumps(c, separators=(",", ":")) + "\n"


def main() -> int:
    build = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "ev.ndjson"
        r = subprocess.run([str(build / "test_station"), "--emit", str(out)], capture_output=True)
        check(r.returncode == 0, "test_station failed")
        evs = [sp.parse(l) for l in out.read_text().splitlines() if l.strip()]
        bad = [(i, sp.validate_event(e)) for i, e in enumerate(evs) if sp.validate_event(e)]
        check(not bad, f"unit-test events invalid: {bad[:3]}")
        check(not sp.check_sequence(evs), f"sequence: {sp.check_sequence(evs)[:3]}")
        print(f"[unit] {len(evs)} events valid")

    p = subprocess.Popen([str(build / "vg-fake-station")], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def send(line: str) -> None:
        p.stdin.write(line)
        p.stdin.flush()

    time.sleep(0.2)
    send(cmd("status", "q1"))
    send(cmd("stimulus", "t1", level=40, duration_ms=300))
    time.sleep(0.1)
    send(cmd("stimulus", "t1", level=40, duration_ms=300))       # duplicate: no restart
    send(cmd("stimulus", "t9", level=40, duration_ms=300))       # busy
    time.sleep(0.4)
    send(cmd("stimulus", "t2", level=80, duration_ms=900))
    time.sleep(0.2)
    send(cmd("abort", "a1"))
    send('{"schema":"vibeguard.station_command","version":1,"type":"stimulus"}\n')  # malformed
    send(cmd("stimulus", "t3", level=100, duration_ms=5000))    # over max: rejected
    time.sleep(0.2)
    p.stdin.close()                                              # link closed
    lines = p.stdout.read().splitlines()
    p.wait(timeout=5)
    evs = [sp.parse(l) for l in lines]
    bad = [(i, sp.validate_event(e)) for i, e in enumerate(evs) if sp.validate_event(e)]
    check(not bad, f"session events invalid: {bad[:3]}")
    check(not sp.check_sequence(evs), f"session sequence: {sp.check_sequence(evs)[:3]}")

    by = lambda t, rid=None: [e for e in evs if e["type"] == t and (rid is None or e.get("request_id") == rid)]
    started_t1 = by("stimulus_started", "t1")
    ended_t1 = by("stimulus_ended", "t1")
    check(len(started_t1) == 1, "t1 must start exactly once (duplicate must not restart)")
    check(len(ended_t1) == 1 and ended_t1[0]["outcome"] == "completed", "t1 must complete")
    if ended_t1:
        on = ended_t1[0]["on_ms"]
        check(300 <= on <= 320, f"t1 on_ms {on} not within 300..320 ms (real-time host)")
    acks = {(e["request_id"], e["result"], e["reason"]) for e in by("ack")}
    check(("t1", "duplicate", None) in acks, "duplicate ack for t1")
    check(("t9", "rejected", "busy") in acks, "busy rejection for t9")
    check(("t3", "rejected", "duration_out_of_range") in acks, "t3 over max must be rejected")
    ended_t2 = by("stimulus_ended", "t2")
    abort_ack = [e for e in by("ack", "a1")]
    check(len(ended_t2) == 1 and ended_t2[0]["outcome"] == "aborted", "t2 must be aborted")
    if ended_t2 and abort_ack:
        lat = (ended_t2[0]["t_us"] - abort_ack[0]["t_us"]) / 1000
        check(0 <= lat <= 5, f"abort latency {lat} ms (device clock) > 5 ms")
    check(any(e["type"] == "error" and e["reason"] == "invalid_request_id" for e in evs),
          "command without request_id must give an invalid_request_id error")
    check(all(e["provenance"]["source"] == "simulated" for e in evs), "fake station must be simulated")
    print(f"[session] {len(evs)} events valid; t1 on_ms={ended_t1[0]['on_ms'] if ended_t1 else None}")

    examples = HERE.parent.parent / "contract" / "examples" / "commands.ndjson"
    if examples.exists():
        for n, line in enumerate(examples.read_text().splitlines(), 1):
            if line.strip():
                check(not sp.validate_command(sp.parse(line)), f"examples/commands.ndjson:{n} invalid")
    print("OK" if not FAIL else f"FAILED: {len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
