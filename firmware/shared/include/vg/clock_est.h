/* Sample-clock tracker: turns jittery READ times of a sensor that samples on
 * its own clock (e.g. the IMU's ODR) into smooth sample timestamps.
 *
 *   predicted += period
 *   err = read_time - predicted
 *   if |err| > resync_us: predicted = read_time (gap/missed samples: re-lock,
 *                         reported via resyncs so the batch break is honest)
 *   else predicted += a * err ; period += b * err     (phase + frequency lock)
 *
 * Timestamps then reflect the sensor clock (steady spacing) while staying
 * locked to the device clock on average. Documented as "reconstructed".
 */
#ifndef VG_CLOCK_EST_H
#define VG_CLOCK_EST_H

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    double period_us;      /* current estimate */
    double nominal_us;
    double predicted_us;
    double alpha, beta;    /* phase and frequency gains */
    double resync_us;      /* error beyond this re-locks to the read time */
    bool locked;
    uint32_t resyncs;
} vg_clock_est_t;

static inline void vg_clock_est_init(vg_clock_est_t *c, double nominal_us)
{
    c->period_us = c->nominal_us = nominal_us;
    c->predicted_us = 0;
    c->alpha = 0.05;
    c->beta = 0.0005;
    c->resync_us = 1.5 * nominal_us;
    c->locked = false;
    c->resyncs = 0;
}

/* Feed the read time of one NEW sample; returns its reconstructed timestamp. */
static inline int64_t vg_clock_est_update(vg_clock_est_t *c, int64_t read_us)
{
    if (!c->locked) {
        c->predicted_us = (double)read_us;
        c->locked = true;
        return read_us;
    }
    c->predicted_us += c->period_us;
    double err = (double)read_us - c->predicted_us;
    if (err > c->resync_us || err < -c->resync_us) {
        c->predicted_us = (double)read_us;
        c->period_us = c->nominal_us;
        c->resyncs++;
    } else {
        c->predicted_us += c->alpha * err;
        c->period_us += c->beta * err;
        /* keep the frequency estimate within +-5 % of nominal */
        if (c->period_us > 1.05 * c->nominal_us) c->period_us = 1.05 * c->nominal_us;
        if (c->period_us < 0.95 * c->nominal_us) c->period_us = 0.95 * c->nominal_us;
    }
    return (int64_t)(c->predicted_us + 0.5);
}

#endif
