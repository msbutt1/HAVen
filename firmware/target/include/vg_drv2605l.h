/* TI DRV2605L haptic driver adapter (I2C), implementing vg_haptic_t.
 * Register facts: TI SLOS854D (rev. March 2018). ERM open-loop, real-time
 * playback (RTP) mode. Output off = MODE register STANDBY (0x40). */
#ifndef VG_DRV2605L_H
#define VG_DRV2605L_H

#include "driver/i2c_master.h"
#include "vg/haptic.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    i2c_master_bus_handle_t bus;
    i2c_master_dev_handle_t dev;
    uint8_t addr;
    uint8_t od_clamp;
    uint8_t last_status;
} vg_drv2605l_t;

vg_haptic_t vg_drv2605l_haptic(vg_drv2605l_t *d, i2c_master_bus_handle_t bus, uint8_t addr,
                               uint8_t od_clamp);

#ifdef __cplusplus
}
#endif
#endif
