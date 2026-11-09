"""Run the backend acceptance scenarios and write backend/EVIDENCE.md with what was OBSERVED.

Format from project-plan/abduljabbar/abduljabbar_05_testing_and_acceptance.md: build/version,
scenario, expected result, observed result, owner, unresolved limitation.

    cd backend && uv run python scripts/run_acceptance.py          # writes EVIDENCE.md, exit 1 on failure

Everything runs in-process on SIMULATED data in a temporary directory, offline. A real-hardware
run needs its own rows (see the 'Not covered' section the script writes).
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests"))

from conftest import ScriptedAdapter, sim_frames, wait_until  # noqa: E402

from haven_backend import __version__  # noqa: E402
from haven_backend.adapters import SimAdapter  # noqa: E402
from haven_backend.backup import export_backup, start_backup_replay  # noqa: E402
from haven_backend.history import seed_history  # noqa: E402
from haven_backend.hub import Hub  # noqa: E402
from haven_backend.service import Backend, processing_config_for  # noqa: E402
from haven_backend.station import MockStationController, StationEngine  # noqa: E402

CFG = processing_config_for("sim")
ROWS: list[dict] = []


def scenario(name: str, expected: str, limitation: str):
    def deco(fn):
        t0 = time.monotonic()
        try:
            observed, ok = fn()
        except Exception as exc:  # noqa: BLE001 - a crash is a failed scenario, reported as such
            observed, ok = f"scenario raised {type(exc).__name__}: {exc}", False
        ROWS.append({"scenario": name, "expected": expected, "observed": observed, "ok": ok,
                     "limitation": limitation, "seconds": round(time.monotonic() - t0, 1)})
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {observed}", flush=True)
        return fn
    return deco


def idx(frames, seconds):
    for i, f in enumerate(frames):
        t = json.loads(f).get("timing")
        if t and (t["t0_us"] if t["mode"] == "uniform" else t["t_us"][0]) / 1e6 >= seconds:
            return i
    return len(frames)


def fresh() -> tuple[Backend, Path]:
    d = Path(tempfile.mkdtemp(prefix="vg-acc-"))
    return Backend(d / "data"), d


def calibrated_session(b: Backend, label: str, duration_s: float = 30, tail_s: float | None = None):
    """Run one simulated run with a session and the demo calibration schedule; returns (session_row, runtime)."""
    frames = sim_frames(duration_s=duration_s)
    if tail_s:
        frames = frames[:idx(frames, tail_s)]
    i0, ir, isq = idx(frames, 0.2), idx(frames, 0.5), idx(frames, 4.5)
    g = [threading.Event() for _ in range(3)]
    script = [*frames[:i0], ("gate", g[0]), *frames[i0:ir], ("gate", g[1]), *frames[ir:isq], ("gate", g[2]), *frames[isq:]]
    rt = b.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True), CFG)
    assert wait_until(lambda: rt.counters["records"] == i0)
    s = b.start_session("wearable", label)
    g[0].set(); assert wait_until(lambda: rt.counters["records"] == ir)
    b.calibrate("wearable", "relaxed")
    g[1].set(); assert wait_until(lambda: rt.counters["records"] == isq)
    b.calibrate("wearable", "squeeze")
    g[2].set(); assert wait_until(lambda: rt.counters["records"] == len(frames), 30)
    return b.stop_session(s["session_id"]), rt


def derived_of(row: dict) -> list[dict]:
    return [json.loads(x) for x in (Path(row["dir"]) / "derived.ndjson").read_text().splitlines()]


# ---------------------------------------------------------------------------------------------

@scenario("Malformed messages do not crash ingestion",
          "runtime stays alive, every bad frame is counted and reported, valid frames after them are processed",
          "Only contract v1 NDJSON was exercised; a future binary wire format needs its own decoder tests.")
def _():
    b, d = fresh()
    frames = sim_frames(duration_s=5)
    bad = [b"{}\n", b"not json\n", b"\xff\xfe\n", b"\n", b"[" * 30000 + b"]" * 30000 + b"\n",
           b'{"schema":"vibeguard.device_telemetry","version":2,"type":"hello"}\n', b"x" * 70000 + b"\n",
           b'{"a":1,"a":2}\n']
    script = [*frames[:3]]
    for i, junk in enumerate(bad):
        script += [junk, frames[3 + i]]
    script += frames[3 + len(bad):]
    rt = b.add_runtime("wearable", ScriptedAdapter(script, clock="device"), CFG)
    ok = wait_until(lambda: rt.finished, 20)
    st = rt.status()
    feats = rt.latest["emg"] is not None
    res = (f"{len(bad)} bad frames in -> invalid_frames={st['counters']['invalid_frames']}, "
           f"records processed={st['counters']['records']}/{len(frames)}, runtime error={rt.error}, "
           f"features still produced={feats}")
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return res, ok and rt.error is None and st["counters"]["invalid_frames"] == len(bad) and \
        st["counters"]["records"] == len(frames) and feats


@scenario("Sequence gaps are surfaced",
          "every record the simulated transport lost appears as a seq_gap count",
          "Gap detection relies on the device's seq counter; firmware must keep it per boot.")
def _():
    from haven_signals.simulator import SimConfig, Simulation
    sim = Simulation(SimConfig(scenario="missing_batches", duration_s=10, seed=1), run_id="acc")
    list(sim.records())
    lost = sim.summary.records_lost
    b, d = fresh()
    rt = b.add_runtime("wearable", ScriptedAdapter(sim_frames("missing_batches", 10, 1, "acc"), clock="device"), CFG)
    wait_until(lambda: rt.finished, 20)
    got = rt.events["seq_gap"]
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return f"simulator dropped {lost} records; backend reports seq_gap total {got}", got == lost and lost > 0


@scenario("A device disconnect produces visible status, not silently stale data",
          "state becomes 'disconnected' fast, streams go stale, no old value is shown as current, it recovers on reconnect",
          "Simulated unplug only; USB re-enumeration timing on real hardware is unmeasured.")
def _():
    b, d = fresh()
    frames = sim_frames(duration_s=10)
    ad = ScriptedAdapter([*frames[:200], "disconnect"], clock="host", auto_reconnect=False)
    rt = b.add_runtime("wearable", ad, CFG)
    wait_until(lambda: rt.counters["records"] == 200, 10)
    t0 = time.monotonic()
    wait_until(lambda: rt.host_state == "disconnected", 5)
    t_disc = time.monotonic() - t0
    wait_until(lambda: rt.events["stream_stale"] >= 2, 5)
    snap = rt.snapshot()
    unavailable = all(snap["latest"][k] is None or snap["latest"][k]["status"] == "unavailable" for k in ("emg", "imu"))
    ad.items.extend(frames[200:300]); ad.reconnect()
    wait_until(lambda: rt.host_state == "ready", 8)
    ok = rt.host_state == "ready" and unavailable
    res = (f"disconnected state visible within {max(t_disc, 0.05):.2f} s (polling resolution), stale events={rt.events['stream_stale']}, "
           f"latest values unavailable while down={unavailable}, state after reconnect={rt.host_state}")
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return res, ok and t_disc < 2.0


@scenario("Repeated commands do not duplicate completed sessions",
          "8 concurrent starts with one request_id -> 1 session; stopping twice -> same completed session",
          "Idempotency keys are kept forever in SQLite; a client must not reuse an id for a different command.")
def _():
    b, d = fresh()
    rt = b.add_runtime("wearable", ScriptedAdapter(sim_frames(duration_s=5), clock="device", hold_open=True), CFG)
    wait_until(lambda: rt.counters["records"] > 5)
    out = []
    ts = [threading.Thread(target=lambda: out.append(b.start_session("wearable", "dup", request_id="same"))) for _ in range(8)]
    [t.start() for t in ts]; [t.join(10) for t in ts]
    ids = {o["session_id"] for o in out}
    s1 = b.stop_session(ids.copy().pop(), request_id="stop-1"); s2 = b.stop_session(s1["session_id"])
    rows = b.store.wearable_sessions()
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return (f"8 starts -> {len(ids)} distinct session, rows in store={len(rows)}, stop twice -> "
            f"{s1['state']}/{s2['state']} with the same ended_at={s1['ended_at'] == s2['ended_at']}"), \
        len(ids) == 1 and len(rows) == 1 and s1["ended_at"] == s2["ended_at"]


@scenario("Live, replay and simulated records preserve their provenance",
          "simulated stays simulated; a replay says replay + original source + recording id; a live stream recorded then replayed says original_source live",
          "No real live device has been connected; 'live' was produced by relabelling simulated frames.")
def _():
    from haven_signals import contract
    b, d = fresh()
    row, rt = calibrated_session(b, "prov", tail_s=12)
    sim_ok = row["source"] == "simulated" and all(o["provenance"]["source"] == "simulated" for o in derived_of(row))
    rp = b.runtime(b.start_replay(row["session_id"], fast=True)["runtime"])
    wait_until(lambda: rp.finished, 20)
    rp_ok = rp.provenance == {"source": "replay", "original_source": "simulated", "recording_id": row["recording_id"]}
    b2, d2 = fresh()
    live = []
    for f in sim_frames(duration_s=6):
        r = json.loads(f); r["provenance"] = {"source": "live"}; contract.require_valid(r)
        live.append(contract.serialize_record(r).encode())
    rt2 = b2.add_runtime("wearable", ScriptedAdapter(live, clock="device", hold_open=True), CFG)
    wait_until(lambda: rt2.counters["records"] > 5)
    s2 = b2.start_session("wearable", "live-ish")
    wait_until(lambda: rt2.counters["records"] == len(live), 10)
    s2 = b2.stop_session(s2["session_id"])
    rp2 = b2.runtime(b2.start_replay(s2["session_id"], fast=True)["runtime"])
    wait_until(lambda: rp2.finished, 20)
    live_ok = s2["source"] == "live" and rp2.provenance["original_source"] == "live" and \
        rp2.provenance["source"] == "replay" and s2["recording_id"].startswith("rec-")
    b.shutdown(); b2.shutdown(); shutil.rmtree(d, ignore_errors=True); shutil.rmtree(d2, ignore_errors=True)
    return (f"simulated session labelled simulated={sim_ok}; its replay={rp.provenance}; "
            f"live-labelled stream recorded as live={s2['source']}, replayed as {rp2.provenance['source']} "
            f"(original {rp2.provenance['original_source']})"), sim_ok and rp_ok and live_ok


@scenario("Sessions survive restart and everything works offline",
          "after a kill-style restart the session, calibration and files are intact; no non-loopback network connection is made",
          "A crash can lose up to ~1 s of the newest telemetry (flush interval).")
def _():
    remote = []
    real_connect = socket.socket.connect

    def spy(self, addr, *a, **k):
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if isinstance(addr, tuple) and host not in ("127.0.0.1", "::1", "localhost"):
            remote.append(host)
        return real_connect(self, addr, *a, **k)
    socket.socket.connect = spy  # type: ignore[assignment]
    try:
        b, d = fresh()
        row, rt = calibrated_session(b, "restart", tail_s=12)
        cal = rt.status()["calibration"]["calibration_id"]
        b.shutdown()
        b2 = Backend(d / "data")
        back = b2.session(row["session_id"])
        files_ok = all((Path(i["path"]).stat().st_size == i["bytes"]) for i in back["files"].values())
        cals = [c["calibration_id"] for c in b2.store.calibrations()]
        b2.shutdown(); shutil.rmtree(d, ignore_errors=True)
    finally:
        socket.socket.connect = real_connect  # type: ignore[assignment]
    return (f"after restart: session {back['state']}, files intact={files_ok}, calibration stored={cal in cals}, "
            f"non-loopback connections={len(remote)}"), back["state"] == "completed" and files_ok and cal in cals and not remote


@scenario("Wearer calibration and live muscle-activation changes work",
          "after relaxed+squeeze, normalised activation is ~0 when relaxed and ~1 under strong activation",
          "Activation is a relative proxy, not force or %MVC; not tested on a person.")
def _():
    b, d = fresh()
    row, rt = calibrated_session(b, "activation")
    emg = [o for o in derived_of(row) if o["type"] == "stream_features" and o["stream"] == "emg"]
    def med(lo, hi):
        v = sorted(o["features"]["emg_activation"]["normalized"]["value"]["emg0"] for o in emg
                   if lo <= o["t_us"] / 1e6 < hi and o["features"]["emg_activation"]["normalized"]["status"] == "ok")
        return v[len(v) // 2] if v else None
    relaxed, strong = med(8.5, 10.5), med(15.5, 18.5)
    res = f"state {rt.calibration_state}; median activation relaxed phase={relaxed:.2f}, strong phase={strong:.2f}"
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return res, rt.calibration_state == "calibrated" and abs(relaxed) < 0.15 and 0.85 < strong < 1.3


@scenario("Vibration changes are visible (within sensor capability)",
          "measured vibration is near the noise floor with no vibration and several m/s^2 with the 0.5 g synthetic source",
          "Unweighted (not ISO 5349). Simulator scale only; real accelerometer sensitivity and range unconfirmed.")
def _():
    b, d = fresh()
    row, rt = calibrated_session(b, "vibration")
    imu = [o for o in derived_of(row) if o["type"] == "stream_features" and o["stream"] == "imu"]
    def med(lo, hi):
        v = sorted(o["features"]["vibration"]["physical"]["combined_rms"] for o in imu
                   if lo <= o["t_us"] / 1e6 < hi and o["features"]["vibration"]["physical"]["status"] == "ok")
        return v[len(v) // 2] if v else None
    off, on = med(9, 11), med(12, 15)
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return f"vibration off={off:.3f} m/s^2, on={on:.3f} m/s^2", off < 0.1 and on > 2.0


@scenario("A wearable session and a station result are saved and listed with their provenance",
          "both appear in history, newest first, labelled; simulated history is marked simulated",
          "Station results here come from the mock controller and simulated responses.")
def _():
    b, d = fresh()
    row, _rt = calibrated_session(b, "wearable part", tail_s=8)
    eng = StationEngine(b, MockStationController())
    st = b.start_station_session("live", "station part", wearable_session_id=None)
    sid = st["station_session_id"]
    eng.present(sid, {"motor_setting": 120, "units": "pwm_duty_0_255"}, response_timeout_s=5)
    eng.respond(sid, "felt")
    eng.complete(sid)
    seed_history(b, days=2, duration_s=10)
    hist = b.history()
    h = hist["items"]
    kinds = {(i["kind"], i["source"], bool(i.get("simulated_history"))) for i in h}
    has_live_station = ("station", "live", False) in kinds
    has_sim_hist = ("wearable", "simulated", True) in kinds and ("station", "simulated", True) in kinds
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return (f"history rows={len(h)}; kinds={sorted(kinds)}; provenance labels present={'labels' in hist}"), \
        has_live_station and has_sim_hist and "labels" in hist


@scenario("Station abort and missing responses behave clearly",
          "timeout -> 'missing' (never 'not_felt'); abort stops the motor, records the open trial as missing and ends the session 'aborted'",
          "Mock controller: the real stimulus command contract with Fahad/Salaar is still PROPOSED.")
def _():
    b, d = fresh()
    ctl = MockStationController(); eng = StationEngine(b, ctl)
    sid = b.start_station_session("live", "abort test")["station_session_id"]
    eng.present(sid, {"motor_setting": 100, "units": "pwm_duty_0_255"}, response_timeout_s=0.2)
    wait_until(lambda: len(b.store.station_trials(sid)) == 1, 3)
    t1 = b.store.station_trials(sid)[0]
    eng.present(sid, {"motor_setting": 140, "units": "pwm_duty_0_255"}, response_timeout_s=10)
    out = eng.abort(sid, "operator abort")
    t = b.store.station_trials(sid)
    eng.shutdown(); b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return (f"timeout trial response={t1['response']}; after abort: session {out['state']}, motor_on={ctl.motor_on}, "
            f"open trial recorded as {t[1]['response']} ({t[1]['note']})"), \
        t1["response"] == "missing" and out["state"] == "aborted" and not ctl.motor_on and t[1]["response"] == "missing"


@scenario("Restart works locally and the recorded backup is usable",
          "a verified backup exported from a session replays (labelled replay) on a fresh backend with the same score, without the original files",
          "The committed backup is SIMULATED; a real-hardware backup must be recorded and exported on demo day.")
def _():
    b, d = fresh()
    row, rt = calibrated_session(b, "backup source")
    export_backup(b, row["session_id"], d / "backup")
    b.shutdown()
    shutil.rmtree(d / "data")
    b2 = Backend(d / "data2")
    r = start_backup_replay(b2, d / "backup", fast=True)
    wait_until(lambda: r.finished, 20)
    cum = r.latest["score"]["cumulative"]["value"]; orig = row["score"]["at_end"]["cumulative_au"]
    b2.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return (f"replay of backup: provenance={r.provenance['source']} (original {r.provenance['original_source']}), "
            f"score {cum} vs original {orig}"), r.provenance["source"] == "replay" and abs(cum - orig) < 1e-4


@scenario("Acquisition is independent of UI refreshes",
          "three WebSocket-style subscribers that never read do not slow or lose ingestion",
          "Measured with in-process subscribers, not with real browsers.")
def _():
    import asyncio
    frames = sim_frames(duration_s=30)

    def run(n_slow: int) -> tuple[float, int]:
        b, d = fresh()
        async def main():
            subs = [b.hub.subscribe(frozenset({"derived", "runtime", "device", "telemetry", "session"})) for _ in range(n_slow)]
            t0 = time.monotonic()
            rt = b.add_runtime("wearable", ScriptedAdapter(frames, clock="device"), CFG)
            while not rt.finished and time.monotonic() - t0 < 60:
                await asyncio.sleep(0.02)
            return time.monotonic() - t0, rt.counters["records"], sum(s.dropped_total for s in subs)
        el, n, dropped = asyncio.run(main())
        b.shutdown(); shutil.rmtree(d, ignore_errors=True)
        return el, n

    t0, n0 = run(0); t3, n3 = run(3)
    return (f"{len(frames)} frames: no clients {t0:.2f} s ({n0} records), 3 non-reading clients {t3:.2f} s ({n3} records)"), \
        n0 == n3 == len(frames) and t3 < max(3 * t0, t0 + 2)


@scenario("A connected-but-silent device is not shown as current",
          "link up, no data for 1.5 s -> data_fresh false, score masked, device_silent published; fresh again when data resumes",
          "Timing is host-clock based (0.5 s threshold); the signals processor still reports its last score internally (KNOWN_ISSUES.md).")
def _():
    b, d = fresh()
    frames = sim_frames(duration_s=10)
    rt = b.add_runtime("wearable", ScriptedAdapter([*frames[:300], ("pause", 1.5), *frames[300:400]],
                                                   clock="host", hold_open=True), CFG)
    wait_until(lambda: rt.counters["records"] == 300, 10)
    wait_until(lambda: rt.silent, 3)
    st, snap = rt.status(), rt.snapshot()
    masked = snap["latest"]["score"]["status"] == "unavailable" and snap["latest"]["score"]["reason"] == "device_silent"
    wait_until(lambda: rt.counters["records"] > 300 and not rt.silent, 5)
    res = (f"while silent: connection={st['connection']}, data_fresh={st['data_fresh']}, score masked={masked}; "
           f"after data resumed: data_fresh={rt.status()['data_fresh']}")
    ok = st["connection"] == "connected" and not st["data_fresh"] and masked and rt.status()["data_fresh"]
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return res, ok


@scenario("A different wearable on the same link is noticed, not silently ignored",
          "after 3 valid frames from a new device id the runtime switches, closes the open session as interrupted, and reports the change",
          "Needs recalibration on the new device; identity is the device_id the firmware reports.")
def _():
    import json as _j
    b, d = fresh()
    a = sim_frames(duration_s=5, run_id="a")
    other = []
    for f in sim_frames(duration_s=5, run_id="b"):
        r = _j.loads(f); r["device_id"] = "sim-wearable-02"
        other.append((_j.dumps(r, separators=(",", ":")) + "\n").encode())
    g = threading.Event()
    rt = b.add_runtime("wearable", ScriptedAdapter([*a[:60], ("gate", g), *other[:200]], clock="device", hold_open=True), CFG)
    wait_until(lambda: rt.counters["records"] == 60)
    s = b.start_session("wearable", "device A")
    g.set()
    wait_until(lambda: rt.device_id == "sim-wearable-02", 10)
    row = b.session(s["session_id"])
    res = (f"device_id now {rt.device_id}, device_changes={rt.counters['device_changes']}, "
           f"open session ended as {row['state']} ({row['end_reason']})")
    ok = rt.device_id == "sim-wearable-02" and row["end_reason"] == "device_changed"
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return res, ok


@scenario("The local API refuses what a hostile web page could send",
          "foreign Origin (CSRF) -> 403, spoofed Host (DNS rebinding) -> 403, oversized body -> 413, WebSocket from a foreign origin refused, status page escapes labels",
          "No authentication by design; this protects against pages in the user's browser, not against other local users.")
def _():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from haven_backend.app import create_app
    from haven_backend.status_page import STATUS_PAGE
    b, d = fresh()
    with TestClient(create_app(b), base_url="http://localhost") as c:
        csrf = c.post("/api/station/sessions", json={}, headers={"Origin": "https://evil.example"}).status_code
        rebind = c.get("/api/health", headers={"Host": "evil.example"}).status_code
        big = c.post("/api/station/sessions", content=b"{" + b" " * 1_100_000 + b"}",
                     headers={"content-type": "application/json"}).status_code
        try:
            with c.websocket_connect("ws://localhost/ws/live", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json(); ws_refused = False
        except WebSocketDisconnect:
            ws_refused = True
        own = c.post("/api/station/sessions", json={}, headers={"Origin": "http://localhost:5173"}).status_code
    escaped = "esc(s.label)" in STATUS_PAGE and "'<td>'+(s.label||'')" not in STATUS_PAGE
    b.shutdown(); shutil.rmtree(d, ignore_errors=True)
    return (f"foreign-origin POST={csrf}, spoofed Host={rebind}, 1.1 MB body={big}, foreign WebSocket refused={ws_refused}, "
            f"local-origin POST={own}, status page escapes labels={escaped}"), \
        csrf == 403 and rebind == 403 and big == 413 and ws_refused and own == 201 and escaped


@scenario("The real serial adapter works end to end over a pseudo-terminal",
          "frames written in irregular chunks arrive complete and in order; hello sent on connect is not lost; garbage and a 70 kB line do not stop ingestion; unplug is visible",
          "A pty is not a USB UART: baud limits, driver buffering and re-enumeration timing are unmeasured on hardware.")
def _():
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
                        str(HERE.parent / "tests" / "test_serial_pty.py")], capture_output=True, text=True, timeout=300)
    last = (r.stdout.strip().splitlines() or ["no output"])[-1]
    return f"tests/test_serial_pty.py: {last}", r.returncode == 0


# ---------------------------------------------------------------------------------------------

def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def write_log(path: Path) -> None:
    commit, branch = git("rev-parse", "--short", "HEAD"), git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = " (uncommitted changes)" if git("status", "--porcelain") else ""
    passed = sum(r["ok"] for r in ROWS)
    lines = [
        "# Backend acceptance evidence", "",
        f"- **Build:** haven-backend {__version__}, git `{commit}` on `{branch}`{dirty}",
        f"- **Python:** {sys.version.split()[0]}   **Platform:** {sys.platform}",
        "- **Owner:** abduljabbar (backend)",
        "- **Data:** SIMULATED, in-process, offline. Nothing here was measured on hardware or a person.",
        f"- **Result:** {passed}/{len(ROWS)} scenarios passed", "",
        "Regenerate with `cd backend && uv run python scripts/run_acceptance.py`. "
        "This file records what the run observed; it is not edited by hand.", "",
        "| Result | Scenario | Expected | Observed | Unresolved limitation |", "|---|---|---|---|---|",
    ]
    for r in ROWS:
        esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        lines.append(f"| {'PASS' if r['ok'] else '**FAIL**'} | {esc(r['scenario'])} | {esc(r['expected'])} | "
                     f"{esc(r['observed'])} | {esc(r['limitation'])} |")
    lines += ["", "## Not covered here (needs hardware or other owners)", "",
              "- Real wearable over USB serial: sample-rate stability, baud rate, throughput, re-enumeration on replug "
              "(the serial path is tested against pseudo-terminals only).",
              "- Calibration and activation on a person; EMG during vibration (the hour-12 gate).",
              "- Station: the stimulus command contract with Fahad/Salaar is PROPOSED (`STATION_INTERFACE.md`); "
              "only the mock controller was exercised.",
              "- Dashboard behaviour (Ali): the WebSocket/HTTP contract is exercised, not the screens.", ""]
    path.write_text("\n".join(lines))


if __name__ == "__main__":
    out = HERE.parent / "EVIDENCE.md"
    write_log(out)
    ok = all(r["ok"] for r in ROWS)
    print(f"\nwrote {out}: {sum(r['ok'] for r in ROWS)}/{len(ROWS)} passed")
    sys.exit(0 if ok else 1)
