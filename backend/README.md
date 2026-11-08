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
uv run pytest -q                       # ~230 tests, about 2 minutes, simulated data, offline
uv run vg-backend seed-history         # optional: 14 days of labelled SIMULATED history
uv run vg-backend serve                # http://127.0.0.1:8000, simulated wearable
```

Then open **http://127.0.0.1:8000/** (a status page: device state, data source, measurements, sessions)
or try the API (the examples assume the default port 8000; with `--http-port N` use `localhost:N`):

```sh
curl -s localhost:8000/api/runtimes/wearable | head -c 600
curl -s -XPOST localhost:8000/api/sessions -H 'content-type: application/json' -d '{"label":"try"}'
curl -s -XPOST localhost:8000/api/sessions/SESSION_ID/stop -H 'content-type: application/json' -d '{}'   # use the id printed above
```

### See a full calibrated session (one command)

With `vg-backend serve` running, this drives a whole simulated session and prints the result:

```sh
uv run python scripts/demo_calibrated_session.py
```

It waits for a fresh 30 s simulated run, starts a session, sends the relaxed capture at
0.5 s and the squeeze capture at 4.5 s of device time, stops the session, replays it, and
checks the replay reproduces the score (about 0.0486 au over about 19 s of scored time).
Stop any session you started above first (a second one is refused with 409, and the script
says so right away). It exits 0 on success and takes about a minute (it waits for a fresh run). On another port:
`uv run python scripts/demo_calibrated_session.py --url http://127.0.0.1:<port>`. The simulated
run restarts every ~30 s and a calibration cannot span a restart, which is why the script
follows the stream instead of timing by hand.

### Device sources

| Command | Device |
|---|---|
| `vg-backend serve` | Simulated wearable (`demo` scenario, 30 s runs that loop with a new boot each time). Every record is labelled `simulated`. |
| `vg-backend serve --scenario fault_mix --duration 60` | Any `vg-sim` scenario (see `uv run --project ../signals vg-sim --list`). |
| `vg-backend serve --device serial --port '/dev/cu.usbmodem*' --baud 921600` | Real firmware over USB serial. The glob finds the port again after a replug. |
| `… --imu-counts-per-g 4096 --imu-conversion-source "LSM6DS3 datasheet, ±8 g"` | Gives m/s² and the score on real hardware. Without a confirmed conversion, vibration stays in counts and the score stays unavailable (by design). |
| `vg-backend serve --device wifi` | **The wearable over Wi-Fi** (board W). It finds the laptop by UDP broadcast on 8766 and streams to TCP 8765. Uses Salaar's live configuration: accelerometer 4096 LSB/g (MPU-6500 at ±8 g, cited), so vibration is in m/s², and tool use is detected. See "The wearable over Wi-Fi" below. |
| `vg-backend fake-wifi` | A **simulated** Wi-Fi wearable that does the same discovery and streams simulator data (labelled `simulated`; default scenario `tool_vs_arm`), to rehearse without the board. |
| `vg-backend serve --device none` | API only: station records, history, replays. |

Data goes to `backend/data/` (git-ignored). Override it with `--data-dir DIR` (before or after
the subcommand) or `$VG_DATA_DIR`; a relative path is resolved once, so sessions keep working
when you start from another folder.

### The wearable over Wi-Fi

Salaar's guide is `firmware/INTEGRATION_FOR_ABDULJABBAR.md`. The backend follows it with option A: it embeds the
signals `Processor` (one per device) and does not re-implement any processing.

```sh
uv run vg-backend serve --device wifi              # dashboard at http://127.0.0.1:8000/dashboard/
uv run vg-backend fake-wifi                        # optional, in another terminal: a SIMULATED wearable
```

| Port | Who | Purpose |
|---|---|---|
| 8765 TCP | wearable → backend | telemetry (contract v1 NDJSON); `--wifi-port` |
| 8766 UDP | wearable → broadcast | discovery: the backend answers `VGDISCOVER 1 <id>` with `VGBRIDGE 1 8765`; `--discovery-port` |
| 8000 TCP | dashboard and phone → backend | web API, `/ws/live`, `/dashboard/`; `--http-port` (moved from 8765 for the wearable) |
| 8767 UDP | phone app → broadcast | the app's Find: the backend answers `VGFIND 1` with `VGAPI 1 <http port> <password 0/1> <name>`; `--app-discovery-port` |

- **Only the wearable can take the stream.** A new connection replaces the current one (the wearable reconnecting
  after a reboot) only when its first line is a telemetry record; anything else (a browser or app pointed at the
  wrong port) is closed and counted in `adapter.rejected_connections`. If something else already listens on 8765
  (`vg-bridge`, another backend), the backend refuses to start the Wi-Fi intake and says so.
- **Grip effort** is `normalized.value` (unbounded; may exceed 100 %) with `normalized.display` for bars, and the
  `above_reference` / `below_relaxed` flags. It is muscle activity relative to this wearer's calibration, not grip force.
- **Tool use** (`tool_use` records) is published on `/ws/live`, kept in the snapshot, and saved to
  `data/tool_use/<device>.json` about once a second; after a backend restart the last known totals are shown as
  `tool_use_saved` until live data arrives. Start a new shift with
  `POST /api/runtimes/wearable/tool-use/new-shift` (`request_id` makes it safe to repeat). Each recording stores its
  tool time in `counters.readings.tool_time_s`.
- **Calibration** is two steps, as the guide's phone flow: `POST /api/runtimes/wearable/calibration` with
  `{"action": "relaxed"}`, wait for `relaxed_captured`, then `{"action": "squeeze"}`; `cancel` stops. It is not kept
  across a wearable reboot (`survives_reboot = False`): after `boot_change` the screens say to recalibrate. A saved
  calibration is restored after a backend restart and shows `calibration_unverified` until new data confirms it.

### The phone app over Wi-Fi

The Android app (`android/`, see `android/README.md`) opens this backend's dashboard from a phone on the same Wi-Fi.
The web API must listen beyond the laptop for that:

```sh
uv run vg-backend serve --device wifi --host 0.0.0.0     # add VG_BASIC_AUTH=user:password off your own network
```

With a non-loopback `--host`, `serve` also answers the app's **Find on this Wi-Fi** (`appdiscovery.py`, UDP 8767):
only to private, link-local or loopback senders, only to a query padded to 64 bytes, with a reply of at most 64
bytes (it never amplifies). `--app-discovery-port 0` turns it off; on the default `--host 127.0.0.1` it never runs.
With `VG_BASIC_AUTH`, an unauthenticated `/ws/live` handshake gets the same `401` challenge as HTTP (not a bare
close), so a browser or the app's WebView retries it with the password it already holds.
- **Silence** is visible: the processor is polled every 50 ms with a 1 s stale limit, so a wearable that stops
  sending shows `stream_stale` within about a second and its values become dashes.
- Raw telemetry and derived records are stored **during recordings** (as before), each keeping its provenance.

### Demo-day tools

All `vg-backend` commands are run from `backend/` as `uv run vg-backend ...`.

| Command | What it does |
|---|---|
| `vg-backend preflight [--expect live] [--backup DIR]` | Checks a running backend: device connected, data really flowing, what the data source is (live/simulated/replay), calibration, WebSocket delivery, saved sessions intact (SHA-256), fallback backup valid, disk. PASS/WARN/FAIL with the fix for each; exit 1 if anything FAILs. |
| `vg-backend export-backup SESSION_ID backup/NAME` | Copies a finished session into a small verified folder (hashes, every frame contract-checked). Refuses to overwrite one. |
| `vg-backend verify-backup backup/NAME` | Detects a missing, edited or truncated backup. |
| `vg-backend serve --backup backup/NAME [--loop]` | Starts with the recording as the `wearable`, **labelled REPLAY** (with its original source). The dashboard needs no change. It plays once and then shows `completed` ("PLAYBACK FINISHED" on the status page); add `--loop` to play it again each time it ends, or `POST /api/runtimes/wearable/restart` to replay by hand. It cannot be DELETEd (that would leave no wearable). |

The recording replays **its own** calibration commands: if you calibrated during the recording, the
wearable shows `calibrating` for the first seconds. For a fallback that is calibrated from the
first frame, calibrate (relaxed, then squeeze) BEFORE starting the session, then start it, stop it
and export it: the calibration active at the start is saved with the session. (A session that
starts with only the relaxed reference cannot be reproduced; it is flagged
`started_with_relaxed_reference_only`.) A session cut off by a crash exports as `interrupted`, with a
warning.

`backup/demo-session/` is a committed known-good recording (SIMULATED, 1196 frames, about 1 MB):
a calibrated session that scores 0.0486 au. Replace it with a real-hardware recording on demo day.

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

## Live delivery: `ws://127.0.0.1:8000/ws/live`

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
the commands reissued, the replay reproduces the original score for a session that starts
with its first frame. A session started while the processor was already warm counts its
first frames at once, while a replay starts cold, so its score is lower by about the 1.2 s
warm-up (`score.started_mid_stream` says which case it is). The backend tests check this.

A replay runtime is named `replay-<id>`. WebSocket clients that did not ask for a runtime by
name (`?runtime=`) do not receive replay streams, so starting a fast replay can never push
live messages out of a dashboard's queue.

A calibration made before the session started is saved with it (as a `load_calibration`
entry in `commands.ndjson`) and applied to the replay as a re-labelled copy
(`…-replay`, with `replay_of_calibration_id`), so the replay reproduces the original
activation and score. Replays always judge staleness in recorded device time, so the
output does not depend on the playback speed. Session files can be downloaded only
after the session stops (409 while it is recording).

## Demo-day checklist (backend)

1. `uv run vg-backend serve --device serial --port '/dev/cu.usbmodem*' --imu-counts-per-g N --imu-conversion-source "..."`
2. `uv run vg-backend preflight --expect live --backup backup/demo-session`: every line PASS
   (WARNs are things to know, FAIL means do not demo this way).
3. Record a known-good session on the day (calibrate first, then start the session), then, from
   `backend/`: `uv run vg-backend export-backup SESSION_ID backup/day-of` and commit it
   (`git add backup/day-of`). Use another `--http-port` and `preflight --url http://127.0.0.1:PORT`
   if 8000 is taken.
4. If live data fails: stop the server and run `uv run vg-backend serve --backup backup/day-of --loop`.
   The dashboard shows the same `wearable`, now labelled `REPLAY`. Say so. Without `--loop` it plays once
   and stops; `POST /api/runtimes/wearable/restart` plays it again.
5. After any restart, `GET /api/health` → `recovered_on_start` and `GET /api/history`
   should still show the earlier sessions.

The acceptance evidence is in [`EVIDENCE.md`](EVIDENCE.md) (regenerate with
`uv run python scripts/run_acceptance.py`).

Ctrl-C or SIGTERM with a session open saves it as `interrupted` (`runtime_stopped`): its files and
counters are intact and it replays fine. Stop the session first for a clean `completed` one.

## Hardware bring-up

`vg-backend ports`, `serial-check`, `fake-device` and `serve --profile hardware.json` prepare for the real
wearable: they list boards, measure a real serial link (rate, drops, wrong baud, EMG raw vs envelope, mains
hum, bandwidth) and let the firmware be rehearsed with no hardware. `--no-dtr` stops auto-reset boards restarting on
every open; `emg.output` and `baud` must be stated in a profile (nothing is assumed). `serve --device-id vg-w-... ` picks the wearable among several ports (both boards share one USB serial number);
`serve --device stream --stream fake.ndjson` reads the firmware's own `vg-fake-device` output. Start with
[`HARDWARE_BRINGUP.md`](HARDWARE_BRINGUP.md) and `hardware.example.json`. Verified on simulated firmware over
a pseudo-serial port only.

## Station trials (PROPOSED interface)

`POST /api/station/sessions/{id}/stimulus` delivers a stimulus through a controller and opens
a trial; `/respond` takes felt / not_felt; a timeout records **missing**; `/abort` stops the
motor. Only a simulated controller exists until the contract with Fahad and Salaar is agreed:
see [`STATION_INTERFACE.md`](STATION_INTERFACE.md).

## Safety of a local API

The server has no authentication, so it defends itself against web pages you might have open:
requests whose `Origin` is not a local page, WebSocket upgrades from foreign origins, and
requests with a non-local `Host` (DNS rebinding) are refused with 403. Bodies over 1 MB are
refused. The status page escapes everything it shows and has a Content-Security-Policy. It
binds to `127.0.0.1` by default; with `--host 0.0.0.0` the Host check is off and the server
prints a warning.

## Limits and open items

- **A delivered stimulus has no record until it is answered.** The open trial lives in memory: if the
  backend crashes between delivery and the answer, the session is marked `interrupted` and that
  trial is missing. On start-up after a crash the controller is told to stop the motor.
- **Station motor commands** run against a simulated controller only. The host→controller
  stimulus contract is a proposal (`STATION_INTERFACE.md`), not agreed with Fahad and Salaar.
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
- No authentication: the server binds to `127.0.0.1` by default (see "Safety of a local API").
- **Silent devices.** A device that stays connected but stops sending is shown as
  `data_fresh: false`, its score is masked, and a `device_silent` event is published. The
  signals processor itself still reports the last score as accumulating; see `KNOWN_ISSUES.md`.
- A different wearable on the same link is switched to after 3 valid frames from its id: any
  open session is closed `interrupted` (`device_changed`) and the new device needs calibrating.
- Staleness is judged against the host clock with a 250 ms floor: a host stall of that length
  (heavy load, a pause) can show a false `stream_stale` that recovers on the next frame.
