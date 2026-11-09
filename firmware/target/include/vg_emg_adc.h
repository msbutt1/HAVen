/* Hardware-paced EMG acquisition: ESP32 ADC1 continuous (DMA) mode.
 *
 * The ESP32's continuous ADC runs at >= 20 kHz (SOC_ADC_SAMPLE_FREQ_THRES_LOW),
 * so EMG is converted at 20 kHz and each block of 20 conversions is averaged
 * into one 1 kHz sample (a boxcar decimator, which also attenuates content
 * near multiples of 1 kHz before the rate reduction).
 *
 * Timestamps are RECONSTRUCTED from the conversion count:
 *     t_k = t_anchor + k * 1000 us
 * so sample spacing reflects the ADC clock, not task scheduling. The anchor
 * is the esp_timer time at start; it is compared with the device clock on
 * every read and re-anchored (visible as a timing break) if they drift apart
 * by more than VG_EMG_REANCHOR_US.
 */
#ifndef VG_EMG_ADC_H
#define VG_EMG_ADC_H

#include <stdbool.h>
#include <stdint.h>

#include "vg/sample_ring.h"

#ifdef __cplusplus
extern "C" {
#endif

#define VG_EMG_CONV_HZ 20000u
#define VG_EMG_OUT_HZ 1000u
#define VG_EMG_REANCHOR_US 2000

typedef struct {
    volatile uint32_t samples, clipped, reanchors, pool_overflows, bad_channel;
    volatile int32_t last_drift_us;
    /* diagnostics: raw 20 kHz readings at 0 or 4095 (total), and the lowest/highest 1 kHz value in the last
     * window (reset by vg_emg_adc_take_range) */
    volatile uint32_t raw_rail_hits;
    volatile int32_t win_min, win_max;
} vg_emg_stats_t;

/* Starts continuous conversion on gpio (must be ADC1) and a reader task on
 * core 1 that pushes 1 kHz samples into ring. */
bool vg_emg_adc_start(int gpio, vg_sample_ring_t *ring, char *detail, size_t n);
const vg_emg_stats_t *vg_emg_adc_stats(void);
/* Lowest and highest 1 kHz value since the last call (for the periodic log); resets the window. */
void vg_emg_adc_take_range(int32_t *lo, int32_t *hi);

#ifdef __cplusplus
}
#endif
#endif
