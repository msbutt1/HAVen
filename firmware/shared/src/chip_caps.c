#include "vg/chip_caps.h"

#include <stddef.h>

/* ESP32 (original). Source: ESP-IDF Programming Guide v6.1, ESP32 target,
 * "GPIO & RTC GPIO" (api-reference/peripherals/gpio.html), read 2026-11-07:
 *  - GPIO34-39 input only, no software pull-up/pull-down
 *  - strapping: GPIO0, 2, 5, 12 (MTDI), 15 (MTDO)
 *  - GPIO6-11 and GPIO16-17 usually connected to integrated SPI flash/PSRAM
 *  - ADC1: 32(CH4) 33(CH5) 34(CH6) 35(CH7) 36(CH0) 37(CH1) 38(CH2) 39(CH3)
 *  - ADC2: 0(CH1) 2(CH2) 4(CH0) 12(CH5) 13(CH4) 14(CH6) 15(CH3) 25(CH8) 26(CH9) 27(CH7)
 *    "ADC2 pins cannot be used when Wi-Fi is used"
 *  - TXD/RXD (GPIO1/3) usually used for flashing and debugging
 *  - JTAG: GPIO12-15
 * GPIO20, 24 and 28-31 are not in the page's pin list: treated as absent.
 * GPIO37/38 are not broken out on the photographed 30-pin board.
 */
#define E VG_GPIO_EXISTS
#define I VG_GPIO_INPUT_ONLY
#define S VG_GPIO_STRAPPING
#define F VG_GPIO_FLASH
#define P VG_GPIO_PSRAM
#define U VG_GPIO_UART0
#define J VG_GPIO_JTAG

static const vg_gpio_caps_t ESP32_GPIO[40] = {
    [0]  = {E | S, 2, 1},
    [1]  = {E | U, 0, -1},
    [2]  = {E | S, 2, 2},
    [3]  = {E | U, 0, -1},
    [4]  = {E, 2, 0},
    [5]  = {E | S, 0, -1},
    [6]  = {E | F, 0, -1},
    [7]  = {E | F, 0, -1},
    [8]  = {E | F, 0, -1},
    [9]  = {E | F, 0, -1},
    [10] = {E | F, 0, -1},
    [11] = {E | F, 0, -1},
    [12] = {E | S | J, 2, 5},
    [13] = {E | J, 2, 4},
    [14] = {E | J, 2, 6},
    [15] = {E | S | J, 2, 3},
    [16] = {E | P, 0, -1},
    [17] = {E | P, 0, -1},
    [18] = {E, 0, -1},
    [19] = {E, 0, -1},
    [20] = {0, 0, -1},
    [21] = {E, 0, -1},
    [22] = {E, 0, -1},
    [23] = {E, 0, -1},
    [24] = {0, 0, -1},
    [25] = {E, 2, 8},
    [26] = {E, 2, 9},
    [27] = {E, 2, 7},
    [28] = {0, 0, -1},
    [29] = {0, 0, -1},
    [30] = {0, 0, -1},
    [31] = {0, 0, -1},
    [32] = {E, 1, 4},
    [33] = {E, 1, 5},
    [34] = {E | I, 1, 6},
    [35] = {E | I, 1, 7},
    [36] = {E | I, 1, 0},
    [37] = {E | I, 1, 1},
    [38] = {E | I, 1, 2},
    [39] = {E | I, 1, 3},
};

int vg_chip_gpio_count(vg_chip_t chip)
{
    return chip == VG_CHIP_ESP32 ? 40 : 0;
}

bool vg_chip_gpio_caps(vg_chip_t chip, int gpio, vg_gpio_caps_t *out)
{
    if (chip != VG_CHIP_ESP32 || gpio < 0 || gpio >= 40 || out == NULL) {
        return false;
    }
    *out = ESP32_GPIO[gpio];
    return true;
}
