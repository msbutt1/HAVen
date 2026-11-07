# VibeGuard firmware

Two ESP32 roles share one portable layer. Host algorithms (EMG filtering, calibration,
vibration features, scoring) stay in `../signals/`; firmware only acquires data and
controls bounded actuators.

| Path | What | Builds on |
|---|---|---|
| `shared/` | Portable C11: hardware-config types, ESP32 GPIO/ADC table, pin and motor-safety checks, fake (SIMULATED) sources | host + ESP32 |
| `boards/<name>/board.c` | Board facts only (chip, pins, enabled peripherals), selected at build time | host + ESP32 |
| `target/` | ESP-IDF-only startup report: measured chip info, config report, GPIO/ADC table cross-check against the IDF driver | ESP32 |
| `wearable/` | ESP32 #1 app: BioAmp EMG + IMU #1 | ESP32 |
| `station/` | ESP32 #2 app: actuator, IMU #2, Velostat, optional OLED/DHT11 | ESP32 |
| `diag/` | Bring-up diagnostic: IMU identity and register readback, BioAmp-free ADC check; console at 115200 ([`WIRING.md`](WIRING.md)) | ESP32 |
| `tests/host/` | Host unit tests; every board file is compiled and checked; `check_fake_streams.py` validates generated telemetry with `signals` | host |
| `tools/fake_device/` | `vg-fake-device`: the firmware's telemetry pipeline on a simulated clock (always `simulated`) | host |
| `tools/build.sh` | Builds one role for one board in an isolated build dir | — |

**Backend (Abduljabbar):** start with [`TRANSPORT.md` → *Start here*](TRANSPORT.md#start-here-abduljabbar-what-this-is-and-where-it-plugs-in): what the device sends, where your serial adapter fits, and how to test it today with `vg-fake-device`.

Status, unresolved hardware facts and evidence levels: [`HANDOFF.md`](HANDOFF.md).
Telemetry pipeline, sample-index rules, wire load and reconnect: [`TRANSPORT.md`](TRANSPORT.md).
Versions: [`VERSIONS.md`](VERSIONS.md). Boards and pins: [`boards/README.md`](boards/README.md).

## Host tests (no ESP-IDF needed)

```sh
cd firmware
cmake -S tests/host -B build/host -G Ninja
cmake --build build/host
ctest --test-dir build/host --output-on-failure   # includes contract_validation (needs uv + ../signals)

# fake device -> authoritative validator / processor
build/host/vg-fake-device --seconds 10 | (cd ../signals && uv run vg-validate -q -)
build/host/vg-fake-device --seconds 10 --baud 921600 --stall 3:5 | (cd ../signals && uv run vg-process - -o /dev/null)
```

## Board builds (ESP-IDF v6.1; install steps in VERSIONS.md)

```sh
cd firmware
. ~/esp/esp-idf-v6.1/export.sh
tools/build.sh wearable esp32_d0wd_v3_devkit30   # board W's chip, read with esptool
tools/build.sh station  esp32_d0wd_v3_devkit30   # board S's chip (identical), read with esptool
tools/build.sh wearable unconfigured             # fake-only build, no hardware facts
tools/build.sh station  unconfigured
```

Read a connected board's identity (read-only) with the ESP-IDF Python environment:

```sh
python -m esptool --port /dev/ttyUSB0 chip-id    # after export.sh; base conda python lacks esptool
python -m esptool --port /dev/ttyUSB0 flash-id
```

Each build goes to `build/<role>-<board>/`, with its own `sdkconfig`. Extra arguments
pass through to `idf.py`. For example, `tools/build.sh wearable unconfigured size`.

**Flashing is not part of this step.** For bring-up (Prompt 3), with a confirmed port:

```sh
tools/build.sh wearable <board> -p /dev/ttyUSB0 flash monitor
```

On this Linux machine, serial access needs membership in the `dialout` group.

## Rules the code enforces

- **Motor output is off.** `vg_motor_output_permitted()` returns true only for the station
  role with the motor enabled, a confirmed brushed-ERM actuator, a confirmed driver,
  `max_pulse_ms` ≤ 2000, and zero pin errors. No board in the repository enables the
  motor, and host tests fail if one does. This build contains no GPIO output or PWM code
  at all.
- **Pins are checked against the ESP32 rules,** from the ESP-IDF v6.1 docs. The checks
  cover flash, UART0, input-only, strapping, ADC1/ADC2 and PSRAM pins, plus duplicates.
- **Fake data is labelled.** Fake sources are labelled `simulated`. Real adapters will be
  separate types.
