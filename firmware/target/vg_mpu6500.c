#include "vg_mpu6500.h"

#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define TIMEOUT_MS 5

static esp_err_t wr(vg_mpu_t *m, uint8_t reg, uint8_t v)
{
    uint8_t b[2] = {reg, v};
    return i2c_master_transmit(m->dev, b, 2, TIMEOUT_MS);
}

static esp_err_t rd(vg_mpu_t *m, uint8_t reg, uint8_t *buf, size_t n)
{
    return i2c_master_transmit_receive(m->dev, &reg, 1, buf, n, TIMEOUT_MS);
}

/* Acquisition setup (differs from the diagnostic only in range: +-8 g gives
 * 4096 LSB/g; tool vibration plus gravity stays well inside +-8 g). */
static const vg_reg_write_t SETUP[] = {
    {VG_MPU_PWR_MGMT_1, 0x01, "SLEEP=0, CLKSEL=1"},
    {VG_MPU_PWR_MGMT_2, 0x00, "all axes on"},
    {VG_MPU_CONFIG, 0x01, "DLPF_CFG=1 (SMPLRT_DIV effective)"},
    {VG_MPU_GYRO_CONFIG, 0x00, "FCHOICE_B=00"},
    {VG_MPU_SMPLRT_DIV, 0x01, "1 kHz / 2 = 500 Hz"},
    {VG_MPU_ACCEL_CONFIG, 0x10, "ACCEL_FS_SEL=10 (+-8 g)"},
    {VG_MPU_ACCEL_CONFIG2, 0x01, "A_DLPF_CFG=1 (218.1 Hz)"},
};

bool vg_mpu_init(vg_mpu_t *m, int sda, int scl, uint8_t addr, uint32_t hz, char *detail,
                 size_t n)
{
    i2c_master_bus_config_t bc = {.i2c_port = I2C_NUM_0, .sda_io_num = sda, .scl_io_num = scl,
                                  .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7,
                                  .flags.enable_internal_pullup = true};
    if (m->bus == NULL && i2c_new_master_bus(&bc, &m->bus) != ESP_OK) {
        snprintf(detail, n, "I2C bus init failed");
        return false;
    }
    i2c_device_config_t dc = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = addr,
                              .scl_speed_hz = hz};
    if (m->dev == NULL && i2c_master_bus_add_device(m->bus, &dc, &m->dev) != ESP_OK) {
        snprintf(detail, n, "I2C add device failed");
        return false;
    }
    m->addr = addr;
    uint8_t who = 0;
    if (rd(m, VG_MPU_WHO_AM_I, &who, 1) != ESP_OK) {
        snprintf(detail, n, "no response at 0x%02x", addr);
        return false;
    }
    m->who_am_i = who;
    if (vg_imu_identify(who).status == VG_IMU_ID_UNSUPPORTED) {
        snprintf(detail, n, "unsupported WHO_AM_I 0x%02x", who);
        return false;
    }
    if (wr(m, VG_MPU_PWR_MGMT_1, 0x80) != ESP_OK) {    /* H_RESET */
        snprintf(detail, n, "reset failed");
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(100));                    /* PS: start-up max 100 ms */
    for (size_t i = 0; i < sizeof SETUP / sizeof SETUP[0]; i++) {
        uint8_t back = 0xFF;
        if (wr(m, SETUP[i].reg, SETUP[i].value) != ESP_OK ||
            rd(m, SETUP[i].reg, &back, 1) != ESP_OK || back != SETUP[i].value) {
            snprintf(detail, n, "reg 0x%02x wrote 0x%02x read 0x%02x", SETUP[i].reg,
                     SETUP[i].value, back);
            return false;
        }
    }
    uint8_t r[5];
    if (rd(m, VG_MPU_SMPLRT_DIV, r, 5) != ESP_OK) {
        snprintf(detail, n, "config readback failed");
        return false;
    }
    m->derived = vg_accel_derive(r[3], r[4], r[0], r[1], r[2]);
    snprintf(detail, n, "%s WHO_AM_I 0x%02x, +-%dg %d LSB/g, BW %.1f Hz, ODR %.0f Hz",
             vg_imu_identify(who).name, who, m->derived.full_scale_g, m->derived.lsb_per_g,
             m->derived.bandwidth_3db_hz, m->derived.output_rate_hz);
    return true;
}

bool vg_mpu_read(vg_mpu_t *m, bool *fresh, int16_t xyz[3])
{
    uint8_t b[7];
    if (rd(m, VG_MPU_INT_STATUS, b, sizeof b) != ESP_OK) {
        m->i2c_errors++;
        return false;
    }
    *fresh = (b[0] & 0x01) != 0;
    for (int a = 0; a < 3; a++) {
        xyz[a] = (int16_t)((b[1 + 2 * a] << 8) | b[2 + 2 * a]);
    }
    return true;
}
