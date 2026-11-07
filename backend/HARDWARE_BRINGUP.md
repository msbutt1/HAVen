# Hardware bring-up: the backend side

For Collins (wiring) and Salaar (firmware). The backend can already read a real serial device; these
tools tell you, in seconds, whether what the board sends is good enough, and what to fix when it is not.
Everything here was exercised on a **simulated** firmware over a pseudo-serial port. No real board has been
connected yet: the first real run may find things this guide does not cover. Tell Abduljabbar what you see.

All commands run from `backend/` as `uv run vg-backend ...`.

## 0. Before the board arrives: rehearse

```sh
uv run vg-backend fake-device                    # prints a port like /dev/ttys003 and acts like the firmware
uv run vg-backend serial-check --port /dev/ttys003 --baud 230400 --seconds 10
uv run vg-backend serve --device serial --port /dev/ttys003 --baud 230400 --http-port 8766
```

The fake device is SIMULATED firmware (no injected faults by default; `--scenario demo` or `fault_mix` add
scripted ones on purpose). Like most USB-serial boards it starts a fresh boot (hello first) each time a program
opens the port, about 0.1 s after it opens. Try its faults to see what a problem looks like in the tools:
`--mode garbage` (wrong-baud symptoms), `--mode silent`, `--hang-after 5`, `--unplug-after 5` (seconds from the
first connection), `--no-hello`, `--rate-error 0.05` (a firmware clock 5% off, at most 0.10).

**What a pseudo-port cannot do.** On macOS it only accepts some baud rates (115200 and 230400 work; 460800 and
921600 fail with "Inappropriate ioctl for device"), and it does not throttle. So with the fake device the
`link bandwidth` line WILL read FAIL (about 136% at 230400): the stream needs about 31 kB/s, more than 230400 baud
can carry. That is the real finding from the signals handoff, not a broken setup. Judge the other lines.
On Linux the fake device cannot tell when a program connects and starts its first boot at once, so the first hello may
be missed (reconnect: a fresh boot). A very quick close-and-reopen (under about 50 ms) is also not noticed.

## 1. Plug it in

```sh
uv run vg-backend ports
```

A board shows up as `/dev/cu.usbmodem*` (native USB) or `/dev/cu.usbserial-*` / `/dev/cu.wchusbserial*`
(USB-UART bridge); the tool says what it recognises. Nothing listed? Try another cable first: many are
power-only.

## 2. Is the link good? `serial-check`

```sh
uv run vg-backend serial-check --port '/dev/cu.usbmodem*' --baud 921600 --seconds 30 --save rec.ndjson
```

Unsure of the baud rate: `--try-bauds 115200,230400,460800,921600` finds the one that gives valid frames. A rate the
port refuses is printed, not hidden. On native USB several rates "work" (the baud is ignored): any will do.
A board that resets when the port opens (Arduino-style, ESP32 dev kits) restarts at every open, so each probe and
each check restarts the firmware; add `--no-dtr` to avoid that (not for native-USB boards that wait for DTR).
Each probe waits up to 10 s for a slow-booting board (`--probe-seconds` sets the talking time).
It runs the **same validator and processor the backend uses**, so PASS means the backend will accept it.
`--save` keeps the raw bytes for `uv run --project ../signals vg-validate rec.ndjson` and `vg-process`.

| Check | PASS means | If it fails |
|---|---|---|
| port opens | the port is free (the tool takes an exclusive lock) | another program has it (Arduino/PlatformIO serial monitor, another vg-backend or serial-check): close it. Linux "permission denied": add yourself to the `dialout` group. Wrong name: `ports` |
| port is ambiguous | the name matched one port | a glob matched several boards: name the exact one |
| data arrives | bytes are flowing | firmware not running / TX not enabled / wrong port; some boards reset on connect: wait. The tool only LISTENS: a firmware that waits for a command, or for DTR (`while(!Serial)`), looks silent |
| contract frames | lines are valid contract v1 NDJSON | the message says which: binary bytes = **wrong baud** or NUL padding; JSON that breaks the contract = a firmware bug (`--save`, then `vg-validate`); plain text = debug prints on the same port. It quotes the first rejected line |
| data labelled live | firmware sends provenance `live` | real firmware must send `{"source":"live"}`; a `simulated` label (copied from the simulator) is a WARN and the backend would label every recording simulated |
| hardware facts known | the hello names firmware, board, rates, raw limits, sensor models | unresolved = the firmware says `null`: fill it in once the parts are confirmed (clipping needs `raw_limits`) |
| emg / imu sample rate | measured from the timestamps, within one uninterrupted stretch (never across a reboot), is within 1% of nominal | 1-5% WARN, more FAIL: wrong ODR/ADC rate setting or timestamps (e.g. `micros()` wrap). If `device rebooted` is also reported, fix the reset first |
| emg / imu clock vs host | (printed only when WARN) device timestamps agree with how fast samples really arrive | the timestamps may be synthesised (index x interval) instead of read from a clock, or the link drops samples |
| emg values | (printed only when WARN/FAIL) the sensor delivers numbers | missing values or `sensor_disconnected`: wiring, supply |
| emg is raw | broadband (most power above 20 Hz after removing slow drift) and symmetric around its bias | almost no power above 20 Hz = an **envelope**: STOP, the pipeline assumes raw EMG (HANDOFF section 7). One-sided = possibly rectified (WARN). Every EMG channel is judged |
| mains hum | no significant power at 50/60 Hz or its harmonics (always printed) | grounding, reference electrode, cable routing; `--mains-hz 50` if you are on 50 Hz |
| emg / imu clipping | (printed when WARN/FAIL) under 0.1% of samples at the ADC limits | reduce gain or range; over 5% = FAIL. Needs `raw_limits` in the hello |
| no records lost | no seq gaps, no dropped samples, no unflagged holes in the sample index | link or buffer problem: raise baud, shrink batches, native USB; the firmware should report drops itself |
| data keeps flowing | (printed only when WARN/FAIL) no silence longer than 2-5 s | hang / watchdog / stuck USB bridge; says if malformed lines kept arriving |
| link bandwidth | the stream fits the link, measured while data flowed (8N1 assumed) | JSON at the provisional rates needs about 22-46 kB/s: **more than 115200 or 230400 baud** can carry. Use >= 460800 (921600 is comfortable), native USB, or a binary encoding (HANDOFF section 2.9). On native USB (`usbmodem`/`ttyACM`) the baud setting is ignored: no verdict |
| also printed | link stays up, hello received, frame size, duplicate records, device rebooted (also a `boot_id` that does not change), device-reported problems, profile matches device | each line says what to do |

## 3. Tell the backend what the hardware is: a profile

Copy `hardware.example.json` to `hardware.json` and fill in only what is **confirmed**:

```json
{ "name": "wearable-v1-bench", "port": "/dev/cu.usbmodem*", "baud": 921600,
  "emg": {"output": "raw", "channels": 1, "mains_hz": 60},
  "imu": {"model": "MPU-6050", "range_g": 2, "counts_per_g": 16384,
          "conversion_source": "MPU-6050 datasheet, sensitivity at the +/-2 g range"} }
```

```sh
uv run vg-backend serve --profile hardware.json
```

- `imu.counts_per_g` needs `conversion_source`: without a cited sensitivity vibration stays in raw
  counts and the score is unavailable, by design. Never guess it.
- `emg.output: "envelope"` is refused: the pipeline would band-pass and RMS an already-smoothed signal.
- Unknown field names are errors (a typo must not silently change what the backend assumes).
- `emg.output` and `baud` are required (nothing is assumed). `emg.output: "raw"` should be backed by the
  `emg is raw` line of `serial-check`.
- The profile (and the settings actually used) is stored with every recording and shown on the status page
  (its name on the Device facts card). `serial-check --profile hardware.json` reads `mains_hz` and `serial` from it
  and compares the channel count with the device's hello; a profile that contradicts the hello shows as a warning
  on the status page. `emg.channels` is checked; `imu.model`, `range_g` and `notes` are only stored.
- Flags on the command line (`--port`, `--baud`) override the profile. A different sensitivity on the command line
  (`--imu-counts-per-g`) needs its OWN `--imu-conversion-source`: the profile's citation is for the profile's number.
- `serial: {"dtr": false, "rts": false}` (or `--no-dtr`) stops an auto-reset board from restarting on every open.
- `serve --device none --profile ...` opens no device (the profile is only validated).

## 4. What the backend needs to be told (from signals/HANDOFF.md section 4.4)

| Fact (Collins) | Where it goes |
|---|---|
| Board model, how many controllers, USB type | firmware hello `hardware.board`; `serial-check` shows it |
| EMG module: model, **raw or envelope**, output voltage range, bandwidth | `emg.output` in the profile; hello `sensor_model`; `serial-check` tests rawness |
| ADC resolution and reference | hello `streams.emg.raw_limits` (min/max counts) |
| IMU: model, range, output data rate, internal low-pass | profile `imu.*`; hello `streams.imu.*` |
| Where the IMU sits, grounding, electrode placement | `notes` in the profile (stored with recordings) |
| Link throughput | `serial-check` link bandwidth line |
| Mains frequency | `emg.mains_hz` / `--mains-hz` |

## 5. Bring-up order (HANDOFF 4.4), with the commands

1. `ports`, then confirm the facts above. Collins checks wiring and logic levels BEFORE anything touches a person.
2. Firmware emits contract records from fake data first, then real sensors.
3. `serial-check --seconds 60 --save rec.ndjson`: rate matches, no gaps, raw EMG, no hum.
4. `uv run vg-backend serve --profile hardware.json --http-port 8765 --data-dir data`, then open
   `http://127.0.0.1:8765/`: the **Device facts** card lists what the device says it is and the measured rates;
   unresolved items are shown as such. (Use another `--http-port` if 8765 is taken.)
5. `uv run vg-backend preflight --expect live`: all PASS.
6. Calibrate a wearer, relaxed first and then squeeze (a weak squeeze must fail with `insufficient_separation`):
   `curl -XPOST localhost:8765/api/runtimes/wearable/calibration -H 'content-type: application/json' -d '{"action":"relaxed"}'`
   then `-d '{"action":"squeeze"}'`. Calibrate BEFORE starting the session you will keep as the fallback.
7. Switch the vibration source on: m/s^2 rises, no clipping, and check the relaxed EMG does not rise from
   vibration alone (the hour-12 gate).
8. Record: `curl -XPOST localhost:8765/api/sessions -H 'content-type: application/json' -d '{"label":"hardware run"}'`,
   later `.../api/sessions/SESSION_ID/stop`, then `uv run vg-backend export-backup SESSION_ID backup/real-hardware`
   and commit it. That is the real-hardware fallback (`serve --backup backup/real-hardware --loop`).

## 6. Symptom guide

| You see | Probably | Do |
|---|---|---|
| `ports` shows nothing | power-only cable, no driver (CH340/CP210x), board not powered | other cable; install the bridge driver; check the LED |
| `port opens` fails "busy" | a serial monitor has it | close it |
| contract frames FAIL, garbage | wrong baud | `--try-bauds`, or match the firmware's `Serial.begin` |
| valid frames but a few % rejected | debug prints on the same port | remove them, or send them on another UART |
| no hello | printed before the host opened the port | send the hello on boot AND again when the host connects |
| rate 2-5% off | wrong output data rate setting | fix the setting; report the measured rate |
| rate way off | wrong timestamps | extend `micros()` to 64 bits (wraps every 71.6 min); microseconds, not ms |
| seq gaps | too slow a link, buffer overrun | raise baud / native USB / smaller batches |
| device resets every few seconds | brown-out (USB power), watchdog, or the host re-opening the port (DTR auto-reset) | power the board properly; check the `device rebooted` line; try `--no-dtr` |
| EMG flat | no contact, no reference electrode | gel electrodes, check the reference |
| status page: "NO NEW DATA" banner or `Last data N s ago (not fresh)` | firmware hung, link up (the backend calls this a silent device) | reset the board; check the watchdog |
| a hello is missing though the firmware sends one | it was sent before the host opened the port | send it on boot AND again when the host connects, or every 1-2 s until the host answers |
| firmware waits for the host (command or DTR) | this tool only listens | make it send unprompted, or use DTR (`if (Serial)` on native USB) |

## 7. What these tools cannot tell you

- Whether the EMG is a good measurement of a muscle, or the IMU of the tool's vibration: that needs a person
  and a controlled source. The tools only test that the numbers are plausible and consistently delivered.
- Real USB timing: re-enumeration after an unplug, driver buffering, behaviour at the board's true baud limit.
  Pseudo-port tests do not cover them.
- The station motor path: the stimulus command contract with Fahad and Salaar is a proposal
  (`STATION_INTERFACE.md`).
