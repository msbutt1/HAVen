"""Demo-day preflight: is the local system ready, and what exactly am I about to show?

Run it against the running backend before going on stage:

    uv run vg-backend preflight                      # any data source, reports which one
    uv run vg-backend preflight --expect live        # fails if the data is not live
    uv run vg-backend preflight --backup backup/demo-session

Every line is PASS, WARN or FAIL. FAIL means do not start the demo that way (switch to the
labelled replay fallback); WARN means know about it. Exit code 0 unless something FAILed.
All checks are local: no network beyond 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    level: str
    name: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, level: str, name: str, detail: str = "", fix: str = "") -> None:
        self.checks.append(Check(level, name, detail, fix))

    @property
    def failed(self) -> bool:
        return any(c.level == FAIL for c in self.checks)

    def render(self, not_ready: str | None = None) -> str:
        lines = []
        for c in self.checks:
            lines.append(f"  {c.level:<4}  {c.name}" + (f": {c.detail}" if c.detail else ""))
            if c.fix and c.level != PASS:
                lines.append(f"        -> {c.fix}")
        n = {lvl: sum(1 for c in self.checks if c.level == lvl) for lvl in (PASS, WARN, FAIL)}
        verdict = ((not_ready or "NOT READY - fix the FAIL lines or switch to the labelled replay fallback") if n[FAIL]
                   else "READY" + (f" (with {n[WARN]} warning(s))" if n[WARN] else ""))
        lines.append(f"\n  {n[PASS]} pass, {n[WARN]} warn, {n[FAIL]} fail  =>  {verdict}")
        return "\n".join(lines)


# The checklist talks to the local backend only: never through a proxy (HTTP_PROXY on a conference
# network would otherwise make a healthy localhost server look unreachable).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(base: str, path: str, timeout: float = 3.0):
    with _OPENER.open(base + path, timeout=timeout) as r:
        return json.loads(r.read())


def _is_local(base: str) -> bool:
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_preflight(base: str = "http://127.0.0.1:8000", runtime: str = "wearable", expect: str = "any",
                  backup: str | Path | None = None, min_free_gb: float = 1.0) -> Report:
    rep = Report()
    base = base.rstrip("/")

    # 1. the server answers
    try:
        health = _get(base, "/api/health")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        rep.add(FAIL, "backend reachable", f"{base} did not answer ({exc})",
                "start it: cd backend && uv run vg-backend serve")
        return rep
    local = _is_local(base)
    rep.add(PASS, "backend reachable", f"version {health.get('version')}")
    rec = health.get("recovered_on_start") or {}
    if any(rec.values()):
        rep.add(WARN, "previous run was cut off", f"{rec}: those sessions are marked 'interrupted'",
                "expected after a crash or power loss; their files are kept")

    # 2. the device source
    rt_path = "/api/runtimes/" + urllib.parse.quote(runtime, safe="")
    try:
        rt = _get(base, rt_path)
    except urllib.error.HTTPError:
        rep.add(FAIL, f"runtime '{runtime}' exists", "no such device source",
                "start with a device: vg-backend serve --device serial --port ... (or --device sim)")
        rt = None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        rep.add(FAIL, f"runtime '{runtime}' readable", str(exc))
        rt = None

    finished = rt is not None and rt["state"] == "completed"
    if rt is not None:
        state = rt["state"]
        is_replay = rt.get("role") == "replay"
        if state in ("ready", "recording", "calibrating"):
            rep.add(PASS, "device connected", f"state {state}")
        elif finished:
            rep.add(FAIL, "device connected", "state completed: the recording has finished playing, nothing is updating",
                    "play it again: POST /api/runtimes/" + urllib.parse.quote(runtime, safe="")
                    + "/restart, or restart with: serve --backup DIR --loop")
        else:
            adapter_kind = (rt.get("adapter") or {}).get("kind")
            fix = ("check the USB cable and port; the backend reconnects by itself once the device is back"
                   if adapter_kind == "serial" else "restart the backend")
            rep.add(FAIL, "device connected", f"state {state}: {rt.get('connection_detail') or rt.get('error') or 'no reason given'}", fix)

        # fresh readings: frames must actually advance
        f0 = rt["counters"].get("frames", 0)
        time.sleep(1.0)
        try:
            rt2 = _get(base, rt_path)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            rt2 = rt
        advanced = rt2["counters"].get("frames", 0) - f0
        age = rt2.get("last_rx_age_s")
        if finished:
            pass  # already reported above; a second FAIL for the same cause would only be noise
        elif advanced > 0 and age is not None and age < 1.0 and rt2.get("data_fresh", True):
            rep.add(PASS, "fresh live readings", f"{advanced} frames in 1 s, last data {age:.2f} s ago")
        else:
            rep.add(FAIL, "fresh live readings", f"{advanced} frames in 1 s, last data age {age}, "
                    f"data_fresh={rt2.get('data_fresh')}", "data is not flowing; do not demo from this state")
        # the processor measures the real sample rate from the device timestamps
        for sname in ("emg", "imu"):
            feat = (rt2.get("latest") or {}).get(sname) or {}
            win = feat.get("window") or {}
            nominal, measured = win.get("nominal_rate_hz"), win.get("measured_rate_hz")
            if nominal and measured:
                from .hardware import RATE_PASS, RATE_WARN  # the same thresholds serial-check uses
                dev = abs(measured - nominal) / nominal
                if dev <= RATE_PASS:
                    rep.add(PASS, f"{sname} sample rate", f"measured {measured:.1f} Hz vs nominal {nominal:g} Hz")
                else:
                    rep.add(WARN if dev <= RATE_WARN else FAIL, f"{sname} sample rate",
                            f"measured {measured:.1f} Hz vs nominal {nominal:g} Hz ({dev * 100:.1f}% off)",
                            "check the timestamps and the configured sensor rate (uv run vg-backend serial-check)")
        hello = rt2.get("hello")
        if hello:
            unresolved = [f"{k}.{f}" for k, v in (hello.get("streams") or {}).items()
                          for f in ("nominal_rate_hz", "raw_limits", "sensor_model") if v.get(f) is None]
            if unresolved:
                rep.add(WARN, "hardware facts known", f"unresolved in the device's hello: {', '.join(unresolved)}",
                        "clipping cannot be detected without raw_limits; fill them in the firmware")
        bad = rt2["counters"].get("invalid_frames", 0)
        if bad:
            rep.add(WARN, "invalid frames seen", f"{bad} rejected so far; last: {rt2.get('last_invalid_frame')}",
                    "check the firmware output and baud rate")
        if rt2["events"].get("seq_gap", 0):
            rep.add(WARN, "records lost", f"{rt2['events']['seq_gap']} record(s) missing so far (seq gaps)",
                    "link or buffer problem; check the cable and baud rate")

        # what are we showing?
        prov = rt2.get("provenance") or {}
        src = prov.get("source", "unknown")
        label = {"live": "LIVE device data", "simulated": "SIMULATED data (not a measurement)",
                 "replay": f"REPLAY of a recording (original: {prov.get('original_source')})"}.get(src, src)
        if expect != "any" and src != expect:
            rep.add(FAIL, f"data source is {expect}", f"it is {label}",
                    "connect the real device, or say out loud that this is a replay/simulation")
        else:
            rep.add(PASS if src == "live" else WARN, "data source", label,
                    "" if src == "live" else "the dashboard will show this label; say it in the pitch")
        cal = rt2["calibration"]
        if cal["state"] == "calibrated":
            rep.add(PASS, "wearer calibrated", cal.get("calibration_id") or "")
        else:
            rep.add(WARN, "wearer not calibrated", f"state {cal['state']}" + (f" ({cal['reason']})" if cal.get("reason") else ""),
                    "demo step 2: capture relaxed, then squeeze")

    # 3. live delivery works (the dashboard's connection)
    try:
        from websockets.sync.client import connect

        ws_url = base.replace("http", "ws", 1) + "/ws/live?runtime=" + urllib.parse.quote(runtime, safe="") \
            + "&channels=derived,runtime"
        with connect(ws_url, open_timeout=3, max_size=None, proxy=None) as ws:
            first = json.loads(ws.recv(timeout=3))
            got = 0
            end = time.monotonic() + 2.0
            while time.monotonic() < end and got < 5:
                try:
                    ws.recv(timeout=0.5)
                    got += 1
                except TimeoutError:
                    pass
        if first.get("channel") == "snapshot" and got >= 5:
            rep.add(PASS, "live delivery (WebSocket)", f"snapshot + {got} messages in <2 s")
        elif first.get("channel") == "snapshot" and (finished or rt is None or not rt.get("data_fresh", True)):
            rep.add(WARN, "live delivery (WebSocket)", "connects and sends a snapshot; no messages because no data "
                    "is flowing (see above)")
        else:
            rep.add(FAIL, "live delivery (WebSocket)", f"first={first.get('channel')}, then {got} messages",
                    "the dashboard would show nothing; check /ws/live and that uvicorn[standard] is installed")
    except Exception as exc:  # noqa: BLE001 - any failure here means the dashboard gets nothing
        rep.add(FAIL, "live delivery (WebSocket)", f"{type(exc).__name__}: {exc}",
                "cd backend && uv sync   (needs uvicorn[standard])")

    # 4. saved sessions are there and intact
    try:
        sessions = _get(base, "/api/sessions?limit=1000")
        done = [s for s in sessions if s["state"] == "completed"]
        if not done:
            rep.add(WARN, "saved sessions", "no completed session yet", "record one before the demo (step 4)")
        elif not local:
            rep.add(WARN, "saved sessions intact", f"{len(done)} completed; file hashes not checked because the "
                    "server is on another machine (its paths are not readable here)")
        else:
            last = done[0]
            bad, checked = [], 0
            for name, info in (last.get("files") or {}).items():
                p = Path(info["path"])
                checked += 1
                if not p.is_file() or p.stat().st_size != info["bytes"] or _sha256(p) != info["sha256"]:
                    bad.append(name)
            if bad:
                rep.add(FAIL, "saved sessions intact", f"{last['session_id']}: damaged or missing {bad}")
            elif not checked:
                rep.add(WARN, "saved sessions intact", f"latest {last['session_id']} has no recorded file hashes, "
                        "so nothing could be verified")
            else:
                rep.add(PASS, "saved sessions intact",
                        f"{len(done)} completed; latest {last['session_id']} ({last['source']}): "
                        f"{checked} files verified by SHA-256")
        interrupted = [s for s in sessions if s["state"] == "interrupted"]
        if interrupted:
            rep.add(WARN, "interrupted sessions", f"{len(interrupted)} (from crashes/restarts)", "they replay fine; ignore for the demo")
        hist = _get(base, "/api/history?limit=1000")
        sim_hist = [i for i in hist["items"] if i.get("simulated_history")]
        mislabelled = [i.get("session_id") or i.get("station_session_id") for i in sim_hist if i.get("source") != "simulated"]
        if mislabelled:
            rep.add(FAIL, "simulated history labelled", f"{len(mislabelled)} history rows are flagged simulated "
                    f"but their source says otherwise: {mislabelled[:3]}")
        elif sim_hist:
            rep.add(PASS, "simulated history labelled", f"{len(sim_hist)} rows, each flagged simulated with source simulated")
        else:
            rep.add(WARN, "no simulated history", "the longitudinal view will be empty",
                    "uv run vg-backend seed-history")
    except (urllib.error.URLError, OSError, ValueError, KeyError, http.client.HTTPException) as exc:
        rep.add(FAIL, "saved sessions readable", str(exc))

    # 5. the fallback exists
    if backup is not None:
        try:
            from .backup import BackupError, verify_backup

            meta = verify_backup(backup)
            rep.add(PASS, "replay fallback ready", f"{backup}: {meta['frames']} frames, {meta['label_for_dashboard']}, hashes verified")
        except BackupError as exc:
            rep.add(FAIL, "replay fallback ready", str(exc), "re-export it: vg-backend export-backup SESSION_ID DIR")
        except Exception as exc:  # noqa: BLE001
            rep.add(FAIL, "replay fallback ready", f"{type(exc).__name__}: {exc}")
    else:
        rep.add(WARN, "replay fallback", "no --backup given, so it was not checked",
                "pass --backup backup/demo-session")

    # 6. the machine
    data_dir = health.get("data_dir")
    if not local:
        rep.add(WARN, "machine checks", "data folder and disk space not checked: the server is on another machine")
    elif data_dir and Path(data_dir).is_dir():
        free_gb = shutil.disk_usage(data_dir).free / 1e9
        writable = os.access(data_dir, os.W_OK)
        rep.add(PASS if writable else FAIL, "data folder writable", data_dir)
        rep.add(PASS if free_gb >= min_free_gb else WARN, "disk space", f"{free_gb:.1f} GB free",
                f"free up space (recordings are ~0.2 MB per 30 s)")
    return rep
