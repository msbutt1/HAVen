# Station hardware: connect-and-go guide

**For Collins (wiring) and Salaar (firmware); Fahad runs the software side.**
Everything on the laptop side is built and tested against simulation. This page is what is left
when the hardware is ready. Nothing here has run against real hardware yet.

## What the station is made of (from the team inventory)

| Part | Use | Notes |
|---|---|---|
| ESP32 V2 (we have 2) | **station controller** | Use one for the station and the other for the wearable, so the two devices are independent (no mode switching) |
| Vibrating disk motor (we have 2) | stimulus under the fingertip | Needs a driver stage (see below) |
| Pushbuttons (Arduino kit) | two buttons: FELT and NOT FELT | ESP32 logic is 3.3 V |
| USB cable to the laptop | power + serial link | no battery needed for the station |
| OLED 128×64, Velostat | optional | not used by the current software; contact pressure is reported as uncontrolled |
| MPU-9250/6500 IMU | stays on the **wearable** | not part of the station |

**Open item for Collins: the motor driver.** The inventory lists the disk motors but no named driver
board. A GPIO pin must never drive a motor directly. Use the vibrating-motor driver board, or a
transistor plus flyback diode, sized from the motor's datasheet. Confirm the supply, the maximum
safe duty cycle and whether the driver wants PWM; then set `VG_SETTING_MAX` (below).

## Wiring (Collins fills in the pins)

1. Motor driver input ← one ESP32 PWM-capable GPIO (`PIN_MOTOR`); driver supply and GND per its datasheet; common GND with the ESP32.
2. FELT button between one GPIO (`PIN_BTN_FELT`) and GND. NOT FELT button between another GPIO (`PIN_BTN_NOT_FELT`) and GND. The sketch uses the internal pull-ups, so no resistors are needed.
3. Mount the disk motor so the fingertip rests on it the same way every time (a fixed pad or a recess). Fingertip contact pressure is not measured, so keep it consistent and say so.
4. Put the two buttons where the person can press them with the other hand without moving the tested finger.

## Firmware (Salaar)

Two options; either way the host software is unchanged.

- **Use the reference sketch** in `firmware/station_controller/`: edit `station_config.h` (pins and setting range; it does not compile until the pins are set), then build and flash with the Arduino IDE or PlatformIO for an ESP32 board. **It has never been compiled for the ESP32 or run on hardware**; only `station_core.h` (the protocol logic) is tested, on a PC.
- **Or write your own** against the line protocol in `contract/STATION_CONTRACT.md` ("Controller link"). It is small: `hello`, `deliver_stimulus`, `stop_stimulus` in; `hello`, `ack`, `stimulus_started`, `stimulus_ended`, `response` out.

Values that must come from the real hardware, not from a guess:

| Value | Where | Why it matters |
|---|---|---|
| `PIN_MOTOR`, `PIN_BTN_FELT`, `PIN_BTN_NOT_FELT` | `station_config.h` | wiring |
| PWM frequency | `kPwmFreqHz` in the `.ino` (200 Hz is a placeholder) | disk motors have a sensible drive range; check the datasheet |
| `VG_SETTING_MIN` / `VG_SETTING_MAX` | `station_config.h` | the range the software uses is **whatever the controller reports in `hello`**; set the max to the highest safe duty |
| Baud rate | `VG_BAUD` (115200) | must match `--baud` on the laptop; the station's traffic is tiny, 115200 is plenty |

## Bring-up on the laptop (about 5 minutes)

Run from `station/`. The COM port is whatever `ports` shows for the ESP32 (COM3 and COM4 on Fahad's laptop are Bluetooth, not the ESP32).

```sh
uv run vg-station ports
uv run vg-station selftest --port COM5
uv run vg-station live --port COM5 --mode preview
uv run vg-station live --port COM5 --mode full
```

`selftest` checks, in order: the controller answers `hello` (and reports its setting range), a stop is
confirmed, the motor accepts low / medium / high pulses (**you confirm by touch that you feel them**; the
program cannot), and both buttons are seen (it reports swapped buttons). Fix anything that says FAIL
before running `live`. `live` runs the real test, prints each trial, saves the result in `results/`, and
Ctrl+C aborts (stopping the motor first).

To rehearse the whole thing with no hardware: `uv run vg-station live --simulate --mode preview`
(real-time, about 10 s, labelled simulated).

## If something fails

| Symptom | Likely cause |
|---|---|
| `controller did not answer hello` | wrong COM port, wrong baud, firmware not flashed, USB cable is charge-only, or the board needs the boot/reset button |
| `setting N is outside the controller's range` | the controller's `hello` range is narrower than expected; the software follows the controller |
| `controller restarted during the session` | brownout or reset (motor supply sagging the ESP32): give the motor its own supply or a bulk capacitor |
| FELT/NOT FELT swapped in selftest | swap the two pins in `station_config.h` |
| motor does nothing | driver wiring or supply; check `PIN_MOTOR` and that the setting range reaches a usable duty |
| session ends `error` right after the first trial | the controller rejected the command (read the `error` line printed), or the link dropped |

## Pitch honesty (unchanged)

The motor setting is a driver command, not a measured vibration amplitude. Fingertip pressure is
uncontrolled. This is a vibration-perception early-warning prototype, not a diagnosis.
