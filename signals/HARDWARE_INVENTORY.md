# Hardware inventory and integration decisions

Updated 2026-11-07 from the team's supplied inventory. Counts below are reported availability, not physical inspection.

| Part | Available | Planned use / remaining fact |
|---|---|---|
| BioAmp EXG Pill kit + extra gel electrodes | 1 | Wearable, one analog EMG channel; confirm board revision/filter configuration |
| Arduino kit | 1 | Optional backup/parts source; ESP32 selected for both controllers |
| ESP32 V2 | 2 | One wearable controller and one station controller; exact module/dev-board variant unknown |
| IMU modules marked MPU-9250/6500 | 2 confirmed | One wearable, one station; verify actual chip identity and configured range/rate during bring-up |
| Vibrating disk motor | 2 | One station actuator plus spare; motor type, rated voltage/current unknown |
| Motor driver board | 0 | Missing; obtain a compatible driver or verify suitable discrete drive components |
| OLED 128×64 | 1 | Optional station prompts; controller/interface/address unknown |
| DHT11 | 1 | Optional ambient-temperature context |
| Sound sensor module | 1 | Spare; outside MVP |
| NeoPixel | 1 | Optional status indicator after core acquisition works |
| Velostat | 1 | Station contact-pressure proxy; needs circuit and repeatability check |
| Conductive rubber sheet | 1 | Spare; gel electrodes remain primary |
| Conductive thread, thin + medium | Available, count unspecified | Spare |
| Nylon fabric square; sewing needles | Available | Existing tattoo sleeve already supplied; no fabrication task |
| Silicone wearables wire | Available | Cable routing/strain relief |
| LiPo 3.7 V 110 mAh or 18650 | One alternative, unresolved | Verify actual cell, protection, permitted load and charging current |
| Type-C LiPo charger board | 2 | Exact model/charge setting/power-path capability unknown |
| FLORA + BlueFruit Flora module | Backup | Use only if primary boards fail |
| DS1302 RTC | Backup | Device monotonic timestamps and host wall time suffice initially |

## Recommended allocation

Use ESP32 #1 for BioAmp analog acquisition and wearable IMU #1 on a common controller clock. Use ESP32 #2 for station motor control, Velostat, IMU #2 and optional OLED/DHT11. Arduino hardware is not required for this allocation; keep its kit for spare parts. The Arduino software framework can still be used to program an ESP32; this does not require an Arduino board.

Before connecting BioAmp OUT, confirm its actual output range and implement the manufacturer's 3.3 V interfacing guidance. ESP32 ADC attenuation sets measurable range; it is not a substitute for appropriate external voltage conditioning. Confirm the ESP32 variant before assigning pins.

Two IMUs remove the need to move one between assemblies. A module label containing both MPU-9250 and MPU-6500 does not confirm which chip is populated. Detect identity and configure the matching driver before choosing acceleration scale or sample rate. Station acceleration measurement still needs repeatable mounting and adequate sensor bandwidth.

## Verified component facts and immediate blockers

The [manufacturer's BioAmp EXG Pill documentation](https://docs.upsidedownlabs.tech/hardware/bioamp/bioamp-exg-pill/index.html) describes a one-channel analog front end with configurable gain/filtering, rather than a digital sensor that chooses ADC counts or sample rate. The controller determines digitization. Its default configuration is described for EEG/EOG; inspect the supplied revision/configuration for EMG use. Follow its documented 3.3 V versus 5 V interface requirements; do not directly assume a 5 V output fits an ESP32 analog input.

A missing motor driver does not require a dedicated breakout if suitable discrete parts are available. For a confirmed brushed DC eccentric motor, a rated transistor/MOSFET, appropriate resistors, flyback diode and suitable supply can provide drive. [Adafruit's transistor motor guide](https://learn.adafruit.com/adafruit-arduino-lesson-13-dc-motors/transistors) explains this arrangement. Confirm the motor is that type and its load ratings before selecting parts. A linear resonant actuator needs a different drive strategy. Do not power a bare motor from a GPIO pin. Driver availability blocks physical stimulation, not station UI/state-machine work.

The small cell's capacity does not establish usable runtime or safe charging current. Confirm cell and charger specifications before portable assembly; a charger board is not automatically a regulated board supply or power-path controller. Body-connected BioAmp use must follow the manufacturer's power/isolation guidance.

## Impact on the mock data

One emg0 channel matches the BioAmp channel count. Raw analog processing is appropriate in principle, but real analog filtering and bandwidth must be checked. Current 0–4095 counts are synthetic controller assumptions. The ESP32 selection makes a 12-bit 0–4095 acquisition configuration plausible, but confirm variant, ADC width, attenuation and calibration in firmware. The [Espressif ADC guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/adc/index.html) describes the range and calibration dependencies. A profile change must scale noise/amplitude and calibration separation settings, not merely clip the old values.

“9-DOF” does not determine acceleration scale, sensor output rate, bandwidth or range. Keep 4096 counts/g and the mock sample rates labeled synthetic until the IMU model/configuration is confirmed. The waveform remains a surrogate, not an actual recorded tool signal.

DHT11, Velostat, station responses, OLED and NeoPixel are not included in Salaar's EMG/IMU telemetry implementation. Agree optional temperature and station fields with the relevant owners before extending the strict contract. Do not insert invented channels into existing batches.

## Next information from Collins

1. ESP32 module/development-board markings and planned ADC input configuration.
2. Actual chip identity of both MPU-9250/6500-marked modules, selected range and output rate.
3. BioAmp revision and current filter configuration.
4. Motor type/rating; transistor/MOSFET, diode and resistors available in the kit.
5. Actual battery and charger model/charge setting.

Then configure ADC limits, conversion metadata, independent sample rates, measured timing and transport budget; capture real relaxed/squeeze/stationary/vibrating samples; compare them with the mock before claiming hardware fidelity.
