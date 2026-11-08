#include "vg/imu_mpu.h"

#include <stddef.h>

vg_imu_identity_t vg_imu_identify(uint8_t who)
{
    switch (who) {
    case 0x71:
        return (vg_imu_identity_t){VG_IMU_ID_OFFICIAL, "MPU-9250",
                                   "RM-MPU-9250A-00 Rev 1.6: WHO_AM_I default 0x71"};
    case 0x70:
        return (vg_imu_identity_t){VG_IMU_ID_SECONDARY, "MPU-6500",
                                   "secondary sources only (official MPU-6500 register map "
                                   "not obtained); register layout assumed from MPU-9250 map"};
    default:
        return (vg_imu_identity_t){VG_IMU_ID_UNSUPPORTED, "unknown",
                                   "WHO_AM_I not in the supported table"};
    }
}

/* ACCEL_CONFIG2 table, RM-MPU-9250A-00 Rev 1.6 section 4.8 (accel_fchoice = 1). */
static const double ACCEL_DLPF_BW_HZ[8] = {218.1, 218.1, 99.0, 44.8, 21.2, 10.2, 5.05, 420.0};

vg_accel_derived_t vg_accel_derive(uint8_t accel_config, uint8_t accel_config2, uint8_t smplrt_div,
                                   uint8_t config, uint8_t gyro_config)
{
    vg_accel_derived_t d;
    int fs = (accel_config >> 3) & 0x3;
    static const int FS_G[4] = {2, 4, 8, 16};
    static const int LSB[4] = {16384, 8192, 4096, 2048};
    d.full_scale_g = FS_G[fs];
    d.lsb_per_g = LSB[fs];
    d.dlpf_bypassed = (accel_config2 >> 3) & 0x1;
    if (d.dlpf_bypassed) {
        d.bandwidth_3db_hz = 1046.0;
        d.internal_rate_hz = 4000.0;
    } else {
        d.bandwidth_3db_hz = ACCEL_DLPF_BW_HZ[accel_config2 & 0x7];
        d.internal_rate_hz = 1000.0;
    }
    int dlpf_cfg = config & 0x7;
    int fchoice_b = gyro_config & 0x3;
    d.smplrt_div_effective = !d.dlpf_bypassed && fchoice_b == 0 && dlpf_cfg > 0 && dlpf_cfg < 7;
    d.output_rate_hz = d.smplrt_div_effective ? d.internal_rate_hz / (1.0 + smplrt_div)
                                              : d.internal_rate_hz;
    return d;
}

/* Diagnostic setup: accelerometer +-4 g, accel DLPF 218.1 Hz, 1 kHz internal,
 * SMPLRT_DIV 1 -> 500 Hz output (bandwidth below the 250 Hz Nyquist limit).
 * This is a bring-up configuration, not the final acquisition choice. */
const vg_reg_write_t VG_DIAG_ACCEL_SETUP[] = {
    {VG_MPU_PWR_MGMT_1, 0x01, "SLEEP=0, CLKSEL=1 (auto: PLL if ready)"},
    {VG_MPU_PWR_MGMT_2, 0x00, "all accel and gyro axes enabled"},
    {VG_MPU_CONFIG, 0x01, "DLPF_CFG=1 (needed for SMPLRT_DIV to apply)"},
    {VG_MPU_GYRO_CONFIG, 0x00, "FCHOICE_B=00"},
    {VG_MPU_SMPLRT_DIV, 0x01, "divide 1 kHz by 2 -> 500 Hz"},
    {VG_MPU_ACCEL_CONFIG, 0x08, "ACCEL_FS_SEL=01 (+-4 g)"},
    {VG_MPU_ACCEL_CONFIG2, 0x01, "accel_fchoice_b=0, A_DLPF_CFG=1 (218.1 Hz)"},
};
const int VG_DIAG_ACCEL_SETUP_N = (int)(sizeof VG_DIAG_ACCEL_SETUP / sizeof VG_DIAG_ACCEL_SETUP[0]);
