"""Regenerate backend/examples/: real API responses and live messages for Ali and Fahad.

Runs the backend on a SIMULATED device (demo scenario, 5x speed) in a temporary
data directory, drives one full wearable session with calibration, a replay, a
station session, and simulated history, and saves what the API actually returned.

    cd backend && uv run python scripts/make_examples.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from vibeguard_backend.adapters import SimAdapter
from vibeguard_backend.app import create_app
from vibeguard_backend.history import seed_history
from vibeguard_backend.service import Backend, processing_config_for

OUT = Path(__file__).resolve().parents[1] / "examples"


def save(name: str, obj) -> None:
    (OUT / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def trim_session(row: dict) -> dict:
    row = dict(row)
    if row.get("files"):
        row["files"] = {k: {**v, "path": f"<data_dir>/sessions/{row['session_id']}/{k}"}
                        for k, v in row["files"].items()}
    if row.get("dir"):
        row["dir"] = f"<data_dir>/sessions/{row['session_id']}"
    return row


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vg-examples-"))
    OUT.mkdir(exist_ok=True)
    backend = Backend(tmp)
    speed = 5.0
    rt = backend.add_runtime("wearable", SimAdapter("demo", seed=1, duration_s=30, speed=speed, loop=False,
                                                    run_id="examples"), processing_config_for("sim"))
    client = TestClient(create_app(backend))
    messages: dict[str, dict] = {}

    def keep(m: dict) -> None:
        ch, p = m["channel"], m["payload"]
        key = ch
        if ch == "derived":
            key = f"derived.{p['type']}" + (f".{p['stream']}" if p["type"] == "stream_features" else
                                            f".{p['event']}" if p["type"] == "stream_event" else "")
        elif ch in ("device", "telemetry"):
            key = f"{ch}.{p['type']}"
        elif isinstance(p, dict) and "event" in p:
            key = f"{ch}.{p['event']}"
        else:
            key = f"{ch}.status"
        messages.setdefault(key, m)
        if key == "derived.stream_features.emg" and p["features"]["emg_activation"]["calibration"]["state"] == "calibrated":
            messages["derived.stream_features.emg.calibrated"] = m
        if key == "derived.experimental_score" and p["status"] == "accumulating":
            messages["derived.experimental_score.accumulating"] = m

    with client.websocket_connect("/ws/live?channels=derived,device,telemetry,runtime,session,station") as ws:
        messages["snapshot"] = ws.receive_json()
        assert rt.connection == "connected" or time.sleep(0.2) is None
        session = client.post("/api/sessions", json={"runtime": "wearable", "label": "example session",
                                                     "request_id": "example-start-1"}).json()
        save("api_post_sessions.json", trim_session(session))
        time.sleep(0.5 / speed)
        save("api_post_calibration_relaxed.json",
             client.post("/api/runtimes/wearable/calibration", json={"action": "relaxed"}).json())
        time.sleep(4.0 / speed)
        save("api_post_calibration_squeeze.json",
             client.post("/api/runtimes/wearable/calibration", json={"action": "squeeze"}).json())
        deadline = time.monotonic() + 30 / speed + 5
        while not rt.finished and time.monotonic() < deadline:
            try:
                while True:
                    keep(ws.receive_json())
                    if rt.finished:
                        break
            except Exception:  # noqa: BLE001
                break
        save("api_get_runtime_snapshot.json", client.get("/api/runtimes/wearable").json())

    sid = session["session_id"]
    save("api_get_session_completed.json", trim_session(client.get(f"/api/sessions/{sid}").json()))
    replay = client.post(f"/api/sessions/{sid}/replay", json={"speed": 10.0}).json()
    save("api_post_replay.json", replay)

    st = client.post("/api/station/sessions", json={"source": "live", "label": "example perception test",
                                                    "protocol": {"kind": "preview"}}).json()
    stid = st["station_session_id"]
    save("api_post_station_session.json", st)
    save("api_post_station_trial_felt.json", client.post(f"/api/station/sessions/{stid}/trials", json={
        "stimulus": {"motor_setting": 160, "units": "pwm_duty_0_255"}, "response": "felt",
        "presented_at": "2026-11-07T18:00:00.000Z", "responded_at": "2026-11-07T18:00:00.640Z",
        "response_latency_ms": 640}).json())
    save("api_post_station_trial_missing.json", client.post(f"/api/station/sessions/{stid}/trials", json={
        "stimulus": {"motor_setting": 120, "units": "pwm_duty_0_255"}, "response": "missing",
        "presented_at": "2026-11-07T18:00:06.000Z", "note": "no response within 3 s"}).json())
    save("api_post_station_abort.json", client.post(f"/api/station/sessions/{stid}/abort",
                                                    json={"reason": "operator_abort"}).json())
    seed_history(backend, days=3)
    hist = client.get("/api/history?limit=4").json()
    hist["items"] = [trim_session(i) if i["kind"] == "wearable" else i for i in hist["items"]]
    save("api_get_history.json", hist)
    save("api_get_health.json", {**client.get("/api/health").json(), "data_dir": "<data_dir>"})
    err = client.post("/api/sessions", json={"runtime": "nope"})
    save("api_error_404.json", {"status": err.status_code, "body": err.json()})

    for key, m in sorted(messages.items()):
        p = m["payload"]
        if key.startswith("telemetry.") and isinstance(p, dict) and len(p.get("samples", [])) > 3:
            m = {**m, "payload": {**p, "samples": p["samples"][:3] + [["... trimmed for the example"]]}}
        if key == "snapshot":
            continue
        save(f"ws_{key.replace('.', '_')}.json", m)
    save("ws_snapshot.json", messages["snapshot"])
    backend.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"wrote {len(list(OUT.glob('*.json')))} examples to {OUT}")


if __name__ == "__main__":
    main()
