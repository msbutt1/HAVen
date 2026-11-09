# HAVen firmware — handoff and hardware-facts log

**Owner:** Salaar (production firmware). Wiring and throwaway bring-up sketches: Collins.
Station test sequencing: Fahad (host). Ingestion/APIs: Abduljabbar. Host algorithms
(EMG filtering, calibration, vibration features, scoring) stay in `signals/` and are not
re-implemented on the controller.

**Status (2026-11-07): board W streams LIVE EMG + IMU over Wi-Fi (contract v1, ~41 records/s) to `vg-bridge`, which runs the signals Processor live and publishes derived records (TCP 8767). Station control (Prompt 5) done on board S. EMG signal quality still limited by the BioAmp setup (no electrodes, BANDPASS jumper default).**

| Evidence level | Status |
|---|---|
| host-tested | **Yes.** 7 ctests: logic, telemetry (rings, queue, encoder, batcher, link, time wrap), **contract_validation** (fake-device streams checked by the `signals` validator and Processor), and one per board file |
| board-compiled | **Yes.** Both roles compile for `esp32_d0wd_v3_devkit30`, the chip read with esptool from **both** boards, using the measured 4 MB flash |
| flashed | **No.** Board W is connected, and only read-only esptool commands have been run |
| measured on hardware | **No** |
| wearer-tested | **No** |

The wearable build streams contract-v1 telemetry from **fake (simulated)** sources over UART0.
No build reads a real sensor or drives a motor. The station build is unchanged since Prompt 1.

Evidence levels used in this file (never merged):
`host-tested` → `board-compiled` → `flashed` → `measured on hardware` → `wearer-tested`.
A compiled fake-device build is not a validated wearable.

---

## Layout (created in Prompt 1; see README.md)

```
firmware/
├── HANDOFF.md / README.md / VERSIONS.md / IDF_VERSION
├── shared/        portable C11: hw-config types, ESP32 GPIO/ADC table, pin + motor checks,
│                  fake (SIMULATED) sources. Telemetry encoder lands here in Prompt 2.
├── boards/        facts only: unconfigured (default), esp32_devkit30_provisional, TEMPLATE
├── target/        ESP-IDF-only startup report + GPIO/ADC table cross-check vs IDF driver
├── wearable/      ESP32 #1 app (skeleton)
├── station/       ESP32 #2 app (skeleton; contains no GPIO output code)
├── tests/host/    host tests (ctest)
└── tools/build.sh role × board builds into build/<role>-<board>/
```

Rules carried into every step:
- Optional peripherals (OLED, DHT11, NeoPixel, sound sensor) are compiled out or disabled
  until configured.
- Motor output is disabled in any target whose motor type, driver and wiring are not
  confirmed. Bare-GPIO motor drive is never a fallback.
- Fake acquisition is labelled `simulated`. Only real hardware acquisition may emit
  `source: live`, and fake samples are never substituted into a live stream.
- Station commands are a separate, proposed, versioned contract. They are not added to
  device telemetry v1.

## Toolchain (Prompt 1 decision)

**ESP-IDF v6.1**, commit `fff9895c82d7`, installed at `~/esp/esp-idf-v6.1`. Rationale,
exact versions and install commands are in `VERSIONS.md`. There was no existing team
toolchain to reuse. No third-party components are used.

---

## Facts read from photos (2026-11-07)

These are visual identifications only, from four photos supplied by Salaar. They are not
electrical or on-device verification. "Unreadable" means the photo does not resolve it.

| Part | What the photo shows | Still unconfirmed |
|---|---|---|
| ESP32 dev board (one photographed; is the second identical?) | 30-pin DevKit-style board, USB-C, EN and BOOT buttons. Left header: EN, VP, VN, D34, D35, D32, D33, D25, D26, D27, D14, D12, D13, GND, VIN. Right header: D23, D22, TX0, RX0, D21, D19, D18, D5, TX2, RX2, D4, D2, D15, GND, 3V3. Module shield is printed **"ESP-32"**, code "XX5R69". It is **not** printed with an Espressif "ESP32-WROOM-32…" name, so it is likely a third-party/clone module. The pin set (VP/VN, D34/D35, TX2/RX2) is consistent with the original dual-core ESP32 and a module that has no PSRAM on GPIO16/17. USB-serial chip U2 is a square QFN; its marking is unreadable. | Chip model, revision and flash size: read with `esptool chip_id` / `flash_id` once connected. USB-serial chip: identify from its VID:PID in `lsusb`. Whether board #2 is identical. |
| IMU modules (×2, same design per Salaar; one module photographed front and back) | Front silkscreen "MPU-9250/6500" with X/Y/Z axis markings; back silkscreen "GY-6500 GY-9250" with dual-function labels (SCL/SCLK, SDA/SDI, ADD/SDO), i.e. I²C or SPI. Pins: VCC, GND, SCL, SDA, EDA, ECL, AD0, INT, NCS, FSYNC. A 3-terminal SOT-23 part near VCC is likely an on-board regulator (unverified). | Chip marking unreadable; populated chip identity per module must come from a WHO_AM_I read on-device (Prompt 3) against TDK register maps — the same board design does not guarantee the same chip. Regulator part/VCC input range and pull-up values unverified. |
| BioAmp EXG Pill (**×3 available** per Salaar, 2026-11-07; one board photographed front and back) | Front: Upside Down Labs "BioAmp EXG Pill", **"SEP 2021 – v1.0"**, header OUT (yellow) / GND (black) / VCC (red), one 14-pin SOIC IC, JST electrode connector. Back (silkscreen): OUT GND VCC; **FILTER** (two empty 2-pad 0603 footprints); **BANDPASS** solder jumper labelled "NARROW", which **appears OPEN** (two half-pads with a visible gap); **GAIN** (empty 0603 footprint); **ELECTRODES** 3-pad jumper labelled "2E", which **appears OPEN** (gaps between pads); electrode pins REF, IN+, IN−. | Jumper states are read from a blurry photo. Confirm unpowered with a magnifier or a multimeter continuity check across each jumper. If BANDPASS is open, the board is in its **default EEG/EOG configuration, not EMG** (manufacturer docs: bridge the pads below BANDPASS for EMG/ECG). ELECTRODES open = 3-electrode mode (the default and recommended mode). |
| Arduino Uno R3 | Genuine "Made in Italy" Uno: ATmega328P DIP, USB-B, 5 V logic. | Backup only. Not used, since ESP32s are selected for both roles. |

**Manufacturer documentation for the BioAmp EXG Pill v1.0**
([Upside Down Labs docs](https://docs.upsidedownlabs.tech/hardware/bioamp/bioamp-exg-pill/index.html),
read 2026-11-07):
- **Default configuration is EEG/EOG.** For EMG, the documentation says to "bridge the pads
  (below bandpass) with solder". Collins must check the back of the board before any EMG
  recording; until then the pipeline's EMG band assumptions do not apply.
- **Supply:** listed as 3.3 V / 5 V. The board is "by default compatible with 5V but you
  can make it compatible with 3.3V as well using a voltage divider".
  - The page does not state the output bias/midpoint or the output swing.
  - The page does not say exactly where the divider goes.
  - These are still open (H5). Do not connect OUT to an ESP32 pin until they are resolved.
- **Swapped power pins destroy the board.** Check the VCC and GND wires against the
  silkscreen before powering it.
- **Noise advice:** keep the laptop off its charger and at least 5 m from AC appliances.
  The page gives no isolation rating.
- Electrode inputs are IN+, IN− and REF. The two-electrode mode is for battery-operated
  setups only.

## Unresolved hardware facts

None of these may be assumed in code. Each blocks only the items listed.

| # | Fact needed | From | Blocks |
|---|---|---|---|
| H1 | **Answered (esptool) for both boards:** ESP32-D0WD-V3 rev v3.1, 40 MHz crystal, 4 MB flash (0x68/0x4016), flash voltage strapped 3.3 V. Board W MAC 28:05:a5:07:55:54, board S MAC 28:05:a5:07:fb:6c. Identical chips: one board config serves both | `esptool chip_id`/`flash_id` once connected; photo of board #2 | Target-specific build, pin assignment, ADC channel choice, USB/serial type |
| H2 | USB-serial path: **CP2102 UART bridge on both boards (measured)**. Board W's link drops long transfers at 460800 (flash reads; also seen after changing USB port); 115200 is reliable. **921600 telemetry on board W is at risk**: measure before relying on it | Collins / measured | Transport choice (NDJSON vs binary), host port mapping |
| H3 | **Wearable module: MPU-6500 (WHO_AM_I 0x70, measured on board W).** Station module: not read yet. pin labels **now known** (both modules same GY-9250/6500 design); regulator part: SOT-23 marking reported as **"LB2K"** (2026-11-07), not identified by web search; VCC input range therefore still unconfirmed (seller listings for GY-9250 boards claim 3–5 V via on-board LDO — not trusted). Measure the regulator output with VCC=3V3 before relying on it. **Proposed (Salaar to confirm):** I²C, AD0→GND (0x68 per TDK docs, verify in Prompt 3), each IMU alone on its own ESP32's bus, VCC from the ESP32 3V3 pin, never VIN/5 V until the regulator is identified | Collins + firmware probe | IMU driver, range/ODR/filter config, `conversion` metadata |
| H4 | BioAmp v1.0 (SEP 2021), ×3 boards. Back photographed: **BANDPASS jumper appears open, i.e. stock EEG/EOG band, not EMG**; ELECTRODES jumper appears open (3-electrode). Still needed: continuity-confirm both jumpers; a decision to bridge BANDPASS on **one** board for EMG (keep the other two stock as spares/reference) after re-reading the manufacturer's instructions; supply voltage; output range | Collins (do not modify board until manufacturer instructions checked) | ADC input wiring, `raw_limits`, EMG band validity, raw-vs-envelope assumption |
| H5 | Manufacturer-approved 3.3 V interfacing for that BioAmp revision; wiring/power diagram; body-connection/isolation guidance reviewed | Collins | Any BioAmp connection; any body connection |
| H6 | **Answered from the product listing supplied by Salaar (2026-11-07):** 10 mm × 2.7 mm coin motor, **11000 RPM at 5 V → a rotating ERM** (not an LRA). **Rated 2.5–3.8 V**; runs at 2–5 V; current 40 mA @2 V, 60 mA @3 V, 80 mA @4 V, 100 mA @5 V; 0.9 g. The size matches the photographed motor. That this listing is the purchased part rests on the listing, not an order record | Salaar | — (sets ERM mode and the voltage clamp) |
| H7 | **Answered and measured:** Adafruit DRV2605L breakout wired to board S (VIN→3V3, GND, SDA→D21, SCL→D22, INT open). Read-only probe: DEVICE_ID 7 = DRV2605L, no fault flags, MODE 0x40 = standby (output off). Still open: how many driver boards exist, and the 3V3 rail's headroom for the 60–70 mA motor current | — | — (motor energisation now waits only for the Prompt 5 safety firmware) |
| H8 | Felt/not-felt input: dashboard buttons initially; physical button available? | Fahad / Collins | Station response path |
| H9 | Which computer connects to both boards; available USB **data** cables | Salaar / Collins | Flashing, simultaneous two-board tests |
| H10 | OLED controller model, interface, address; DHT11 module vs bare sensor; Velostat divider resistor and mounting | Collins | Optional/stretch peripherals only |
| H11 | Battery cell and charger models, charge current, protection, power path | Collins | Portable power only (USB power first) |
| H12 | Local mains frequency (50/60 Hz) and the controlled vibration source | Collins | Interference checks, vibration verification |

## Completed checks

| Date | Check | Result | Evidence level |
|---|---|---|---|
| 2026-11-07 | Repository inspection | No existing firmware, backend or station code; only `project-plan/` (teammates' plans) and `signals/` (Salaar's host package) | — |
| 2026-11-07 | Build tools on dev machine | cmake 4.3.2, ninja 1.10.1, gcc 14.3.0, Python 3.13, uv 0.11.14, node 22 present. **Not present:** ESP-IDF / `idf.py`, esptool, PlatformIO, arduino-cli | — |
| 2026-11-07 | Serial devices | No `/dev/ttyUSB*` or `/dev/ttyACM*`; no ESP32 attached. User `salaar` is **not** in group `dialout` (needed for serial access on this Linux machine, or use a udev rule) | — |
| 2026-11-07 | Visual identification from 4 photos (ESP32 board, IMU module front + back, BioAmp v1.0, Arduino Uno) | See *Facts read from photos* | visual only |
| 2026-11-07 | BioAmp EXG Pill v1.0 manufacturer docs read | EMG needs bandpass pads bridged; 3.3 V needs divider; output bias not documented | documentation |
| 2026-11-07 | ESP-IDF v6.1 installed (`install.sh esp32`) | `idf.py --version` → ESP-IDF v6.1; xtensa gcc 15.2.0; esptool 5.6.0 | toolchain |
| 2026-11-07 | Host tests: `cmake -S tests/host -B build/host -G Ninja && cmake --build build/host && ctest --test-dir build/host` | 4/4 pass (logic: 110 checks; one binary per board file). Mutation check: enabling the motor on the provisional board makes `board_esp32_devkit30_provisional` fail with `actuator_unknown`, `driver_unconfirmed`, `pulse_limit` | host-tested |
| 2026-11-07 | `tools/build.sh {wearable,station} {unconfigured,esp32_devkit30_provisional}`, clean builds | All 4 exit 0, no warnings from our sources. App sizes 0x20c20–0x20d90 bytes (87 % of the 1 MB app partition free). Board name verified in each binary | board-compiled (**provisional target**) |
| 2026-11-07 | Source audit | No `gpio_config` / `gpio_set_level` / LEDC (PWM) calls anywhere in firmware sources | static |
| 2026-11-07 | IDF default flash settings in build output | `--flash-size 2MB` (IDF default, **not** the board's actual flash; read with `esptool flash-id` at bring-up) | note |
| 2026-11-07 | Board "W" plugged in: `lsusb` / `udevadm info` | USB-serial bridge **Silicon Labs CP2102** (`10c4:ea60`, driver `cp210x`) at `/dev/ttyUSB0`. Its USB serial number is the generic `0001`, so it can't tell the boards apart; use the ESP32 MAC from `esptool chip-id` instead. Port owned by `root:dialout`; user not yet in `dialout` | measured (host side) |
| 2026-11-07 | `esptool chip-id` ×3 and `flash-id` on board W (`/dev/ttyUSB0`), read-only | **ESP32-D0WD-V3 rev v3.1**, dual core 240 MHz, Vref calibration in eFuse, coding scheme None, **40 MHz** crystal, MAC `28:05:a5:07:55:54`; flash mfr 0x68 / dev 0x4016, **4 MB**, flash voltage **3.3 V set by strapping pin** (GPIO12 must stay low at reset). The first attempt glitched (crystal read 15.55 → 26 MHz, then the stub failed). All three later reads agree on 40 MHz | **measured on hardware** (chip identity only) |
| 2026-11-07 | Board config `esp32_d0wd_v3_devkit30` (chip confirmed; pins proposed, all disabled) plus board `sdkconfig.defaults` setting 4 MB flash; `tools/build.sh` applies board defaults | Host tests 5/5 pass. Both roles build (exit 0; `flash-size 4MB` in the flash args; 0x20db0 / 0x20c80 bytes). Unconfigured builds still 2 MB | host-tested + board-compiled |
| 2026-11-07 | `esptool chip-id` ×2 and `flash-id` on board S (`/dev/ttyUSB0`), read-only | Identical to board W except MAC **`28:05:a5:07:fb:6c`**. USB bridge again CP2102 (`10c4:ea60`). One re-read returned W's MAC: board W had been plugged back in. Detected by MAC and not logged as S | **measured on hardware** (chip identity only) |
| 2026-11-07 | BioAmp back-side photo (one of 3 boards) | BANDPASS ("NARROW") jumper and ELECTRODES ("2E") jumper both appear open; FILTER and GAIN footprints empty. Consistent with the manufacturer's default EEG/EOG, 3-electrode configuration | visual only |
| 2026-11-07 | Prompt 2 host tests (`ctest --test-dir build/host`) | 7/7 pass. `test_telemetry`: 107 checks. `contract_validation`: every record of 7 fake-device scenarios is valid per `signals` (clean, stalled sink, paused serializer, disconnect/nulls, jitter, restart, encoder edge cases at 2^53−1), and the Processor accepts them all | host-tested |
| 2026-11-07 | Loss accounting under a stalled sink (921600 simulated, stall 3–5 s) | Seq gaps = `tx_dropped_records`; acquisition dropped 0 samples; index jumps without `dropped_samples_before` (transport). Paused serializer: ring overrun reported as `dropped_samples_before` + `overrun`, equal to `emg_dropped_samples` | host-tested |
| 2026-11-07 | Mutation check: batcher stops reporting `dropped_samples_before` | Caught by `test_telemetry` and by 3 checks in `contract_validation`; restored | host-tested |
| 2026-11-07 | Wire load (real encoder output incl. framing) | EMG 1 kHz + IMU 100–1000 Hz: 26.0–36.8 kB/s = 28–40 % of 921600; 115200 overloaded (simulated: 471 records dropped, visibly). See `TRANSPORT.md` | host-measured (simulated link) |
| 2026-11-07 | Wearable ESP32 build with fake pipeline, console disabled on UART0 | `tools/build.sh wearable esp32_d0wd_v3_devkit30`: exit 0, app 0x240d0 bytes; `CONFIG_ESP_CONSOLE_NONE`, log level NONE, 4 MB flash in its sdkconfig. All 4 role × board builds exit 0 | board-compiled |
| 2026-11-07 | IMU register documentation | TDK InvenSense RM-MPU-9250A-00 **Rev 1.6** (register map) and PS-MPU-9250A-01 Rev 1.0 (product spec) obtained as original TDK PDFs. WHO_AM_I 0x71 = MPU-9250 (official). The **official MPU-6500 register map could not be retrieved** (TDK links return HTML), so 0x70 = MPU-6500 is from secondary sources only and is reported as RECOGNISED, not SUPPORTED | documentation |
| 2026-11-07 | ESP32 ADC/IO limits | ESP32 datasheet v5.3: ADC effective range at 12 dB = 150–2450 mV (±60 mV calibrated, Table 4-4); VIH max = VDD + 0.3 V (Table 5-3); supply absolute max 3.6 V | documentation |
| 2026-11-07 | Diagnostic firmware `diag/` | `tools/build.sh diag esp32_d0wd_v3_devkit30`: exit 0, app 0x29f50 bytes, no warnings from our sources. Host test `imu` (identity table, register decoding, diag setup → ±4 g, 218.1 Hz, 500 Hz): pass; 8/8 ctests | board-compiled + host-tested |
| 2026-11-07 | Board on `/dev/ttyUSB0` at diagnostic build time | MAC 28:05:a5:07:fb:6c = **board S** (read-only `chip-id`) | measured (identity) |
| 2026-11-07 | Board S flash backup before first flash | `~/vibeguard-flash-backups/backup-boardS-2805a507fb6c.bin`, 4 MB, sha256 `297b35a1…80f84430` | measured |
| 2026-11-07 | **First flash of diag on board S: boot loop** (`Guru Meditation … LoadProhibited`, a crash inside logging) | Cause: stack overflow. Three startup functions each put about 4.8 KB of issue records on the 3.5 KB `app_main` stack, and the **wearable app had the same bug**. Fixed: error counting without buffers, a static report buffer, `CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192` in all 3 apps, and `-Wframe-larger-than=1024` on shared code in host builds | found on hardware, fixed |
| 2026-11-07 | Diag on board S after the fix (USB only; no IMU, no jumpers) | No crash in 16 s. Measured chip model 1, rev 301, 2 cores. Pin check 0/0. **GPIO/ADC table vs IDF driver on the real chip: 0 mismatches.** I²C scan on D21/D22: **0 ACKs** (expected: nothing wired), visible FAIL. ADC: eFuse Vref line-fitting calibration available; GPIO36 and GPIO34 unconnected read **raw 0** (same as GND, so step a alone proves nothing); oneshot throughput **23,919 reads/s**. Log: `~/vibeguard-flash-backups/diag-boardS-2026-11-07.log` | **measured on hardware** |
| 2026-11-07 | Board W flash backup | Full read failed repeatedly ("Packet content transfer stopped"), and region 0x30000 failed every time at 460800. The image was assembled from 64 KB chunks (2 of them at 115200) and **verified with `verify-flash`: digest matched**. `~/vibeguard-flash-backups/backup-boardW-2805a5075554.bin`, sha256 `191faaa1…0ee5a83`. Board W's USB link is less reliable than board S's (see H2) | measured |
| 2026-11-07 | Diag on board W (reported wiring: IMU attached, BioAmp VCC→3V3, GND→GND, OUT→D34; no electrodes) | No crash. Chip model 1, rev 301, 2 cores; pin check 0/0; table vs IDF 0 mismatches. **I²C scan on D21/D22: 0 ACKs. The IMU was not detected** (wiring/power to be checked). BioAmp OUT on D34 with no electrodes: wanders between raw 0 and about 560 (≤ about 0.5 V calibrated) over 16 s; never near mid-supply. No conclusion about the BioAmp's output bias until electrodes or a defined input are present. GPIO36 unconnected: raw 0. Oneshot throughput 23,828 reads/s. Log: `~/vibeguard-flash-backups/diag-boardW-2026-11-07.log` | **measured on hardware** |
| 2026-11-07 | **Wearable IMU on board W** (actual wiring reported by Salaar: VCC→3V3, GND→GND, **SDA→D18, SCL→D19**, AD0 and FSYNC unconnected); board config updated to GPIO18/19 (pin check 0/0) | I²C scan: 1 ACK at **0x68** (AD0 reads low with nothing connected). **WHO_AM_I = 0x70 → MPU-6500** (secondary-source identity; the module is labelled MPU-9250/6500, but the populated chip is not an MPU-9250). Magnetometer not probed. All 7 configuration registers read back exactly as written (±4 g, accel DLPF 218.1 Hz, SMPLRT_DIV 1). **Data-ready events: 500 in 1.000 s** (derived ODR 500 Hz), 0 I²C errors. At rest: mean (−369, 163, −8751) counts, \|mean\| = **1.069 g** at the documented 8192 LSB/g (sd 18–80 counts; 0 identical consecutive samples). Log: `~/vibeguard-flash-backups/diag-boardW-imu-2026-11-07.log` | **measured on hardware** |
| 2026-11-07 | Board W IMU after AD0→GND and FSYNC→GND | Rerun: no crash; 0x68 ACK; WHO_AM_I 0x70; 7/7 register readbacks match; 500 data-ready events/s; 0 I²C errors; \|mean\| = 1.071 g at rest. Unchanged from the floating-pin run. Log: `~/vibeguard-flash-backups/diag-boardW-imu-grounded-2026-11-07.log` | **measured on hardware** |
| 2026-11-07 | Motor and driver photo (front/back) | Adafruit **DRV2605L** I²C haptic driver breakout with a coin vibration motor soldered to the − / + pads, plus a STEMMA QT cable. The "no motor driver" inventory entry is wrong for this part | visual only |
| 2026-11-07 | TI DRV2605L datasheet SLOS854D (rev. March 2018) and Adafruit pinout page read | I²C address 0x5A (7-bit). VDD 2–5.2 V. STATUS 0x00 DEVICE_ID[7:5] = 7 means DRV2605L. **MODE 0x01 STANDBY default 1: the device is in software standby at power-up and after reset, so the motor is off unless firmware writes MODE.** N_ERM_LRA (0x1A bit 7) default 0 = ERM; set before auto-calibration. Wait ≥ 250 µs after power-up before I²C. Adafruit: VIN 3–5 V, SCL/SDA 10 kΩ pull-ups to VIN, INT = trigger/audio input | documentation |
| 2026-11-07 | Motor identification attempts | The "3-5VDC" marking is the **DRV2605L board's supply range** (VLogic/Vcc), not a motor rating. No text visible on the motor's label. Unpowered shake test: no audible rattle, which is **inconclusive** (neither ERM nor LRA masses normally rattle). Type and rating are still unknown. Next: order record, or the 3 V battery test with the motor desoldered from the driver | visual / inconclusive |
| 2026-11-07 | Motor spec (product listing supplied by Salaar) and DRV2605L voltage programming (SLOS854D §8.5.2) | **ERM** (11000 RPM), rated 2.5–3.8 V, ≤ 100 mA at 5 V. DRV2605L: N_ERM_LRA = 0 (ERM, the default). Open-loop ERM full-scale voltage V = 21.59 mV × OD_CLAMP (Eq. 6), so **3.0 V → OD_CLAMP 139 (derived, not yet written or measured)**. The closed-loop clamp (Eq. 8) depends on drive timings; to be worked out in Prompt 5. The output cannot exceed the driver's VDD (datasheet note), so with VIN = 3V3 the motor stays below its 3.8 V rated maximum | documentation / derived |
| 2026-11-07 | **DRV2605L on board S, READ-ONLY probe** (diag now selects the station role from the measured MAC; the code writes nothing to 0x5A) | I²C scan on D21/D22: 1 ACK at **0x5A**. **STATUS = 0xE0 → DEVICE_ID 7 = DRV2605L** (TI SLOS854D Table 4); DIAG_RESULT 0, OVER_TEMP 0, OC_DETECT 0. **MODE = 0x40 → STANDBY = 1, output off** (the documented power-up default). No station IMU (not wired). Motor not driven. Log: `~/vibeguard-flash-backups/diag-boardS-drv2605l-2026-11-07.log` | **measured on hardware** |
| 2026-11-07 | Prompt 5 host tests | 10/10 ctests. `station` (72 checks: completion, abort, duplicates, busy, bounds, 13 malformed variants, oversized, fragmented input, link timeout, driver faults, disabled/init failure, request memory). `station_protocol`: all events from the unit tests plus a real-time `vg-fake-station` session validate (`contract/station_protocol.py`). Mutants (duplicate detection disabled; expiry disabled) caught | host-tested |
| 2026-11-07 | **Station app on board S: UART0 problems found and fixed** | (1) With `CONFIG_ESP_CONSOLE_NONE`, the app was silent: GPIO1/3 were not routed to UART0 → fixed with `uart_set_pin()`. (2) With an ESP-IDF UART **TX ring buffer** on UART0, the host received repeated chunks far beyond the wire rate (e.g. 1.29 MB in 11 s at 115200; reproduced in 2 builds, absent without the ring) → no TX ring; writes happen only in the TX task (frame queue), so the control task never blocks. The same fix is applied to the wearable's shared `vg_uart_tx.c`, whose 921600 path was affected and never run on hardware. Root cause of (2) not investigated further | **found on hardware**, fixed |
| 2026-11-07 | Station app on S, no-motor protocol checks | hello reports DRV2605L / ERM / open_loop_rtp / full_scale_v 3.001, `provenance: live`; level 0 → `level_out_of_range`; 1500 ms → `duration_out_of_range`; non-JSON → `malformed`; missing `duration_ms` → `malformed`; idle abort accepted; status idle, motor off. 11/11 events valid. Saved: `~/vibeguard-flash-backups/station-nomotor-test-2026-11-07.ndjson` | **measured on hardware** |
| 2026-11-07 | **First motor pulses on S** (approved by Salaar; ERM, 3.0 V full scale) | level 30/200 ms → on_ms **200.001**; level 60/500 ms → **500.001**; level 60/800 ms aborted at **147.0 ms** (`stimulus_ended` on the same 1 ms tick as the abort ack); duplicate during a 600 ms pulse → `duplicate`/`in_progress`, pulse not extended (**600.001**); concurrent stimulus → `busy`; final status idle, motor off, 0 faults. 20/20 events valid. on_ms is the device-clock time between commanding the driver on and off; the electrical/mechanical stop is not instrumented (needs the station IMU). **Observed by the team: the motor buzzed for each of the 4 pulses and stopped each time.** Saved: `~/vibeguard-flash-backups/station-motor-test-2026-11-07.ndjson` | **measured on hardware** (command timing) |
| 2026-11-07 | Board W replaced Collins's Arduino sketch (`~/Downloads/haven_sleeve.ino`; it posted 1 Hz on-device summaries to `http://10.84.43.16/api/sleeve`, port 80, where nothing was listening → TCP resets). Diagnosed with tcpdump; firewall inactive; hotspot allows client-to-client (ping OK) | measured |
| 2026-11-07 | **Live wearable, first version** (EMG oneshot reads + IMU polled in one 1 kHz loop) over Wi-Fi | Rates were right (EMG 1000.0/s, IMU 498.7/s by sample index over 73 s), but per-sample timing jitter (EMG steps 0.5–1.5 ms) cut batches to mostly **1 row** → ~590 records/s → TX queue drops (8591 records) and IMU ring overruns | **found on hardware** |
| 2026-11-07 | **Fix: hardware-paced EMG + reconstructed IMU timing** | EMG: ADC1 continuous (DMA) at 20 kHz, mean of 20 → 1 kHz, timestamps from the conversion count, drift checked against esp_timer (measured offset −313…−317 µs, 0 re-anchors). IMU: data-ready polled at 1 kHz, timestamps from a phase/frequency-locked estimator of the IMU clock (`shared/include/vg/clock_est.h`, host-tested). Result on board W: **EMG 50-row and IMU 25-row uniform batches, ~41 records/s, ~32 kB/s, 0 sample drops, 0 IMU resyncs, 0 invalid records (80k+)** | **measured on hardware** |
| 2026-11-07 | `vg-bridge` (signals) live with board W | UDP discovery finds the laptop (no fixed IP); TCP 8765 ingest; Processor (host clock, poll 50 ms); vibration `ok` 0.046 m/s² at rest (noise floor), static_mean z −4357 counts ≈ 1.06 g; EMG envelope ~700 counts `degraded` (clipped) with no electrodes; activation uncalibrated; score paused. Raw recording byte-identical to the stream in the host self-test | **measured on hardware** |
| 2026-11-07 | IMU init robustness | One boot ran with the IMU not initialised (`imu_fresh=0/0`); the firmware now keeps the IMU stream declared, reports `disconnected` and retries init every 1 s (and re-initialises after sustained I2C failures) | found on hardware, fixed |
| 2026-11-07 | **Tool-use detection, live on board W** (`signals/processing/tool_use.py`; phone vibration motor pressed on the IMU as a tool stand-in) | Still: `no_tool`, tool band 0.05–0.1 m/s². Taps/knocks: tool band ≤ 0.9, 0 candidates. Vigorous arm movement: arm band up to 6.5 m/s², tool band ≤ 0.74, 0 candidates → `no_tool`. Phone vibrating: tool band 3–4 m/s² at **152–160 Hz**, arm ~0.3 → **`tool`**, 10.3 s tool time, 1 bout; back to `no_tool` ~3 s after removal | **measured on hardware** (tool stand-in, not a real power tool) |
| 2026-11-07 | Wi-Fi stability on the phone hotspot | Before: host stale limit 0.25 s tripped on normal 250–500 ms hotspot pauses (69 stale resets in ~4 min → 1–2 s `unknown` flicker) and the 16 KiB TX queue dropped 610 records. After (bridge stale 1.0 s, 64 KiB heap frame queue, TCP window 16 KiB): **126 s idle: 0 dropped records, 0 dropped samples, 1 stale event; 98.5 % of tool_use records valid** | **measured on hardware** |
| 2026-11-07 | Laptop network | NetworkManager kept switching the laptop from hotspot "Salaar" back to eduroam (laptop IP 172.28.x), cutting the device off; fixed by disabling eduroam auto-connect | operational note |
| 2026-11-07 | Host signal package baseline | `signals/`: 318 tests passing at commit `241758c` | host-tested (synthetic) |

## Pending from Prompt 1

- **On-device checks, which run at boot once flashed:**
  - measured `esp_chip_info()`;
  - the GPIO/ADC table cross-check against the IDF driver;
  - the boot log reporting "motor output: OFF".
- **Board ↔ port mapping when both are plugged in.** Both enumerate as CP2102 `/dev/ttyUSB*` with the same generic serial number, so check the MAC with `esptool chip-id` before flashing. W = 28:05:a5:07:55:54, S = 28:05:a5:07:fb:6c.

## Pending hardware steps (in order)

0. (Done 2026-11-07) Board S backed up and flashed with diag; bare-board results recorded above.
1. **Wire one IMU** to the board under test, exactly as in `WIRING.md`, including the
   pre-power checks. Measure and record the module regulator output voltage.
2. **User confirms** the board and port mapping (MAC via `chip-id`) and approves
   flashing. Back up the existing flash first (`read-flash`, 4 MB).
3. **Flash `diag` and record the actual VGD lines here:**
   - I²C scan;
   - WHO_AM_I and the identity status;
   - register readback;
   - the measured data-ready rate;
   - the at-rest accel mean (≈1 g?);
   - ADC steps a–d on VP and D34;
   - ADC oneshot throughput.
4. **Prompt 2 hardware check:** flash `wearable` (fake telemetry) and capture UART0 at
   921600 into `vg-validate` / `vg-process`, recording throughput, errors, and whether
   opening the port resets the board.
5. **Repeat 1–3 on the other board.**

Not done, and not to be done in this step: BioAmp connection (H4/H5) and motor (H6/H7).

## Next executable step

Steps 1–3 above. Until they run, the diagnostic is compiled only, and **no IMU or ADC
observation exists**.
