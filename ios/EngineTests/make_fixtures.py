"""Make the reference fixtures for the Swift engine tests from a REAL sleeve recording.

Runs slices of a raw recording through the Python processor (the reference implementation), with the
calibration commands injected exactly where they were pressed during the recording, and stores the
input lines, the commands and the Python outputs. The Swift tests feed the same lines and commands
and must reproduce the outputs.

    cd signals && uv run python ../ios/EngineTests/make_fixtures.py recordings/gun-raw-2026-11-08.ndjson \
        recordings/gun-derived-2026-11-08.ndjson
"""

import dataclasses
import json
import sys
from pathlib import Path

from haven_signals import contract
from haven_signals.bridge import live_config
from haven_signals.processing import Processor

OUT = Path(__file__).parent / "Fixtures"
# (name, first seq, last seq) of the slices; all from one boot of board W, 2026-11-08
SLICES = [("grip_calibration", 79150, 79900), ("tool_calibration", 91700, 93500)]
GRIP = {"capturing_relaxed": "relaxed", "capturing_squeeze": "squeeze"}
TOOL = {"capturing_still": "toolcal:still", "capturing_movement": "toolcal:movement",
        "capturing_tool": "toolcal:tool", "uncalibrated": "toolcal:reset"}


def commands(derived: Path, boot: str) -> dict[int, list[str]]:
    """seq after which each command was given (the processor's last seq when it ran)."""
    out: dict[int, list[str]] = {}
    for line in derived.open():
        o = json.loads(line)
        if o.get("type") != "stream_event" or o.get("boot_id") != boot:
            continue
        st = (o.get("detail") or {}).get("state")
        cmd = (GRIP.get(st) if o["event"] == "calibration_state" else
               TOOL.get(st) if o["event"] == "tool_calibration_state" else None)
        if cmd:
            out.setdefault(o["source_seq"], []).append(cmd)
    return out


def r(v, d=4):
    return None if v is None else round(float(v), d)


def main(raw: str, derived: str) -> None:
    lines = Path(raw).read_bytes().splitlines()
    boot = None
    for name, lo, hi in SLICES:
        cfg = dataclasses.replace(live_config(4096.0), clock="device")
        proc = Processor(cfg)
        sel = []
        for ln in lines:
            o = json.loads(ln)
            if lo <= o.get("seq", -1) <= hi and o.get("type") in ("emg_batch", "imu_batch", "status"):
                boot = boot or o["boot_id"]
                if o["boot_id"] == boot:
                    sel.append(ln)
        hello = next(ln for ln in lines if b'"type":"hello"' in ln and boot.encode() in ln)
        sel = [hello] + sel
        cmds = commands(Path(derived), boot)
        expected, events = [], []
        for ln in sel:
            rec, issues = contract.parse_line(ln)
            outs = proc.feed(rec)
            for c in cmds.get(rec["seq"], []) if rec["type"] != "hello" else []:
                if c.startswith("toolcal:"):
                    ph = c[8:]
                    outs += (proc.tool_calibration_reset() if ph == "reset" else proc.tool_calibration_start(ph))
                else:
                    outs += proc.calibration_start(c)
            for o in outs:
                t = o["type"]
                if t == "stream_features" and o["stream"] == "emg":
                    a = o["features"]["emg_activation"]
                    expected.append({"seq": o["source_seq"], "stream": "emg",
                                     "envelope": (a["envelope"]["value"] or {}).get("emg0"),
                                     "envelope_status": a["envelope"]["status"],
                                     "activation": (a["normalized"]["value"] or {}).get("emg0"),
                                     "calibration": a["calibration"]["state"]})
                elif t == "stream_features" and o["stream"] == "imu":
                    p = o["features"]["vibration"]["physical"]
                    expected.append({"seq": o["source_seq"], "stream": "imu",
                                     "vibration": p["combined_rms"], "vibration_status": p["status"]})
                elif t == "tool_use" and o.get("source_seq") is not None:
                    expected.append({"seq": o["source_seq"], "stream": "tool", "state": o["state"],
                                     "tool": o["tool_band_rms_ms2"], "motion": o["motion_band_rms_ms2"],
                                     "dominant_hz": o["dominant_hz"], "conc": o["spectral_concentration"],
                                     "steadiness": o.get("steadiness"),
                                     "session_tool_s": o["session_tool_s"]})
                elif t == "exposure" and o.get("trigger") == "imu":
                    expected.append({"seq": o["source_seq"], "stream": "exposure",
                                     "points": o["daily"]["points"], "a8": o["daily"]["a8_ms2"],
                                     "contact_s": o["daily"]["contact_s"],
                                     "tool_on_points": o["by_gate"]["tool_on"]["points"],
                                     "level": o["level"], "counting": o["current"]["counting"]})
                elif t == "experimental_score" and o.get("trigger") == "imu":
                    expected.append({"seq": o["source_seq"], "stream": "score",
                                     "cumulative": o["cumulative"]["value"], "rate": o["rate"]["value"]})
                elif t == "stream_event" and o["event"] in ("calibration_state", "tool_calibration_state"):
                    d = o["detail"]
                    events.append({"event": o["event"], "state": d.get("state"), "reason": d.get("reason"),
                                   "thresholds": d.get("thresholds")})
        fixture = {"note": "REAL recording from board W (live), processed by the Python reference processor",
                   "lines": [ln.decode() for ln in sel],
                   "commands": {str(k): v for k, v in cmds.items() if lo <= k <= hi},
                   "expected": expected, "events": events}
        (OUT / f"{name}.json").write_text(json.dumps(fixture))
        print(name, len(sel), "lines,", len(expected), "outputs,", [(e["state"], e["reason"]) for e in events])


if __name__ == "__main__":
    main(*sys.argv[1:3])
