/* Default board: nothing is known. Every peripheral disabled, no pins assigned,
 * motor output impossible. Builds run fake (SIMULATED) acquisition only. */
#include "vg/hw_config.h"

#define UNCONFIGURED_COMMON                                                   \
    .chip = VG_CHIP_UNKNOWN, .chip_status = VG_FACT_UNKNOWN,                  \
    .chip_evidence = "none", .gpio16_17_free_status = VG_FACT_UNKNOWN,        \
    .emg = {.enabled = false, .gpio = VG_PIN_NONE},                           \
    .imu = {.enabled = false, .sda_gpio = VG_PIN_NONE, .scl_gpio = VG_PIN_NONE}, \
    .motor = {.enabled = false, .gpio = VG_PIN_NONE},                         \
    .velostat = {.enabled = false, .gpio = VG_PIN_NONE},                      \
    .oled = {.enabled = false}, .dht11 = {.enabled = false, .gpio = VG_PIN_NONE}

const vg_hw_config_t vg_board_wearable = {
    .board_name = "unconfigured",
    .description = "no hardware facts; fake acquisition only",
    .role = VG_ROLE_WEARABLE,
    UNCONFIGURED_COMMON,
};

const vg_hw_config_t vg_board_station = {
    .board_name = "unconfigured",
    .description = "no hardware facts; fake acquisition only; motor output impossible",
    .role = VG_ROLE_STATION,
    UNCONFIGURED_COMMON,
};
