# Station command protocol v1 (PROPOSED)

**Status:** proposed by Salaar (firmware), for review by **Fahad** (test engine) and
**Abduljabbar** (backend adapter). Authoritative implementation:
`firmware/shared/src/station.c`. Validator: `firmware/contract/station_protocol.py`.

This is **separate** from the wearable telemetry contract (`signals/contract/`). Station
commands are never sent inside EMG or IMU records, and the telemetry validator is
unchanged.

## What it is for

The laptop (Fahad's test engine) decides **what** stimulus to give and when. The station
ESP32 only **delivers one bounded vibration pulse at a time** and reports exactly what
happened, with device timestamps. The trial sequence, staircase, felt/not-felt responses
and results all stay on the host.

```
Fahad's engine ──► backend adapter ──USB serial 115200──► station ESP32 ──I²C──► DRV2605L ──► motor
       ◄────────── events (ack / started / ended / status / error) ◄──────────
```

## Transport

- **Link:** USB serial (CP2102) at **115200 8N1**, no flow control. The station uses
  115200, not 921600: traffic is tiny, and 115200 was reliable on both boards.
- **Framing:** one JSON object per line, ending in `\n`. A command line is at most **512
  bytes**, including the newline.
- **Port contents:** the station's console is disabled, so the port carries only this
  protocol. The exception is the ESP32 boot ROM banner at reset, which arrives as
  undecodable bytes: skip any line that isn't JSON.
- **Resets:** opening the port can reset the board (DTR/RTS auto-reset). A reset always
  shows up as a new `boot_id`.

## Commands (host → station)

A command is a flat JSON object: no nesting and no backslash escapes. All four common
fields are required. Unknown fields are rejected.

| Field | Value |
|---|---|
| `schema` | `"vibeguard.station_command"` |
| `version` | `1` |
| `type` | `"stimulus"`, `"abort"` or `"status"` |
| `request_id` | 1–64 characters of `[A-Za-z0-9._:-]`, **unique per command** (e.g. `sess7-trial3`) |

`stimulus` also requires:

| Field | Value |
|---|---|
| `level` | Integer **1–100**: a **motor setting**, the percentage of the configured full-scale drive (3.0 V open-loop on the current station). It is not a measured vibration amplitude |
| `duration_ms` | Integer **1–`max_duration_ms`** (1000 on the current station; never above 2000) |

Exact examples (`contract/examples/commands.ndjson`):

```json
{"schema":"vibeguard.station_command","version":1,"type":"stimulus","request_id":"sess7-trial3","level":40,"duration_ms":500}
{"schema":"vibeguard.station_command","version":1,"type":"abort","request_id":"sess7-abort-1"}
{"schema":"vibeguard.station_command","version":1,"type":"status","request_id":"sess7-status-1"}
```

## Events (station → host)

Every event has these fields:
- `schema: "vibeguard.station_event"` and `version: 1`;
- `type`, `device_id`, `boot_id`;
- `seq`: starts at 0 on each boot and goes up by 1 for every event;
- `t_us`: device microseconds since boot;
- `provenance.source`: `"live"` with the real driver, `"simulated"` with the fake one.

| `type` | When | Fields |
|---|---|---|
| `hello` | At boot, then every 5 s | `firmware`, `driver {model, actuator, drive, full_scale_v}`, `limits {level_min, level_max, max_duration_ms, heartbeat_timeout_ms, command_max_bytes}`, `state` |
| `status` | At boot (after hello), on a `status` command, and on faults | `state` (idle/active/fault/disabled), `motor` (on/off), `active_request_id`, `fault_detail`, `counters` |
| `ack` | Once per well-formed command | `request_id`, `command`, `result` (accepted/rejected/duplicate), `reason`, plus `outcome` on duplicates |
| `stimulus_started` | The driver output was switched on | `request_id`, `level`, `duration_ms` |
| `stimulus_ended` | The output was switched off | `request_id`, `outcome` (completed/aborted/link_timeout/fault), `requested_ms`, `on_ms` |
| `error` | A line could not be used as a command | `reason`, `detail`, `request_id` (null if not readable) |

Below is a real session from `vg-fake-station` (simulated). The common fields are trimmed;
the full lines are in `contract/examples/events_fake_session.ndjson`.

```json
{"type":"hello","seq":0,"driver":{"model":"fake",...},"limits":{"level_min":1,"level_max":100,"max_duration_ms":1000,"heartbeat_timeout_ms":1500,"command_max_bytes":512},"state":"idle"}
{"type":"status","seq":1,"state":"idle","motor":"off","active_request_id":null,"fault_detail":null,"counters":{...}}
{"type":"ack","seq":2,"t_us":17079133942,"request_id":"sess7-trial3","command":"stimulus","result":"accepted","reason":null}
{"type":"stimulus_started","seq":3,"t_us":17079133942,"request_id":"sess7-trial3","level":40,"duration_ms":500}
{"type":"stimulus_ended","seq":4,"t_us":17079634041,"request_id":"sess7-trial3","outcome":"completed","requested_ms":500,"on_ms":500.099}
{"type":"ack","seq":5,"request_id":"sess7-trial3","command":"stimulus","result":"duplicate","reason":null,"outcome":"completed"}
{"type":"stimulus_started","seq":7,"request_id":"sess7-trial4","level":60,"duration_ms":800}
{"type":"ack","seq":8,"request_id":"sess7-trial5","command":"stimulus","result":"rejected","reason":"busy"}
{"type":"ack","seq":9,"request_id":"sess7-abort-1","command":"abort","result":"accepted","reason":null}
{"type":"stimulus_ended","seq":10,"request_id":"sess7-trial4","outcome":"aborted","requested_ms":800,"on_ms":102.175}
{"type":"ack","seq":11,"request_id":"sess7-trial6","command":"stimulus","result":"rejected","reason":"duration_out_of_range"}
```

## Rules

- **Acknowledgements:**
  - `accepted` means the station will act on the command.
  - `rejected` gives a `reason`: `busy`, `fault`, `motor_disabled`, `level_out_of_range`,
    `duration_out_of_range` or `request_id_reused`.
  - The time the vibration actually started is in `stimulus_started.t_us`, **not** in
    the ack.
- **One stimulus at a time:** a `stimulus` while another is active is rejected as `busy`.
  Nothing is queued.
- **Duplicates:** the station remembers the last 32 `request_id`s of the current boot.
  - **Same id and same parameters:** the station replies `duplicate` with the current
    `outcome`. It **never restarts or extends** a stimulus; this is tested.
  - **Same id with different parameters:** rejected as `request_id_reused`.
  - **A duplicate `abort`:** does nothing, so it can't kill a newer stimulus.
- **Abort:** stops any active stimulus immediately. It is handled in the same task, on
  the same tick, as the received bytes. If nothing is running it is harmless and still
  `accepted`.
- **Output-off guarantees:** the motor is off:
  - **at boot:** the DRV2605L powers up in standby (TI SLOS854D), and the firmware
    forces standby again;
  - **at the end of every stimulus,** on abort, and on any driver fault;
  - **when the host goes silent:** if no well-formed command arrives for
    `heartbeat_timeout_ms` (1500 ms) during a stimulus, it ends with `link_timeout`.
  - **No pulse is ever longer than `max_duration_ms`.** The firmware also has a hard
    ceiling of 2000 ms that no configuration or command can exceed.
- **Faults:** if the driver reports over-current or over-temperature, or any I²C access
  fails, output goes off and the station latches `state: fault` until reboot. Stimuli
  are then rejected with `fault`.
- **Malformed input:** produces an `error` event and never moves the motor. This covers
  bad JSON, the wrong schema or version, an unknown type or field, a non-integer level,
  and similar. A line over 512 bytes is discarded whole (`oversized`), and the parser
  resynchronises at the next newline.
- **Reboot identity:** every boot gets a new random `boot_id`, and `seq` restarts at 0.
  The station forgets earlier request ids. If the host sees a new `boot_id`, any stimulus
  it had in flight is over: the motor was off through the reset.
- **Timing:** `on_ms` is measured on the device, from switch-on to switch-off. The
  control loop runs at 1 ms, so expect requested + about 0–2 ms on hardware (measured:
  see `firmware/HANDOFF.md`).

## Using it today without hardware

`vg-fake-station` (built with the host tests) runs this exact controller with a fake motor
and always reports `simulated`. It reads commands on stdin and writes events on stdout,
in real time:

```sh
cd firmware && cmake -S tests/host -B build/host -G Ninja && cmake --build build/host
echo '{"schema":"vibeguard.station_command","version":1,"type":"stimulus","request_id":"t1","level":40,"duration_ms":300}' \
  | (cat; sleep 1) | build/host/vg-fake-station
python3 contract/station_protocol.py events.ndjson            # validate captured events
python3 contract/station_protocol.py --commands cmds.ndjson   # validate your commands
```

To exercise the real serial path without hardware, connect it to a pseudo-terminal
(e.g. `socat PTY,link=/tmp/vgstation,raw EXEC:build/host/vg-fake-station`).

## Open points for Fahad and Abduljabbar

1. **Is `level` 1–100 the right stimulus unit for the staircase?** It is a motor setting.
   Measured amplitude needs the station IMU, which isn't wired yet.
2. **Are 1000 ms maximum pulses enough?** The ceiling can go up to 2000 ms. Longer
   pulses need status heartbeats every < 1500 ms.
3. **Who owns the serial port on the laptop?** Proposal: the backend adapter opens it
   and the engine talks to the backend.
4. **Felt/not-felt responses:** dashboard buttons through the backend. No physical
   button is planned in this firmware.
