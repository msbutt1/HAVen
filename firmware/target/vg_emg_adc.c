#include "vg_emg_adc.h"

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "esp_adc/adc_continuous.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define DECIM (VG_EMG_CONV_HZ / VG_EMG_OUT_HZ)            /* 20 */
#define FRAME_CONV (DECIM * 10)                           /* 10 ms of conversions */
#define FRAME_BYTES (FRAME_CONV * SOC_ADC_DIGI_RESULT_BYTES)

static adc_continuous_handle_t s_h;
static adc_channel_t s_ch;
static vg_sample_ring_t *s_ring;
static vg_emg_stats_t s_stats;

static bool IRAM_ATTR on_ovf(adc_continuous_handle_t h, const adc_continuous_evt_data_t *e, void *u)
{
    (void)h;
    (void)e;
    (void)u;
    s_stats.pool_overflows++;
    return false;
}

static void emg_task(void *arg)
{
    (void)arg;
    static uint8_t buf[FRAME_BYTES];
    uint32_t acc = 0, n_in_block = 0;
    bool clip = false;
    uint64_t k = 0;                                       /* output sample count */
    int64_t anchor = esp_timer_get_time();
    int64_t window_start = anchor;
    int32_t min_drift = INT32_MAX;
    for (;;) {
        uint32_t got = 0;
        if (adc_continuous_read(s_h, buf, sizeof buf, &got, 100) != ESP_OK) {
            continue;
        }
        int64_t now = esp_timer_get_time();
        for (uint32_t i = 0; i + SOC_ADC_DIGI_RESULT_BYTES <= got; i += SOC_ADC_DIGI_RESULT_BYTES) {
            const adc_digi_output_data_t *d = (const adc_digi_output_data_t *)&buf[i];
            if (d->type1.channel != s_ch) {
                s_stats.bad_channel++;
                continue;
            }
            uint32_t v = d->type1.data;
            acc += v;
            clip |= (v == 0 || v >= 4095);
            if (++n_in_block == DECIM) {
                vg_sample_t s = {0};
                s.t_us = anchor + (int64_t)(k * (1000000u / VG_EMG_OUT_HZ));
                s.v[0] = (int32_t)((acc + DECIM / 2) / DECIM);
                if (clip) {
                    s.flags = VG_SF_CLIPPED;
                    s_stats.clipped++;
                }
                vg_ring_push(s_ring, &s);
                s_stats.samples++;
                k++;
                acc = 0;
                n_in_block = 0;
                clip = false;
            }
        }
        /* Clock check. The newest reconstructed time trails the read time by
         * the DMA backlog (variable). The MINIMUM lag over each second is the
         * true offset between the ADC clock and the device clock; re-anchor
         * (visible as a timing break on the host) if it leaves [-2 ms, 20 ms]. */
        int64_t newest = anchor + (int64_t)(k * (1000000u / VG_EMG_OUT_HZ));
        int32_t drift = (int32_t)(now - newest);
        if (drift < min_drift) min_drift = drift;
        if (now - window_start >= 1000000) {
            s_stats.last_drift_us = min_drift;
            if (min_drift < -VG_EMG_REANCHOR_US || min_drift > 20000) {
                anchor += min_drift;
                s_stats.reanchors++;
            }
            min_drift = INT32_MAX;
            window_start = now;
        }
    }
}

bool vg_emg_adc_start(int gpio, vg_sample_ring_t *ring, char *detail, size_t n)
{
    adc_unit_t unit;
    if (adc_continuous_io_to_channel(gpio, &unit, &s_ch) != ESP_OK || unit != ADC_UNIT_1) {
        snprintf(detail, n, "GPIO%d is not an ADC1 pin", gpio);
        return false;
    }
    adc_continuous_handle_cfg_t hc = {.max_store_buf_size = FRAME_BYTES * 8,
                                      .conv_frame_size = FRAME_BYTES};
    if (adc_continuous_new_handle(&hc, &s_h) != ESP_OK) {
        snprintf(detail, n, "adc_continuous_new_handle failed");
        return false;
    }
    adc_digi_pattern_config_t pat = {.atten = ADC_ATTEN_DB_12, .channel = (uint8_t)s_ch,
                                     .unit = ADC_UNIT_1, .bit_width = 12};
    adc_continuous_config_t cc = {.pattern_num = 1, .adc_pattern = &pat,
                                  .sample_freq_hz = VG_EMG_CONV_HZ,
                                  .conv_mode = ADC_CONV_SINGLE_UNIT_1};
    adc_continuous_evt_cbs_t cbs = {.on_pool_ovf = on_ovf};
    if (adc_continuous_config(s_h, &cc) != ESP_OK ||
        adc_continuous_register_event_callbacks(s_h, &cbs, NULL) != ESP_OK ||
        adc_continuous_start(s_h) != ESP_OK) {
        snprintf(detail, n, "continuous ADC config/start failed");
        return false;
    }
    s_ring = ring;
    snprintf(detail, n, "ADC1_CH%d continuous %u Hz, 12 dB, mean of %u -> %u Hz", (int)s_ch,
             (unsigned)VG_EMG_CONV_HZ, (unsigned)DECIM, (unsigned)VG_EMG_OUT_HZ);
    return xTaskCreatePinnedToCore(emg_task, "vg_emg", 4096, NULL, configMAX_PRIORITIES - 2, NULL,
                                   1) == pdPASS;
}

const vg_emg_stats_t *vg_emg_adc_stats(void)
{
    return &s_stats;
}
