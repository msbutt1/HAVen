/* Extend a wrapping 32-bit microsecond counter to 64 bits.
 *
 * The ESP32 firmware timestamps with esp_timer_get_time(), which is already
 * 64-bit, so this is only needed for 32-bit time sources (e.g. a sensor's or
 * a peripheral's own counter). Correct as long as it is called at least once
 * per wrap period (2^32 us = 71.6 min) and readings are monotonic modulo 2^32.
 */
#ifndef VG_TIME_EXT_H
#define VG_TIME_EXT_H

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint32_t last;
    uint64_t high;
    bool init;
} vg_time_ext_t;

static inline uint64_t vg_time_extend32(vg_time_ext_t *e, uint32_t raw)
{
    if (e->init && raw < e->last) {
        e->high += (uint64_t)1 << 32;
    }
    e->init = true;
    e->last = raw;
    return e->high | raw;
}

#endif
