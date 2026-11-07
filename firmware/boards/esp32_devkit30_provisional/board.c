/* PROVISIONAL: 30-pin ESP32 dev board photographed 2026-11-07 (module shield
 * "ESP-32", code XX5R69, USB-C, EN/BOOT buttons). Chip family inferred from
 * the broken-out pins only: NOT read with esptool. Second board assumed
 * identical: NOT confirmed. Pins below are PROPOSED; nothing is wired to them
 * yet, and every peripheral that depends on an unresolved fact stays disabled.
 *
 * Pin rationale (ESP-IDF v6.1 ESP32 GPIO docs, see chip_caps.c):
 *  - EMG     GPIO36 (VP): ADC1_CH0, input-only (fine for analog in), not strapping.
 *            Disabled until BioAmp 3.3 V interfacing + EMG jumper state are confirmed.
 *  - IMU     SDA GPIO21 / SCL GPIO22: not strapping, not flash/UART, output-capable.
 *            Address 0x68 assumes AD0 tied to GND (proposed wiring); identity is read
 *            on-device in Prompt 3. 100 kHz until the bus is verified.
 *            Disabled until the module is wired as proposed.
 *  - Motor   GPIO25: output-capable, not strapping/flash/UART. DISABLED: motor type,
 *            ratings and driver are unknown; no driver exists yet.
 *  - Velostat GPIO34: ADC1_CH6, input-only. Disabled: divider circuit unknown.
 *  - OLED / DHT11: disabled (optional; controller/address and module type unknown).
 */
#include "vg/hw_config.h"

#define PROVISIONAL_CHIP                                                       \
    .chip = VG_CHIP_ESP32, .chip_status = VG_FACT_PROVISIONAL,                 \
    .chip_evidence = "photo of 30-pin board (VP/VN, D34/D35, TX2/RX2 broken out)", \
    .gpio16_17_free_status = VG_FACT_UNKNOWN

#define PROPOSED_IMU                                                           \
    .imu = {.enabled = false, .sda_gpio = 21, .scl_gpio = 22, .address = 0x68,  \
            .clock_hz = 100000}

const vg_hw_config_t vg_board_wearable = {
    .board_name = "esp32_devkit30_provisional",
    .description = "PROVISIONAL photo-based config; pins proposed, nothing verified",
    .role = VG_ROLE_WEARABLE,
    PROVISIONAL_CHIP,
    .emg = {.enabled = false, .gpio = 36, .atten = VG_ATTEN_UNSET},
    PROPOSED_IMU,
    .motor = {.enabled = false, .gpio = VG_PIN_NONE},
    .velostat = {.enabled = false, .gpio = VG_PIN_NONE},
    .oled = {.enabled = false},
    .dht11 = {.enabled = false, .gpio = VG_PIN_NONE},
};

const vg_hw_config_t vg_board_station = {
    .board_name = "esp32_devkit30_provisional",
    .description = "PROVISIONAL photo-based config; pins proposed; motor disabled",
    .role = VG_ROLE_STATION,
    PROVISIONAL_CHIP,
    .emg = {.enabled = false, .gpio = VG_PIN_NONE},
    PROPOSED_IMU,
    .motor = {.enabled = false, .gpio = 25, .active_high = true,
              .actuator = VG_ACTUATOR_UNKNOWN, .driver_confirmed = false,
              .max_pulse_ms = 0},
    .velostat = {.enabled = false, .gpio = 34, .atten = VG_ATTEN_UNSET},
    .oled = {.enabled = false},
    .dht11 = {.enabled = false, .gpio = VG_PIN_NONE},
};
