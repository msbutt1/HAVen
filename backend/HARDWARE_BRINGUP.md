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
uv run vg-backend serve --device serial --port /dev/ttys003 --baud 230400
```

The fake device is SIMULATED firmware. Like most USB-serial boards it starts a fresh boot (hello first) each
time a program opens the port. Try its faults to see what a problem looks like in the tools:
`--mode garbage` (wrong-baud symptoms), `--mode silent`, `--hang-after 5`, `--unplug-after 5`, `--no-hello`,
`--rate-error 0.05` (a firmware clock that is 5% off). A pseudo-port ignores the baud rate, so the bandwidth
line reports what the stream NEEDS, not what a pty can carry.

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

Unsure of the baud rate: `--try-bauds 115200,230400,460800,921600` finds the one that gives valid frames.
It runs the **same validator and processor the backend uses**, so PASS means the backend will accept it.
`--save` keeps the raw bytes for `uv run --project ../signals vg-validate rec.ndjson` and `vg-process`.

| Check | PASS means | If it fails |
|---|---|---|
| port opens | the port is free and openable | something else holds it (Arduino/PlatformIO serial monitor, another vg-backend); wrong name |
| data arrives | bytes are flowing | firmware not running / TX not enabled / wrong port; some boards reset on connect: wait |
| contract frames | lines are valid contract v1 NDJSON | **wrong baud** (garbage) or stray `Serial.print` debug text on the same port |
| hardware facts known | the hello names firmware, board, rates, raw limits, sensor models | unresolved = the firmware says `null`: fill it in once the parts are confirmed (clipping needs `raw_limits`) |
| emg / imu sample rate | measured from the timestamps is within 1% of nominal | off by 1-5% WARN, more FAIL: wrong ODR/ADC rate setting or timestamps (e.g. `micros()` wrap) |
| emg is raw | noisy, signed-around-mid-scale samples | smooth and one-sided = an **envelope**: STOP, the pipeline assumes raw EMG (HANDOFF section 7) |
| mains hum | little power at 50/60 Hz | grounding, reference electrode, cable routing; `--mains-hz 50` if you are on 50 Hz |
| clipping | no samples at the ADC limits | reduce gain, check the input |
| no records lost | no seq gaps, no dropped samples | link or buffer problem: raise baud, shrink batches, native USB |
| data keeps flowing | frames keep arriving to the end | hang / watchdog / stuck USB bridge: the backend would show this as a silent device |
| link bandwidth | the stream fits the link | JSON at the provisional rates needs about 22-46 kB/s: that is **more than 115200 or 230400 baud** can carry. Use >= 460800 (921600 is comfortable), native USB, or a binary encoding (HANDOFF section 2.9) |

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
- The profile is stored with every recording (in the runtime meta) and shown on the status page.
- Flags on the command line (`--port`, `--baud`, `--imu-counts-per-g`) override the profile.

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
4. `serve --profile hardware.json`, open `http://127.0.0.1:8765/`: the **Device facts** card lists what the
   device says it is and the measured rates; unresolved items are shown as such.
5. `uv run vg-backend preflight --expect live`: all PASS.
6. Calibrate a wearer (relaxed, then squeeze); a weak squeeze must fail with `insufficient_separation`.
7. Switch the vibration source on: m/s^2 rises, no clipping, and check the relaxed EMG does not rise from
   vibration alone (the hour-12 gate).
8. Record a session, `export-backup`, and keep it as the real-hardware fallback.

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
| device resets every few seconds | brown-out (USB power), watchdog | power the board properly; check the `reboots` line |
| EMG flat | no contact, no reference electrode | gel electrodes, check the reference |
| `device_silent` on the status page | firmware hung, link up | reset the board; check watchdog |

## 7. What these tools cannot tell you

- Whether the EMG is a good measurement of a muscle, or the IMU of the tool's vibration: that needs a person
  and a controlled source. The tools only test that the numbers are plausible and consistently delivered.
- Real USB timing: re-enumeration after an unplug, driver buffering, behaviour at the board's true baud limit.
  Pseudo-port tests do not cover them.
- The station motor path: the stimulus command contract with Fahad and Salaar is a proposal
  (`STATION_INTERFACE.md`).
