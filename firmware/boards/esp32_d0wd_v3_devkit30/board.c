/* CONFIRMED CHIP on both boards, 2026-11-07, esptool 5.6.0:
 *   board "W" (wearable) MAC 28:05:a5:07:55:54; board "S" (station) MAC 28:05:a5:07:fb:6c.
 *   ESP32-D0WD-V3 rev v3.1, dual core 240 MHz, 40 MHz crystal, Vref cal in eFuse,
 *   4 MB flash (mfr 0x68, dev 0x4016), flash voltage 3.3 V set by strapping pin.
 *   USB-serial: Silicon Labs CP2102 (10c4:ea60).
 * Identical readings on both boards. 30-pin dev board photographed 2026-11-07 (module shield
 * "ESP-32", code XX5R69, USB-C, EN/BOOT buttons). PSRAM on GPIO16/17 not
 * checked (left unknown). Pins below are PROPOSED; nothing is wired to them
 * yet, and every peripheral that depends on an unresolved fact stays disabled.
 * Flash voltage is strapped to 3.3 V: nothing may pull GPIO12 (MTDI) high at reset.
 *
 * Pin rationale (ESP-IDF v6.1 ESP32 GPIO docs, see chip_caps.c):
 *  - EMG     GPIO36 (VP): ADC1_CH0, input-only (fine for analog in), not strapping.
 *            Disabled until BioAmp 3.3 V interfacing + EMG jumper state are confirmed.
 *  - IMU     SDA GPIO21 / SCL GPIO22: not strapping, not flash/UART, output-capable.
 *            Address 0x68 assumes AD0 tied to GND (proposed wiring); identity is read
 *            on-device in Prompt 3. 100 kHz until the bus is verified.
 *            Disabled until the module is wired as proposed.
 *  - Motor   DRV2605L haptic driver over I2C (see station config below); no GPIO.
 *  - Velostat GPIO34: ADC1_CH6, input-only. Disabled: divider circuit unknown.
 *  - OLED / DHT11: disabled (optional; controller/address and module type unknown).
 */
#include "vg/hw_config.h"

#define BOARD_W_CHIP                                                       \
    .chip = VG_CHIP_ESP32, .chip_status = VG_FACT_CONFIRMED,                 \
    .chip_evidence = "esptool 5.6.0: ESP32-D0WD-V3 rev v3.1 on W (28:05:a5:07:55:54) and S (28:05:a5:07:fb:6c)", \
    .gpio16_17_free_status = VG_FACT_UNKNOWN

/* Wearable IMU WIRED (reported by Salaar 2026-11-07): VCC->3V3, GND->GND,
 * SDA->D18 (GPIO18), SCL->D19 (GPIO19); AD0->GND and FSYNC->GND (grounded later the
 * same day, per PS-MPU-9250A-01). Verified: MPU-6500 at 0x68 before and after. */
#define WEARABLE_IMU                                                           \
    .imu = {.enabled = true, .sda_gpio = 18, .scl_gpio = 19, .address = 0x68,   \
            .clock_hz = 400000}
/* Station IMU: not wired yet; proposed pins. */
#define PROPOSED_IMU                                                           \
    .imu = {.enabled = false, .sda_gpio = 21, .scl_gpio = 22, .address = 0x68,  \
            .clock_hz = 100000}

const vg_hw_config_t vg_board_wearable = {
    .board_name = "esp32_d0wd_v3_devkit30",
    .description = "board W: chip confirmed; pins PROPOSED, nothing wired or verified",
    .role = VG_ROLE_WEARABLE,
    BOARD_W_CHIP,
    /* WIRED (reported by Salaar 2026-11-07): BioAmp OUT -> D34 (GPIO34, ADC1_CH6),
     * BioAmp VCC -> 3V3, GND -> GND. ENABLED for live acquisition (Prompt 4):
     * 12 dB attenuation (ESP32 datasheet effective range 150-2450 mV; the
     * BioAmp at 3V3 cannot exceed 3.3 V). Caveat in hello: BioAmp BANDPASS
     * jumper still in its default EEG/EOG position. */
    .emg = {.enabled = true, .gpio = 34, .atten = VG_ATTEN_DB_12, .bitwidth = 12},
    WEARABLE_IMU,
    .motor = {.enabled = false, .gpio = VG_PIN_NONE},
    .velostat = {.enabled = false, .gpio = VG_PIN_NONE},
    .oled = {.enabled = false},
    .dht11 = {.enabled = false, .gpio = VG_PIN_NONE},
};

const vg_hw_config_t vg_board_station = {
    .board_name = "esp32_d0wd_v3_devkit30",
    .description = "board S: chip confirmed; pins PROPOSED; motor disabled",
    .role = VG_ROLE_STATION,
    BOARD_W_CHIP,
    .emg = {.enabled = false, .gpio = VG_PIN_NONE},
    PROPOSED_IMU,
    /* CONFIRMED 2026-11-07: Adafruit DRV2605L breakout on the station I2C bus
     * (VIN->3V3, SDA->D21, SCL->D22), read-only probe DEVICE_ID 7, standby.
     * Motor: coin ERM, rated 2.5-3.8 V (product listing). Open-loop full scale
     * 3.0 V (OD_CLAMP 139). Pulses capped at 1000 ms. No GPIO drives the motor. */
    .motor = {.enabled = true, .driver = VG_MOTOR_DRIVER_DRV2605L, .gpio = VG_PIN_NONE,
              .i2c_address = 0x5A, .od_clamp = 139, .active_high = true,
              .actuator = VG_ACTUATOR_ERM_BRUSHED, .driver_confirmed = true,
              .max_pulse_ms = 1000},
    .velostat = {.enabled = false, .gpio = 34, .atten = VG_ATTEN_UNSET},
    .oled = {.enabled = false},
    .dht11 = {.enabled = false, .gpio = VG_PIN_NONE},
};
