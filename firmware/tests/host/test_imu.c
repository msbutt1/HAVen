/* Host tests for IMU identity and documentation-derived accelerometer values. */
#include "vg/imu_mpu.h"
#include "vg_test.h"

static void test_identity(void)
{
    vg_imu_identity_t id = vg_imu_identify(0x71);
    CHECK(id.status == VG_IMU_ID_OFFICIAL && strcmp(id.name, "MPU-9250") == 0);
    id = vg_imu_identify(0x70);
    CHECK(id.status == VG_IMU_ID_SECONDARY && strcmp(id.name, "MPU-6500") == 0);
    /* anything else, including values some clone parts report, is unsupported */
    const uint8_t others[] = {0x00, 0x68, 0x73, 0x75, 0x98, 0xFF};
    for (unsigned i = 0; i < sizeof others; i++) {
        CHECK(vg_imu_identify(others[i]).status == VG_IMU_ID_UNSUPPORTED);
    }
}

static void test_derive_tables(void)
{
    /* RM-MPU-9250A-00 r1.6: ACCEL_FS_SEL, ACCEL_CONFIG2 table, SMPLRT_DIV rule */
    vg_accel_derived_t d = vg_accel_derive(0x00, 0x00, 0x00, 0x00, 0x00);
    CHECK(d.full_scale_g == 2 && d.lsb_per_g == 16384);
    CHECK(!d.dlpf_bypassed && d.bandwidth_3db_hz == 218.1 && d.internal_rate_hz == 1000.0);
    CHECK(!d.smplrt_div_effective && d.output_rate_hz == 1000.0); /* dlpf_cfg=0 */

    d = vg_accel_derive(0x18, 0x08, 0x04, 0x01, 0x00);           /* +-16 g, bypass */
    CHECK(d.full_scale_g == 16 && d.lsb_per_g == 2048);
    CHECK(d.dlpf_bypassed && d.bandwidth_3db_hz == 1046.0 && d.internal_rate_hz == 4000.0);
    CHECK(!d.smplrt_div_effective && d.output_rate_hz == 4000.0);

    d = vg_accel_derive(0x10, 0x03, 0x09, 0x03, 0x00);           /* +-8 g, 44.8 Hz, /10 */
    CHECK(d.full_scale_g == 8 && d.bandwidth_3db_hz == 44.8 && d.smplrt_div_effective &&
          d.output_rate_hz == 100.0);

    d = vg_accel_derive(0x00, 0x00, 0x09, 0x01, 0x01);           /* gyro fchoice_b != 0 */
    CHECK(!d.smplrt_div_effective && d.output_rate_hz == 1000.0);
}

static void test_diag_setup_derives_500hz(void)
{
    uint8_t reg[256] = {0};
    for (int i = 0; i < VG_DIAG_ACCEL_SETUP_N; i++) {
        reg[VG_DIAG_ACCEL_SETUP[i].reg] = VG_DIAG_ACCEL_SETUP[i].value;
    }
    vg_accel_derived_t d = vg_accel_derive(reg[VG_MPU_ACCEL_CONFIG], reg[VG_MPU_ACCEL_CONFIG2],
                                           reg[VG_MPU_SMPLRT_DIV], reg[VG_MPU_CONFIG],
                                           reg[VG_MPU_GYRO_CONFIG]);
    CHECK(d.full_scale_g == 4 && d.lsb_per_g == 8192);
    CHECK(d.output_rate_hz == 500.0 && d.bandwidth_3db_hz == 218.1);
    CHECK(d.bandwidth_3db_hz < d.output_rate_hz / 2);  /* below Nyquist */
}

int main(void)
{
    RUN(test_identity);
    RUN(test_derive_tables);
    RUN(test_diag_setup_derives_500hz);
    return DONE();
}
