/* MPU-6500 / MPU-9250 accelerometer acquisition over I2C (ESP-IDF).
 * Register facts: TDK RM-MPU-9250A-00 Rev 1.6 (see shared/include/vg/imu_mpu.h).
 * Board W: MPU-6500 (WHO_AM_I 0x70, measured), SDA GPIO18, SCL GPIO19, 0x68. */
#ifndef VG_MPU6500_H
#define VG_MPU6500_H

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "vg/imu_mpu.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    i2c_master_bus_handle_t bus;
    i2c_master_dev_handle_t dev;
    uint8_t addr;
    uint8_t who_am_i;
    vg_accel_derived_t derived;     /* from read-back registers */
    uint32_t i2c_errors;
} vg_mpu_t;

/* Acquisition configuration: +-8 g (4096 LSB/g), accel DLPF 218.1 Hz,
 * SMPLRT_DIV 1 -> 500 Hz output. Written then read back; false on mismatch
 * or unsupported identity (detail explains). */
bool vg_mpu_init(vg_mpu_t *m, int sda, int scl, uint8_t addr, uint32_t hz, char *detail,
                 size_t n);

/* One burst read of INT_STATUS + ACCEL_XOUT_H..ZOUT_L (0x3A..0x40).
 * Returns false on I2C error. *fresh = RAW_DATA_RDY (reading clears it), so a
 * sample is only new when *fresh is true. */
bool vg_mpu_read(vg_mpu_t *m, bool *fresh, int16_t xyz[3]);

#ifdef __cplusplus
}
#endif
#endif
