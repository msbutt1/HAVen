// Board-specific values for the station controller. EVERY VALUE BELOW IS UNSET ON PURPOSE.
//
// Collins owns the wiring: set these from the real wiring and the real motor driver. Nothing here
// is guessed; the sketch will not compile until they are defined (see HARDWARE.md).

#ifndef VG_STATION_CONFIG_H
#define VG_STATION_CONFIG_H

// GPIO connected to the motor driver's input (PWM). Never drive the disk motor straight from a GPIO.
// #define PIN_MOTOR 0

// GPIO for each pushbutton. Wire the other side to GND; the sketch uses INPUT_PULLUP.
// #define PIN_BTN_FELT 0
// #define PIN_BTN_NOT_FELT 0

// Motor setting range reported to the host in `hello` (settings are PWM duty percent).
// 0 is reserved for "motor off". Lower the max if the motor/driver should not run at 100 %.
#define VG_SETTING_MIN 1
#define VG_SETTING_MAX 100

#define VG_FW_NAME "vibeguard-station-ref"
#define VG_FW_VERSION "0.1"

#define VG_BAUD 115200
#define VG_DEBOUNCE_MS 30

#endif
