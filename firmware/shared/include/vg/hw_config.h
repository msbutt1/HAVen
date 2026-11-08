/* VibeGuard firmware: hardware configuration types.
 *
 * Board/pin/power FACTS live in boards/<name>/board.c as data of these types.
 * Logic never hard-codes a pin. Every fact carries a status so an unconfirmed
 * value cannot be mistaken for a measured one.
 *
 * Portable C11: no ESP-IDF headers, so this builds and is tested on the host.
 */
#ifndef VG_HW_CONFIG_H
#define VG_HW_CONFIG_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VG_PIN_NONE (-1)

/* Hard local ceiling for any single stimulus pulse, independent of host
 * commands and of board config. Board configs may only set a lower value. */
#define VG_MOTOR_MAX_PULSE_CEILING_MS 2000u

/* Station motor (product listing, 2026-11-07): ERM rated 2.5-3.8 V. DRV2605L
 * open-loop full scale V = 21.59 mV x OD_CLAMP (SLOS854D Eq. 6), so 3.8 V
 * rated max -> OD_CLAMP <= 176. */
#define VG_DRV_OD_CLAMP_MAX 176

typedef enum {
    VG_FACT_UNKNOWN = 0,   /* not known: logic must treat as unavailable */
    VG_FACT_PROVISIONAL,   /* inferred (photo, seller label); not verified */
    VG_FACT_CONFIRMED,     /* verified on the device or from its own marking + datasheet */
} vg_fact_status_t;

typedef enum {
    VG_CHIP_UNKNOWN = 0,
    VG_CHIP_ESP32,         /* original dual-core ESP32 (Xtensa LX6) */
} vg_chip_t;

typedef enum {
    VG_ROLE_WEARABLE = 1,
    VG_ROLE_STATION = 2,
} vg_role_t;

typedef enum {
    VG_ATTEN_UNSET = 0,    /* must be chosen from the confirmed input range */
    VG_ATTEN_DB_0,
    VG_ATTEN_DB_2_5,
    VG_ATTEN_DB_6,
    VG_ATTEN_DB_12,
} vg_adc_atten_t;

typedef enum {
    VG_ACTUATOR_UNKNOWN = 0,
    VG_ACTUATOR_ERM_BRUSHED,   /* eccentric rotating mass, brushed DC */
    VG_ACTUATOR_LRA,           /* linear resonant actuator: needs a different driver */
} vg_actuator_t;

typedef struct {
    bool enabled;
    int sda_gpio;
    int scl_gpio;
    uint8_t address;           /* 7-bit; identity is read on-device, never assumed */
    uint32_t clock_hz;
} vg_i2c_imu_cfg_t;

typedef struct {
    bool enabled;
    int gpio;
    vg_adc_atten_t atten;
    uint8_t bitwidth;          /* 0 = driver default */
} vg_adc_input_cfg_t;

typedef enum {
    VG_MOTOR_DRIVER_GPIO = 0,      /* discrete driver (transistor/MOSFET) on a GPIO */
    VG_MOTOR_DRIVER_DRV2605L = 1,  /* TI DRV2605L haptic driver on the shared I2C bus */
} vg_motor_driver_t;

typedef struct {
    bool enabled;              /* false unless motor, driver and wiring are confirmed */
    vg_motor_driver_t driver;
    int gpio;                  /* GPIO driver input pin (DRIVER_GPIO only); never the motor */
    uint8_t i2c_address;       /* DRV2605L: 0x5A (TI SLOS854D) */
    uint8_t od_clamp;          /* DRV2605L open-loop full scale: V = 21.59 mV x od_clamp */
    bool active_high;
    vg_actuator_t actuator;
    bool driver_confirmed;     /* a real driver circuit exists and is verified */
    uint32_t max_pulse_ms;     /* <= VG_MOTOR_MAX_PULSE_CEILING_MS */
} vg_motor_cfg_t;

typedef struct {
    bool enabled;              /* optional: shares the IMU I2C bus */
    uint8_t address;
} vg_oled_cfg_t;

typedef struct {
    bool enabled;              /* optional */
    int gpio;
} vg_dht_cfg_t;

typedef struct {
    const char *board_name;
    const char *description;
    vg_role_t role;

    vg_chip_t chip;
    vg_fact_status_t chip_status;
    const char *chip_evidence;

    /* GPIO16/17 are used for PSRAM on some module types. */
    vg_fact_status_t gpio16_17_free_status;

    /* wearable */
    vg_adc_input_cfg_t emg;
    /* both roles: one IMU per controller */
    vg_i2c_imu_cfg_t imu;
    /* station */
    vg_motor_cfg_t motor;
    vg_adc_input_cfg_t velostat;
    vg_oled_cfg_t oled;
    vg_dht_cfg_t dht11;
} vg_hw_config_t;

/* Provided by the selected board component (boards/<name>/board.c). */
extern const vg_hw_config_t vg_board_wearable;
extern const vg_hw_config_t vg_board_station;

const char *vg_fact_status_name(vg_fact_status_t s);
const char *vg_chip_name(vg_chip_t c);
const char *vg_atten_name(vg_adc_atten_t a);
const char *vg_actuator_name(vg_actuator_t a);

#ifdef __cplusplus
}
#endif
#endif
