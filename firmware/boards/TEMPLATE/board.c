/* TEMPLATE: copy to boards/<new_name>/board.c and fill in from CONFIRMED facts.
 *
 * For every value, record where it came from (esptool output, module marking +
 * datasheet, measured wiring) in firmware/HANDOFF.md. Leave anything unknown as
 * VG_PIN_NONE / VG_FACT_UNKNOWN / disabled. Run the host tests and
 * `tools/build.sh <role> <new_name>`: the startup report lists pin-check errors.
 *
 * Motor: enabled = true requires actuator type, a confirmed driver circuit and
 * max_pulse_ms (<= VG_MOTOR_MAX_PULSE_CEILING_MS). Never wire a motor to a GPIO.
 */
#include "vg/hw_config.h"

const vg_hw_config_t vg_board_wearable = {
    .board_name = "TEMPLATE",
    .description = "describe the physical board and where facts came from",
    .role = VG_ROLE_WEARABLE,
    .chip = VG_CHIP_UNKNOWN,                 /* VG_CHIP_ESP32 once read by esptool */
    .chip_status = VG_FACT_UNKNOWN,
    .chip_evidence = "e.g. esptool chip_id output, date",
    .gpio16_17_free_status = VG_FACT_UNKNOWN,
    .emg = {.enabled = false, .gpio = VG_PIN_NONE, .atten = VG_ATTEN_UNSET, .bitwidth = 0},
    .imu = {.enabled = false, .sda_gpio = VG_PIN_NONE, .scl_gpio = VG_PIN_NONE,
            .address = 0x68, .clock_hz = 100000},
    .motor = {.enabled = false, .gpio = VG_PIN_NONE},
    .velostat = {.enabled = false, .gpio = VG_PIN_NONE},
    .oled = {.enabled = false},
    .dht11 = {.enabled = false, .gpio = VG_PIN_NONE},
};

const vg_hw_config_t vg_board_station = {
    .board_name = "TEMPLATE",
    .description = "describe the physical board and where facts came from",
    .role = VG_ROLE_STATION,
    .chip = VG_CHIP_UNKNOWN,
    .chip_status = VG_FACT_UNKNOWN,
    .chip_evidence = "",
    .gpio16_17_free_status = VG_FACT_UNKNOWN,
    .emg = {.enabled = false, .gpio = VG_PIN_NONE},
    .imu = {.enabled = false, .sda_gpio = VG_PIN_NONE, .scl_gpio = VG_PIN_NONE,
            .address = 0x68, .clock_hz = 100000},
    .motor = {.enabled = false, .gpio = VG_PIN_NONE, .active_high = true,
              .actuator = VG_ACTUATOR_UNKNOWN, .driver_confirmed = false,
              .max_pulse_ms = 0},
    .velostat = {.enabled = false, .gpio = VG_PIN_NONE, .atten = VG_ATTEN_UNSET},
    .oled = {.enabled = false, .address = 0x3c},
    .dht11 = {.enabled = false, .gpio = VG_PIN_NONE},
};
