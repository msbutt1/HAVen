# Station interface: PROPOSED (for Fahad and Salaar to review)

**Status: proposal.** Nothing in this file is agreed. The backend implements it against a
simulated `MockStationController` so the station screens, the station engine and the backend
can be built in parallel. If you need something different, tell abduljabbar; the engine
(`src/haven_backend/station.py`) is small and easy to change.

**Why this exists.** The project plan lists these commands in the shared contract: start/abort
station test, deliver stimulus, submit response. The backend stores and broadcasts station
results, and it needs a defined way to drive the motor and time the response. Fahad owns the
test behaviour (staircase, reversals, catch trials); this only covers one trial at a time.

## Division of work

| Who | Owns |
|---|---|
| Fahad | The test protocol: which setting next (staircase, 8 reversals, catch trials), when to stop |
| Abduljabbar (backend) | One trial: deliver, wait for the answer, time it, record it, abort safely |
| Salaar / Collins | The controller itself: how a stimulus reaches the motor (see below) |

## Controller interface (backend to controller)

```
deliver(stimulus, trial_id) -> {accepted, controller_time_us|null, measured_amplitude|null, error|null}
abort()                     -> {stopped}          # must always make the motor safe
```

- `stimulus` is a motor **setting**, for example `{"motor_setting": 120, "units": "pwm_duty_0_255"}`.
  The backend does not interpret it.
- `measured_amplitude` is `{value, units, source}` **only if the hardware measured it**. If there
  is no measurement it is `null` and the record says nothing about amplitude. It is never
  derived from the setting.
- `accepted: false` means nothing was delivered. The backend then opens no trial and returns 502.
- Delivery must return promptly. The vibration duration (if the motor stops by itself) is the
  controller's business; the backend also calls `abort()` when the wearer answers, on timeout
  and on abort.
- Open question for Salaar: serial framing for these two commands (the device telemetry
  contract is device to host only). Suggested: the same NDJSON framing, host to device,
  `{"type":"deliver","trial_id":...,"stimulus":{...}}` and `{"type":"abort"}`, with a
  `{"type":"ack",...}` reply. A real `SerialStationController` would implement the two methods above.

## HTTP API (station screens to backend) (now implemented on the backend)

| Call | Meaning |
|---|---|
| `POST /api/station/sessions` | start a station session (already existed) |
| `POST /api/station/sessions/{id}/stimulus` | deliver a stimulus and open a trial. Body: `{"stimulus": {...}, "is_catch": false, "response_timeout_s": 3.0, "request_id": "..."}` |
| `POST /api/station/sessions/{id}/respond` | the wearer answers. Body: `{"response": "felt" \| "not_felt"}` |
| `GET  /api/station/sessions/{id}/trial` | the open trial, if any, with how long it has waited |
| `POST /api/station/sessions/{id}/complete` | finish normally (409 while a trial is waiting) |
| `POST /api/station/sessions/{id}/abort` | stop the motor, close any open trial as missing, end `aborted` |

Rules enforced by the backend:

- **A timeout is `missing`, never `not_felt`.** Only an explicit answer is `felt` or `not_felt`.
  A late answer after the timeout is refused (409) and not recorded.
- **One open trial per session.** A second stimulus while one waits is a 409.
- **Catch trials** (`is_catch: true`) deliver nothing; the answer is still collected, and the
  summary counts them separately.
- **Abort** stops the motor first, then records the open trial as `missing` with the note
  `aborted before a response`, and ends the session `aborted`.
- **Repeated commands** with the same `request_id` return the first result and change nothing.
- **Timing** is measured on the host: `presented_at`, `responded_at` (UTC) and
  `response_latency_ms` (monotonic clock). It includes any controller and USB latency.
- Backend shutdown stops the motor and closes the open trial as `missing`. After a **crash** the open
  trial (kept in memory) is gone: the session is marked `interrupted`, that trial has no record, and the
  controller is told to stop the motor when the backend starts again.
- `abort` stops the motor twice (before and after waiting for any in-flight delivery), so it is safe
  even while a slow `deliver` call is still running. If `deliver` raises or returns a bad acknowledgement
  the backend also stops the motor before reporting the error (502).
- The answer's `response_latency_ms` is taken before the motor is stopped, so a slow controller does not
  inflate it. `complete` is refused (409) until the answer has actually been stored.

## Live events (WebSocket, channel `station`)

`station_stimulus_delivered`, `station_trial_recorded`, `station_session_completed`,
`station_session_aborted`, `station_controller_error`.

## What the summary does and does not say

`summary` counts responses for stimulus trials and catch trials. It does **not** estimate a
threshold: settings are not measured amplitudes, and the protocol is a prototype, not a validated
procedure. Do not present it as a diagnostic result.
