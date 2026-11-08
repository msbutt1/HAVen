/* MPU-9250 / MPU-6500 family: register addresses, identity, and values DERIVED
 * from documentation (never measured). Portable; tested on the host.
 *
 * Primary sources (TDK InvenSense):
 *   RM-MPU-9250A-00 "MPU-9250 Register Map and Descriptions", Rev 1.6, 01/07/2015
 *   PS-MPU-9250A-01 "MPU-9250 Product Specification", Rev 1.0
 * The official MPU-6500 register map could not be retrieved (TDK's document
 * links now return HTML). WHO_AM_I = 0x70 for the MPU-6500 therefore comes
 * from secondary sources only and is reported as such.
 */
#ifndef VG_IMU_MPU_H
#define VG_IMU_MPU_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* I2C address: 0b110100X, X = AD0 pin level (PS-MPU-9250A-01). */
#define VG_MPU_ADDR_AD0_LOW 0x68
#define VG_MPU_ADDR_AD0_HIGH 0x69

/* Register addresses (RM-MPU-9250A-00 Rev 1.6, register map table). */
enum {
    VG_MPU_SMPLRT_DIV = 0x19,
    VG_MPU_CONFIG = 0x1A,
    VG_MPU_GYRO_CONFIG = 0x1B,
    VG_MPU_ACCEL_CONFIG = 0x1C,
    VG_MPU_ACCEL_CONFIG2 = 0x1D,
    VG_MPU_INT_STATUS = 0x3A,     /* bit0 RAW_DATA_RDY_INT */
    VG_MPU_ACCEL_XOUT_H = 0x3B,   /* 6 bytes: X, Y, Z big-endian int16 */
    VG_MPU_PWR_MGMT_1 = 0x6B,     /* bit7 H_RESET, bit6 SLEEP, [2:0] CLKSEL */
    VG_MPU_PWR_MGMT_2 = 0x6C,     /* DIS_XA..DIS_ZG */
    VG_MPU_WHO_AM_I = 0x75,
};

typedef enum {
    VG_IMU_ID_UNSUPPORTED = 0,    /* fail visibly: no configuration attempted */
    VG_IMU_ID_OFFICIAL,           /* identity value from a primary TDK document */
    VG_IMU_ID_SECONDARY,          /* identity value from secondary sources only */
} vg_imu_id_status_t;

typedef struct {
    vg_imu_id_status_t status;
    const char *name;             /* e.g. "MPU-9250" or "unknown" */
    const char *source;           /* where the identity value comes from */
} vg_imu_identity_t;

vg_imu_identity_t vg_imu_identify(uint8_t who_am_i);

/* Accelerometer settings decoded from register values, using the tables in
 * RM-MPU-9250A-00 Rev 1.6 (ACCEL_CONFIG, ACCEL_CONFIG2, SMPLRT_DIV). These
 * are DERIVED from documentation, not measured. */
typedef struct {
    int full_scale_g;             /* 2, 4, 8, 16 */
    int lsb_per_g;                /* PS-MPU-9250A-01 sensitivity table */
    bool dlpf_bypassed;           /* accel_fchoice_b = 1 */
    double bandwidth_3db_hz;
    double internal_rate_hz;      /* 4000 if bypassed, else 1000 */
    double output_rate_hz;        /* internal / (1 + SMPLRT_DIV) when DLPF active */
    bool smplrt_div_effective;    /* per RM: needs fchoice_b = 00 and 0 < dlpf_cfg < 7 */
} vg_accel_derived_t;

vg_accel_derived_t vg_accel_derive(uint8_t accel_config, uint8_t accel_config2, uint8_t smplrt_div,
                                   uint8_t config, uint8_t gyro_config);

/* The diagnostic's proposed accelerometer configuration (written, then read back). */
typedef struct {
    uint8_t reg;
    uint8_t value;
    const char *meaning;
} vg_reg_write_t;

extern const vg_reg_write_t VG_DIAG_ACCEL_SETUP[];
extern const int VG_DIAG_ACCEL_SETUP_N;

#ifdef __cplusplus
}
#endif
#endif
