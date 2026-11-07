"""Drive one full calibrated session through a RUNNING backend and print what happened.

SIMULATED device. Needs `vg-backend serve` running (default speed 1, demo scenario).

    cd backend && uv run python scripts/demo_calibrated_session.py [--url http://127.0.0.1:8765]

The calibration is a device-time schedule inside one 30 s simulated run, so the script
waits for a run to start, then follows the stream: it starts a session, sends the relaxed
capture at 0.5 s of device time and the squeeze capture at 4.5 s, lets the run finish,
stops the session and replays it. It checks that the replay reproduces the score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

import websockets

RELAXED_AT_S, SQUEEZE_AT_S, RUN_END_S = 0.5, 4.5, 29.6


def call(base: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(base + path, method=method, headers={"content-type": "application/json"},
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {path} -> {exc.code}: {exc.read().decode()}")


def say(text: str) -> None:
    print(text, flush=True)


async def drive(base: str) -> dict:
    ws_url = base.replace("http", "ws", 1) + "/ws/live?runtime=wearable&channels=derived,runtime"
    session = None
    sent = set()
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.recv()  # snapshot
        say("Waiting for a fresh simulated run to start (the demo repeats every ~30 s)...")
        boot = None
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 60))
            p = msg["payload"]
            if msg["channel"] != "derived" or p.get("t_us") is None:
                continue
            t_s = p["t_us"] / 1e6
            if session is None:
                if t_s < 0.4:  # early in a run: start recording now so the whole run is captured
                    boot = p["boot_id"]
                    session = call(base, "POST", "/api/sessions", {"label": "calibrated demo"})
                    say(f"Started session {session['session_id']} at device time {t_s:.2f} s "
                        f"(source: {session['source']})")
                continue
            if p["boot_id"] != boot or t_s >= RUN_END_S:
                break  # stop just before the run ends: the next boot would (correctly) invalidate the calibration
            if "relaxed" not in sent and t_s >= RELAXED_AT_S:
                sent.add("relaxed")
                call(base, "POST", "/api/runtimes/wearable/calibration", {"action": "relaxed"})
                say(f"  t={t_s:5.2f} s  -> relaxed capture started (keep the muscle relaxed for 3 s)")
            if "squeeze" not in sent and t_s >= SQUEEZE_AT_S:
                sent.add("squeeze")
                call(base, "POST", "/api/runtimes/wearable/calibration", {"action": "squeeze"})
                say(f"  t={t_s:5.2f} s  -> squeeze capture started (squeeze for 3 s)")
            if p["type"] == "stream_event" and p["event"] == "calibration_state":
                d = p["detail"]
                say(f"  t={t_s:5.2f} s  calibration: {d['state']}" + (f" ({d['reason']})" if d.get("reason") else ""))
    return call(base, "POST", f"/api/sessions/{session['session_id']}/stop", {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    base = ap.parse_args().url.rstrip("/")
    health = call(base, "GET", "/api/health")
    say(f"Backend ok, runtimes: {health['runtimes']}")
    row = asyncio.run(drive(base))

    sc = row["score"]
    say("\n=== Recorded session ===")
    say(f"  state            {row['state']} ({row['end_reason']}), source: {row['source']}")
    say(f"  frames           {row['counters']['frames']}")
    say(f"  calibration      {row['calibration_id']}   (used: {row['counters']['calibration_ids']})")
    say(f"  score status     {sc['status']}")
    say(f"  scored time      {sc['session_valid_s']} s  ->  {sc['session_au']} au   (arbitrary units, experimental)")
    say(f"  {sc['explanation']}")
    if sc["excluded_by_reason"]:
        say("  not scored because:")
        for reason, secs in list(sc["excluded_by_reason"].items())[:5]:
            say(f"    {secs:7.3f} s  {reason}")

    say("\n=== Replay of that session ===")
    rp = call(base, "POST", f"/api/sessions/{row['session_id']}/replay", {"fast": True})
    name = rp["runtime"]
    for _ in range(100):
        rr = call(base, "GET", f"/api/runtimes/{name}")
        if rr["state"] == "completed":
            break
        asyncio.run(asyncio.sleep(0.2))
    cum = rr["latest"]["score"]["cumulative"]["value"]
    orig = sc["at_end"]["cumulative_au"] - (sc["at_start"]["cumulative_au"] if sc["at_start"] else 0.0)
    prov = rr["provenance"]
    say(f"  provenance       {prov['source']} of a {prov['original_source']} recording ({prov['recording_id']})")
    say(f"  frames           {rr['counters']['frames']} (original {row['counters']['frames']})")
    cal = rr["calibration"]
    say(f"  calibration      {cal['state']}" + (f" ({cal['reason']})" if cal.get("reason") else ""))
    say(f"  replayed score   {cum} au   original session score {round(orig, 6)} au")
    call(base, "DELETE", f"/api/runtimes/{name}")
    good = (prov["source"] == "replay" and rr["counters"]["frames"] == row["counters"]["frames"]
            and sc["status"] == "ok" and abs(cum - orig) < 1e-4)
    say("\nRESULT: " + ("PASS - calibrated, scored, and the replay reproduced the score."
                        if good else "CHECK - see the numbers above."))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
