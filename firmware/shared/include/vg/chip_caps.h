/* Per-chip GPIO capability tables, transcribed from Espressif's ESP-IDF v6.1
 * "GPIO & RTC GPIO" page for the ESP32 target (see VERSIONS.md). On target
 * builds the ADC mapping is cross-checked against the IDF driver at startup.
 */
#ifndef VG_CHIP_CAPS_H
#define VG_CHIP_CAPS_H

#include <stdbool.h>
#include <stdint.h>

#include "vg/hw_config.h"

#ifdef __cplusplus
extern "C" {
#endif

enum {
    VG_GPIO_EXISTS     = 1u << 0,
    VG_GPIO_INPUT_ONLY = 1u << 1,  /* ESP32: GPIO34-39, no internal pulls */
    VG_GPIO_STRAPPING  = 1u << 2,  /* ESP32: 0, 2, 5, 12, 15 */
    VG_GPIO_FLASH      = 1u << 3,  /* ESP32: 6-11 (SPI flash) */
    VG_GPIO_PSRAM      = 1u << 4,  /* ESP32: 16-17 on modules with PSRAM */
    VG_GPIO_UART0      = 1u << 5,  /* ESP32: 1 (TXD0), 3 (RXD0): flashing/console */
    VG_GPIO_JTAG       = 1u << 6,  /* ESP32: 12-15 */
};

typedef struct {
    uint8_t flags;
    int8_t adc_unit;     /* 1, 2, or 0 = none */
    int8_t adc_channel;  /* -1 = none */
} vg_gpio_caps_t;

/* Returns false if the chip is unknown or the GPIO is out of range. */
bool vg_chip_gpio_caps(vg_chip_t chip, int gpio, vg_gpio_caps_t *out);
int vg_chip_gpio_count(vg_chip_t chip);

#ifdef __cplusplus
}
#endif
#endif
