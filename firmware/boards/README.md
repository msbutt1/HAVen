# Board configurations

A board directory holds **facts only**: chip, pins, which peripherals are enabled. Select
one with `tools/build.sh <role> <board>`. To add a board, copy `TEMPLATE/board.c`. Each
fact carries a status: `unknown`, `provisional` (photo or seller label) or `confirmed`
(read on the device, or from its own marking plus datasheet).

| Board | Chip status | Peripherals enabled | Use |
|---|---|---|---|
| `unconfigured` | unknown | none | Default. Fake acquisition only; motor output impossible |
| `esp32_devkit30_provisional` | ESP32, **provisional** (photo of 30-pin board) | none | Compile-checks the proposed pins. Not a confirmed board |
| `esp32_d0wd_v3_devkit30` | ESP32-D0WD-V3 rev v3.1, **confirmed** on board W (MAC 28:05:a5:07:55:54) and board S (MAC 28:05:a5:07:fb:6c); 4 MB flash via `sdkconfig.defaults` | none | **Use this.** Pins as below, all still proposed |
| `TEMPLATE` | unknown | none | Copy for a confirmed board |

## Proposed pins: `esp32_d0wd_v3_devkit30` (same as the provisional board)

None of these are wired or verified. The rules come from the ESP-IDF v6.1 ESP32 GPIO
docs; the same table is in `shared/src/chip_caps.c` and is cross-checked against the IDF
driver at boot.

| Function | Role | GPIO (board label) | Why this pin | Enabled? | Blocked by |
|---|---|---|---|---|---|
| EMG (BioAmp OUT) | wearable | 36 (VP) | ADC1_CH0. Input-only is fine for analog. Not strapping, flash or UART | no | BioAmp 3.3 V interfacing, EMG jumper state, attenuation choice (H4/H5) |
| IMU SDA / SCL | both | 21 / 22 (D21 / D22) | Output-capable, not strapping, flash or UART. Matches the I²C wiring proposed to Collins | no | Wiring confirmed; AD0→GND for 0x68; identity read on the device (H3) |
| Motor driver input | station | 25 (D25) | Output-capable, not strapping, flash or UART | **no** | Motor type and ratings, driver circuit (H6/H7) |
| Velostat | station | 34 (D34) | ADC1_CH6, input-only | no | Divider circuit and attenuation (H10) |
| OLED | station | (IMU I²C bus) | Shares the bus | no | Controller and address (H10) |
| DHT11 | station | none | — | no | Module type (H10) |

**Pins to avoid** on this chip (the pin check flags each one):
- GPIO6–11: flash.
- GPIO1/3: UART0, used for flashing and telemetry.
- GPIO0/2/5/12/15: strapping. An error for actuator output, a warning otherwise.
  Board W's flash voltage is strapped to 3.3 V, so nothing may pull GPIO12 high at reset.
- GPIO34–39: input-only. An error for I²C, DHT and motor.
- GPIO16/17: PSRAM on some modules, until it is confirmed absent.
- ADC2 pins (0, 2, 4, 12–15, 25–27) for analog input: unusable with Wi-Fi. Warning.
