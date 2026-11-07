/* ESP-IDF-only startup helpers shared by both role apps. */
#ifndef VG_TARGET_H
#define VG_TARGET_H

#include "vg/hw_config.h"

#ifdef __cplusplus
extern "C" {
#endif

#define VG_FIRMWARE_NAME "vibeguard-fw"
#define VG_FIRMWARE_VERSION "0.3.0-prompt5"

/* Logs firmware/board identity, the measured chip info, the configuration
 * report, and cross-checks the portable GPIO/ADC table against the IDF driver.
 * Returns the number of configuration errors plus table mismatches. */
int vg_target_startup(const vg_hw_config_t *cfg);

#ifdef __cplusplus
}
#endif
#endif
