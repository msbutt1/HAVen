# Bench wiring for the diagnostic (Prompt 3)

Applies to **both** ESP32 boards. They are identical: ESP32-D0WD-V3 rev v3.1, 30-pin
dev board, CP2102 USB bridge. Board W is MAC `28:05:a5:07:55:54`; board S is MAC
`28:05:a5:07:fb:6c`. Pin names below are the **silkscreen labels** on the photographed
boards and modules.

**Power comes from USB only.** Connect only the IMU and the ADC test jumpers described
here. **No BioAmp. No motor. No battery.**

> **Board W as actually wired (2026-11-07):** VCC→3V3, GND→GND, **SDA→D18, SCL→D19**,
> **AD0→GND, FSYNC→GND**. The diagnostic found the MPU-6500 at 0x68 and passed with the
> pins floating and again after grounding. The board config uses GPIO18/19 for the
> wearable.
> The table below is the original proposal; the station keeps D21/D22.

## IMU module (GY-9250 / "MPU-9250/6500" board) → ESP32

| IMU pin | Connect to (ESP32 label) | Why | Source |
|---|---|---|---|
| VCC | **3V3** (right header, next to USB-C) | 3.3 V is inside the MPU VDD range of 2.4–3.6 V whether or not the module's regulator ("LB2K", unidentified) drops it. **Never VIN** (5 V) | PS-MPU-9250A-01 §2.4 |
| GND | **GND** (right header, above 3V3) | Common reference | — |
| SCL | **D22** (GPIO22) | I²C clock. Output-capable; not strapping, flash or UART | ESP-IDF v6.1 ESP32 GPIO page |
| SDA | **D21** (GPIO21) | I²C data | Same |
| AD0 | **GND** | Sets I²C address 0x68 (0b1101000) | PS-MPU-9250A-01, I²C address table |
| FSYNC | **GND** | "Frame synchronization digital input. Connect to GND if unused." | PS-MPU-9250A-01 pin table |
| NCS | **Unconnected** for the first test | "Chip select (SPI mode only)". The module may already pull it up. If the scan finds nothing with otherwise correct wiring, measure NCS with a multimeter before changing anything | PS-MPU-9250A-01 pin table |
| EDA, ECL | Unconnected | The IMU's auxiliary I²C master for external sensors; not used | PS-MPU-9250A-01 pin table |
| INT | Unconnected | The diagnostic polls INT_STATUS instead | — |

I²C pull-ups: the module's own pull-ups are unknown. The diagnostic enables the ESP32's
weak internal pull-ups as a fallback and runs the bus at 100 kHz (the part allows 400 kHz).

### Before powering (Collins)

1. With USB unplugged, check continuity: **no short between 3V3 and GND** on the
   breadboard.
2. Check that VCC goes to **3V3**, not VIN, and that VCC and GND are not swapped at the
   module.
3. Plug in USB. Measure the module's regulator output (the "LB2K" part's output leg)
   against GND. Expect roughly 3.0–3.3 V. Record the value in `HANDOFF.md`.

## ADC check without the BioAmp (known safe levels only)

The only allowed inputs are this board's **own GND and 3V3 pins**. 3.3 V is within the
ESP32 input limit (VIH max = VDD + 0.3 V, ESP32 datasheet v5.3, Table 5-3). Nothing
external.

| Step | Jumper | Pin under test | Expected diagnostic line |
|---|---|---|---|
| a | GND → **VP** | GPIO36 (proposed EMG input) | `reads ~0` (raw ≈ 0). Indistinguishable from unconnected (step d) |
| b | 3V3 → **VP** | GPIO36 | `SATURATED` (raw 4095): above the 12 dB effective range |
| c (optional) | Two **equal** resistors (e.g. 10 kΩ + 10 kΩ) from 3V3 to GND, midpoint → VP | GPIO36 | About 1650 mV. Compare it with a multimeter reading at the midpoint, since resistor tolerance and the actual 3V3 level shift it |
| d | Nothing connected | GPIO36 | **Measured on board S: raw 0, same as GND.** So step a alone proves nothing; steps b and c are the informative ones |
| a–d again | Same, on **D34** | GPIO34 (proposed Velostat input) | Same as above |

The ESP32 ADC's datasheet effective range at 12 dB attenuation is **150–2450 mV**,
±60 mV after eFuse calibration (Table 4-4). Steps a and b sit outside it on purpose:
they only prove the pin and channel mapping. Step c tests accuracy.

## What the diagnostic does NOT touch

- **Motor pin (GPIO25):** never configured; the build contains no GPIO output code.
- **BioAmp:** must not be connected. Its 3.3 V interfacing is still open (H5), and the
  board is still in its EEG/EOG jumper setting (H4).
- **Magnetometer:** not probed and not assumed. Acceleration is the only signal needed.

## Board and port mapping

Both boards appear as a CP2102 `/dev/ttyUSB*` port with the same USB serial number. The
MAC decides which board is which:

```sh
python -m esptool --port /dev/ttyUSB0 chip-id      # after . ~/esp/esp-idf-v6.1/export.sh
```

The diagnostic also prints the MAC as its first result line.

## Flash and read the diagnostic

```sh
cd firmware && . ~/esp/esp-idf-v6.1/export.sh
python -m esptool --port /dev/ttyUSB0 read-flash 0 0x400000 backup-<mac>.bin   # keep the original firmware
tools/build.sh diag esp32_d0wd_v3_devkit30 -p /dev/ttyUSB0 flash
tools/build.sh diag esp32_d0wd_v3_devkit30 -p /dev/ttyUSB0 monitor        # Ctrl+] to exit
```
