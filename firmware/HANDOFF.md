# VibeGuard firmware — handoff and hardware-facts log

**Owner:** Salaar (production firmware). Wiring and throwaway bring-up sketches: Collins.
Station test sequencing: Fahad (host). Ingestion/APIs: Abduljabbar. Host algorithms
(EMG filtering, calibration, vibration features, scoring) stay in `signals/` and are not
re-implemented on the controller.

**Status (2026-11-07): Prompt 2 complete: wearable telemetry encoder and transport, on fake (simulated) acquisition.** Details: `TRANSPORT.md`.

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
| H2 | USB-serial path: **CP2102 UART bridge on both boards (measured)**; max reliable baud still to measure | Collins / measured | Transport choice (NDJSON vs binary), host port mapping |
| H3 | Populated IMU chip on each module (read WHO_AM_I on-device); pin labels **now known** (both modules same GY-9250/6500 design); regulator part: SOT-23 marking reported as **"LB2K"** (2026-11-07), not identified by web search; VCC input range therefore still unconfirmed (seller listings for GY-9250 boards claim 3–5 V via on-board LDO — not trusted). Measure the regulator output with VCC=3V3 before relying on it. **Proposed (Salaar to confirm):** I²C, AD0→GND (0x68 per TDK docs, verify in Prompt 3), each IMU alone on its own ESP32's bus, VCC from the ESP32 3V3 pin, never VIN/5 V until the regulator is identified | Collins + firmware probe | IMU driver, range/ODR/filter config, `conversion` metadata |
| H4 | BioAmp v1.0 (SEP 2021), ×3 boards. Back photographed: **BANDPASS jumper appears open, i.e. stock EEG/EOG band, not EMG**; ELECTRODES jumper appears open (3-electrode). Still needed: continuity-confirm both jumpers; a decision to bridge BANDPASS on **one** board for EMG (keep the other two stock as spares/reference) after re-reading the manufacturer's instructions; supply voltage; output range | Collins (do not modify board until manufacturer instructions checked) | ADC input wiring, `raw_limits`, EMG band validity, raw-vs-envelope assumption |
| H5 | Manufacturer-approved 3.3 V interfacing for that BioAmp revision; wiring/power diagram; body-connection/isolation guidance reviewed | Collins | Any BioAmp connection; any body connection |
| H6 | Motor part/label, type (brushed ERM vs LRA), rated voltage/current | Collins | Driver design, any motor energisation |
| H7 | Driver board part, or transistor/MOSFET + flyback diode + resistor markings; motor supply; circuit diagram | Collins | Real motor output (stays disabled) |
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
| 2026-11-07 | Host signal package baseline | `signals/`: 318 tests passing at commit `241758c` | host-tested (synthetic) |

## Pending from Prompt 1

- **On-device checks, which run at boot once flashed:**
  - measured `esp_chip_info()`;
  - the GPIO/ADC table cross-check against the IDF driver;
  - the boot log reporting "motor output: OFF".
- **Board ↔ port mapping when both are plugged in.** Both enumerate as CP2102 `/dev/ttyUSB*` with the same generic serial number, so check the MAC with `esptool chip-id` before flashing. W = 28:05:a5:07:55:54, S = 28:05:a5:07:fb:6c.

## Pending from Prompt 2 (hardware)

- **Flash the fake telemetry build to board W** and capture UART0 at 921600 into
  `vg-validate` / `vg-process`. This gives a real throughput and error rate, real
  `esp_timer` jitter, the boot ROM banner behaviour, and whether opening the port resets
  the board.
- Confirm that 921600 is reliable on these boards and cables. If it is not, fall back to
  460800 and then to binary framing (`TRANSPORT.md`).

## Next executable step

**Prompt 3: diagnostic bring-up.** IMU WHO_AM_I and register readback, a BioAmp-free
ADC check, and a wiring table. It needs board W flashed, plus the IMU wired as proposed
(VCC→3V3, GND, SDA→D21, SCL→D22, AD0→GND). The pending Prompt 2 UART capture above can be
done in the same session.
