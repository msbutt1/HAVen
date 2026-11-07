# VibeGuard backend

**Owner:** Abduljabbar (device adapters, serial ingestion, sessions, storage, live
delivery, recording/replay, history, reconnect handling).

Everything here runs locally: one Python process, one SQLite file, and session files on
disk. It needs no cloud services or accounts. It was verified on **simulated data only**;
no real wearable has been connected yet.

```
 SimAdapter ─┐                      one acquisition thread per device
 SerialAdapter ─┼─► DeviceRuntime ─► contract.parse_line ─► Processor (signals/) ─┐
 ReplayAdapter ─┘        │                                                        │
                         ├─► session files (telemetry / derived / commands)       │
                         └─► SQLite (sessions, calibrations, devices)             ▼
                                                     Hub ─► WebSocket /ws/live ─► dashboard
 HTTP API (FastAPI) ─► Backend service ─► runtime.call(...) on the acquisition thread
```

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11. Run from `backend/`.

```sh
cd backend
uv sync
uv run pytest -q                       # all tests, simulated data, offline
uv run vg-backend seed-history         # optional: 14 days of labelled SIMULATED history
uv run vg-backend serve                # http://127.0.0.1:8765, simulated wearable
```

Then try it:

```sh
curl -s localhost:8765/api/runtimes/wearable | head -c 600
curl -s -XPOST localhost:8765/api/sessions -H 'content-type: application/json' -d '{"label":"try"}'
curl -s -XPOST localhost:8765/api/runtimes/wearable/calibration -H 'content-type: application/json' -d '{"action":"relaxed"}'
```

### See a full calibrated session (one command)

With `vg-backend serve` running, this drives a whole simulated session and prints the result:

```sh
uv run python scripts/demo_calibrated_session.py
```

It waits for a fresh 30 s simulated run, starts a session, sends the relaxed capture at
0.5 s and the squeeze capture at 4.5 s of device time, stops the session, replays it, and
checks the replay reproduces the score (about 0.0486 au over about 19 s of scored time).
It exits 0 on success. The simulated run restarts every ~30 s and a calibration cannot
span a restart, which is why the script follows the stream instead of timing by hand.

### Device sources

| Command | Device |
|---|---|
| `vg-backend serve` | Simulated wearable (`demo` scenario, 30 s runs that loop with a new boot each time). Every record is labelled `simulated`. |
| `vg-backend serve --scenario fault_mix --duration 60` | Any `vg-sim` scenario (see `uv run --project ../signals vg-sim --list`). |
| `vg-backend serve --device serial --port '/dev/cu.usbmodem*' --baud 921600` | Real firmware over USB serial. The glob finds the port again after a replug. |
| `… --imu-counts-per-g 4096 --imu-conversion-source "LSM6DS3 datasheet, ±8 g"` | Gives m/s² and the score on real hardware. Without a confirmed conversion, vibration stays in counts and the score stays unavailable (by design). |
| `vg-backend serve --device none` | API only: station records, history, replays. |

Data goes to `backend/data/` (git-ignored), or `--data-dir` / `$VG_DATA_DIR`.

## What the backend guarantees

| Requirement | How |
|---|---|
| Malformed messages do not crash ingestion | Each frame is validated by `contract.parse_line`. Invalid frames are counted, reported as `invalid_record` events, and recorded raw; the next valid frame is processed normally. |
| Gaps and disconnects are surfaced | `seq_gap` / `sample_gap` / `boot_change` events from the processor are counted per runtime and per session. Adapter loss publishes `device_connection: disconnected` and the host state becomes `disconnected`. A poll every 50 ms turns silence into `stream_stale`, so old values are never shown as current. Serial reconnects with backoff. |
| Repeated commands do not duplicate sessions | Every POST accepts `request_id`; a repeat returns the first response. Stopping a stopped session returns it unchanged. Starting a second session on a busy device returns 409. |
| Provenance is preserved | `live` / `simulated` / `replay` is stored on every session, station session and derived record. Replays keep `original_source` and `recording_id`, and the live view labels every message. |
| Sessions survive restart | SQLite in WAL mode. On start, sessions a crash cut off are marked `interrupted` (`end_reason: backend_restart`) with their files kept. The last calibration is reloaded and applied only once the signals processor confirms it is compatible (same device, boot and pipeline). |
| Acquisition is independent of the UI | Acquisition runs on its own thread. WebSocket clients have bounded queues that drop the oldest messages; a lagging client gets `dropped_before: n`. |

## Host states

`GET /api/runtimes/{name}` → `state`:

| State | Meaning |
|---|---|
| `disconnected` | The adapter has no link (unplugged, port missing). It retries. |
| `ready` | Data is flowing, nothing is recording. |
| `calibrating` | A relaxed or squeeze capture is in progress. |
| `recording` | A wearable session is recording. |
| `completed` | A finite source (replay) reached its end. |
| `error` | Unexpected failure. Details are in `error`. |

## HTTP API

Every POST body is a JSON object and may include `"request_id"` for safe retries. A
repeat with the same id **and target** returns the first result; the same id for another
command or target is a 409. Wrong types are a 400. Example
responses captured from a real run are in [`examples/`](examples/).

| Method + path | Body | Result |
|---|---|---|
| `GET /api/health` | | version, runtime states, sessions recovered at start |
| `GET /api/runtimes` | | all device sources with state, counters, events |
| `GET /api/runtimes/{name}` | | status + `latest` emg/imu features and score (for a page that loads mid-stream) |
| `POST /api/runtimes/{name}/calibration` | `{"action":"relaxed"\|"squeeze"\|"cancel"}` | calibration events + state |
| `POST /api/runtimes/{name}/simulate-disconnect` | `{"seconds":3}` | simulated devices only: fake a cable pull |
| `DELETE /api/runtimes/{name}` | | stop a replay |
| `POST /api/sessions` | `{"runtime":"wearable","label":"..."}` | 201, session `recording` (409 if busy or disconnected) |
| `POST /api/sessions/{id}/stop` | | session `completed` with score summary, counters, file hashes. `score.status` is `ok` only if some time was scored; otherwise `no_valid_time`, with `excluded_by_reason` and a plain-language `explanation` (0 au then means "not measured", not "low"). `unavailable` means the score cannot be attributed to this session (e.g. mixed data sources). |
| `GET /api/sessions?source=&state=` | | wearable sessions, newest first |
| `GET /api/sessions/{id}` | | one session |
| `GET /api/sessions/{id}/files/{name}` | | `telemetry.ndjson`, `derived.ndjson`, `rx_times.csv`, `commands.ndjson`, `manifest.json` |
| `POST /api/sessions/{id}/replay` | `{"speed":1.0}` or `{"fast":true}` | 201, a new runtime `replay-…` streaming with `provenance.source = "replay"` |
| `POST /api/station/sessions` | `{"source":"live","label":"...","protocol":{...},"wearable_session_id":"..."}` | 201, station session `testing` |
| `POST /api/station/sessions/{id}/trials` | `{"stimulus":{"motor_setting":160,"units":"pwm_duty_0_255"},"response":"felt"\|"not_felt"\|"missing","is_catch":false,"presented_at":"...","responded_at":"...","response_latency_ms":640,"measured_amplitude":null}` | 201, trial |
| `POST /api/station/sessions/{id}/complete` / `abort` | `{"reason":"..."}` | final state + counts summary |
| `GET /api/station/sessions`, `/api/station/sessions/{id}` | | with trials |
| `GET /api/history?source=` | | wearable + station sessions, newest first, with provenance labels |
| `GET /api/devices`, `GET /api/calibrations?device_id=` | | |

A station `stimulus` is the **motor setting**, not a measured amplitude. Send
`measured_amplitude` (`{value, units, source}`) only when one was actually measured. A
timeout is `response: "missing"`, never `not_felt`.

## Live delivery: `ws://127.0.0.1:8765/ws/live`

Query parameters: `channels=derived,runtime,session,station,device,telemetry` (default:
everything except the high-volume `telemetry`) and `runtime=wearable` (default: all).

The first message is `{"channel":"snapshot","hub_seq":N,"rx_wall":"...","payload":{"runtimes":[...]}}`;
messages with `hub_seq` ≤ N are already reflected in it. Every message after that is:

```json
{"v":1,"hub_seq":812,"channel":"derived","runtime":"wearable",
 "rx_wall":"2026-11-07T18:02:11.532Z","payload":{ ... }}
```

| Channel | Payload |
|---|---|
| `derived` | signals processor output: `stream_features` (emg/imu), `stream_event`, `experimental_score` (see `../signals/HANDOFF.md` §5) |
| `runtime` | runtime status on every state change; `{"event":"device_connection","connection":"disconnected",...}` |
| `session` | `session_started` / `session_completed` / `session_interrupted` with the session row |
| `station` | `station_session_started`, `station_trial_recorded`, `station_session_completed` / `_aborted` |
| `device` | raw `hello` and `status` records |
| `telemetry` | raw `emg_batch` / `imu_batch` records (opt-in) |

`dropped_before` appears on the message right after a gap, when that client fell behind and
lost older messages; summed over a connection it equals the number lost. After a gap, re-read
`GET /api/runtimes/{name}` for state.

## Recording and replay

A session directory (`data/sessions/<id>/`) holds:

- `telemetry.ndjson`: every frame exactly as received. `vg-replay` and `vg-process` read
  it directly.
- `rx_times.csv`: the host's wall-clock and monotonic receive time for each line.
- `derived.ndjson`: everything the processor produced during the session.
- `commands.ndjson`: calibration commands, each with the `(boot_id, seq)` it followed, so
  a replay recalibrates at exactly the same point in the stream.
- `manifest.json`: the session row, processing config, adapter, and calibration record.

`POST /api/sessions/{id}/replay` streams the recording back with replay provenance. Only
`provenance` changes; the device ids, sequence numbers and timestamps stay the same. With
the commands reissued, the replay reproduces the original score. The backend tests
check this.

A calibration made before the session started is saved with it (as a `load_calibration`
entry in `commands.ndjson`) and applied to the replay as a re-labelled copy
(`…-replay`, with `replay_of_calibration_id`), so the replay reproduces the original
activation and score. Replays always judge staleness in recorded device time, so the
output does not depend on the playback speed. Session files can be downloaded only
after the session stops (409 while it is recording).

## Demo-day checklist (backend)

1. `uv run vg-backend serve --device serial --port '/dev/cu.usbmodem*'` (or the simulator
   as a fallback; it is labelled `simulated` everywhere).
2. `GET /api/runtimes/wearable` → `state: ready`, `last_rx_age_s` < 0.2.
3. Record a known-good session the day before. If live ingestion fails, use
   `POST /api/sessions/{id}/replay`; the dashboard then shows `REPLAY`.
4. After any restart, `GET /api/health` → `recovered_on_start` and `GET /api/history`
   should still show the earlier sessions.

## Limits and open items

- **Station motor commands are not implemented here.** The host→controller stimulus
  contract is not agreed yet (Fahad + Salaar). This backend stores station sessions and
  trials and broadcasts them; the station engine posts them.
- The wire format is NDJSON. If the firmware moves to a binary frame, only a decoder in
  `SerialAdapter` changes.
- Session files are flushed at least once per second, also while the device is silent or
  unplugged: a crash can lose up to about 1 s of telemetry. After a crash, the session is
  marked `interrupted`, and its row records how many complete frames are on disk.
- One server per data directory (a lock file). `vg-backend seed-history` never touches a
  running server's sessions.
- The score for one session is the change in the processor's cumulative score from start
  to stop. If the accumulation resets mid-session (provenance change), it is reported as
  unavailable, with the reason.
- No authentication: the server binds to `127.0.0.1` by default.
