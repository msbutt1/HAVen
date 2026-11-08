/* Wearable role (ESP32 #1, board W): LIVE EMG + IMU acquisition -> device
 * telemetry v1 -> Wi-Fi (persistent TCP to the laptop bridge).
 *
 *   core 1  EMG task: BioAmp OUT on GPIO34, ADC1 continuous (DMA) at 20 kHz,
 *             averaged in blocks of 20 -> 1 kHz; timestamps from the
 *             conversion count (hardware-paced, see vg_emg_adc.h)
 *   core 1  IMU task, 1 ms poll: MPU-6500 on I2C GPIO18/19 @400 kHz, burst
 *             read of INT_STATUS + accel; pushed ONLY when data-ready says
 *             it is new; timestamps reconstructed from the IMU's own clock
 *   any     serializer task, every 10 ms: rings -> batches -> NDJSON -> queue
 *   core 0  Wi-Fi task: queue -> TCP (vg_wifi_link)
 *
 * Timestamps are on the esp_timer device clock (reconstructed as above). Samples
 * are raw counts; the host converts and processes (signals/). Provenance is
 * "live" because these batches come from real sensors; there is no fallback
 * to fake data. Console (115200) carries human-readable diagnostics only.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bootloader_random.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "vg/link.h"
#include "vg/pin_check.h"
#include "vg/clock_est.h"
#include "vg_emg_adc.h"
#include "vg_mpu6500.h"
#include "vg_target.h"
#include "vg_wifi_link.h"
#include "wifi_secrets.h"

#define EMG_RATE_HZ 1000.0
#define IMU_STALE_US 50000          /* no fresh IMU sample for 50 ms (25 periods) -> stale */
#define IMU_FAIL_LIMIT 50           /* consecutive I2C failures -> disconnected */

static const char *TAG = "wearable";
static const char *const EMG_CH[] = {"emg0"};
static const char *const IMU_CH[] = {"ax", "ay", "az"};

static vg_sample_t s_emg_slots[512], s_imu_slots[256];   /* 0.5 s each; drained every 10 ms */
static vg_sample_ring_t s_emg_ring, s_imu_ring;
static vg_batcher_t s_emg_b, s_imu_b;
/* Frame queue on the heap: 64 KiB = ~2 s at ~32 kB/s, rides out Wi-Fi stalls
 * (phone hotspot pauses of 250-500 ms measured; 16 KiB dropped 610 records). */
static uint8_t *s_fq_buf;
static vg_frame_queue_t s_fq;
static vg_link_t s_link;
static vg_mpu_t s_mpu;
static bool s_emg_on, s_imu_on;
static volatile vg_sensor_state_t s_imu_state = VG_SENSOR_UNKNOWN;
static volatile uint32_t s_imu_fresh, s_imu_polls;

/* IMU task (core 1): poll INT_STATUS every 1 ms; push a sample only when the
 * data-ready flag says it is new. Its timestamp is reconstructed from the
 * IMU's own sample clock (vg_clock_est), locked to the read times, so polling
 * jitter does not appear as sampling jitter. */
static vg_clock_est_t s_imu_clk;

static char s_imu_detail[120];
static bool s_imu_ready;

static void imu_task(void *arg)
{
    (void)arg;
    const vg_hw_config_t *cfg = &vg_board_wearable;
    TickType_t last = xTaskGetTickCount();
    uint32_t fails = 0;
    int64_t last_fresh = esp_timer_get_time();
    for (;;) {
        if (!s_imu_ready) {
            /* Not initialised (absent, unpowered, bus error at boot): retry every
             * second; the stream stays declared and reports "disconnected". */
            s_imu_ready = vg_mpu_init(&s_mpu, cfg->imu.sda_gpio, cfg->imu.scl_gpio,
                                      cfg->imu.address, cfg->imu.clock_hz, s_imu_detail,
                                      sizeof s_imu_detail);
            ESP_LOGI(TAG, "IMU init %s: %s", s_imu_ready ? "OK" : "failed, retrying", s_imu_detail);
            if (!s_imu_ready) {
                s_imu_state = VG_SENSOR_DISCONNECTED;
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }
            last = xTaskGetTickCount();
            last_fresh = esp_timer_get_time();
            fails = 0;
        }
        vTaskDelayUntil(&last, 1);                       /* 1 ms (CONFIG_FREERTOS_HZ=1000) */
        bool fresh = false;
        int16_t xyz[3];
        int64_t t = esp_timer_get_time();
        s_imu_polls++;
        if (vg_mpu_read(&s_mpu, &fresh, xyz)) {
            fails = 0;
            if (fresh) {
                vg_sample_t s = {0};
                s.t_us = vg_clock_est_update(&s_imu_clk, t);
                s.v[0] = xyz[0];
                s.v[1] = xyz[1];
                s.v[2] = xyz[2];
                vg_ring_push(&s_imu_ring, &s);
                s_imu_fresh++;
                last_fresh = t;
                s_imu_state = VG_SENSOR_OK;
            } else if (t - last_fresh > IMU_STALE_US) {
                s_imu_state = VG_SENSOR_STALE;
            }
        } else if (++fails >= IMU_FAIL_LIMIT) {
            s_imu_state = VG_SENSOR_DISCONNECTED;
            if (fails >= 10 * IMU_FAIL_LIMIT) {
                s_imu_ready = false;                     /* sustained failure: re-initialise */
            }
        }
    }
}

static void serializer_task(void *arg)
{
    (void)arg;
    TickType_t last = xTaskGetTickCount();
    int64_t next_log = esp_timer_get_time() + 5000000;
    for (;;) {
        int64_t now = esp_timer_get_time();
        if (s_imu_on) vg_link_set_sensor(&s_link, VG_STREAM_IMU, s_imu_state, now);
        vg_link_step(&s_link, now);
        if (now >= next_log) {
            const vg_wifi_stats_t *w = vg_wifi_link_stats();
            const vg_emg_stats_t *e = vg_emg_adc_stats();
            ESP_LOGI(TAG, "seq=%" PRIu64 " wifi=%d tcp=%d bridge=%s sent=%" PRIu64 "B "
                     "offline_drops=%" PRIu64 " emg_drop=%" PRIu64 " imu_drop=%" PRIu64
                     " imu_fresh=%" PRIu32 "/%" PRIu32 " polls imu_resync=%" PRIu32
                     " emg=%" PRIu32 " clip=%" PRIu32 " drift=%" PRId32 "us reanchor=%" PRIu32,
                     s_link.tm.next_seq, w->wifi_up, w->connected, w->bridge_ip, w->bytes_sent,
                     w->frames_dropped_offline, vg_ring_dropped(&s_emg_ring),
                     vg_ring_dropped(&s_imu_ring), s_imu_fresh, s_imu_polls, s_imu_clk.resyncs,
                     e->samples, e->clipped, e->last_drift_us, e->reanchors);
            next_log = now + 5000000;
        }
        vTaskDelayUntil(&last, pdMS_TO_TICKS(10));
    }
}

static void make_boot_id(char *out, size_t n)
{
    uint8_t r[8];
    bootloader_random_enable();
    esp_fill_random(r, sizeof r);
    bootloader_random_disable();
    snprintf(out, n, "b-%02x%02x%02x%02x%02x%02x%02x%02x", r[0], r[1], r[2], r[3], r[4], r[5],
             r[6], r[7]);
}

void app_main(void)
{
    static char device_id[32], boot_id[32], imu_detail[120], problems_s[24];
    const vg_hw_config_t *cfg = &vg_board_wearable;
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_BASE);
    snprintf(device_id, sizeof device_id, "vg-w-%02x%02x%02x%02x%02x%02x", mac[0], mac[1],
             mac[2], mac[3], mac[4], mac[5]);
    make_boot_id(boot_id, sizeof boot_id);      /* before the ADC and Wi-Fi start */
    int problems = vg_target_startup(cfg);
    snprintf(problems_s, sizeof problems_s, "%d", problems);

    /* ---- EMG: ADC1 continuous (hardware-paced), started after the rings ---- */
    static char emg_detail[96] = "disabled";
    /* ---- IMU: initialised (and re-initialised) by its task ---- */
    const double imu_rate = 500.0;   /* fixed by the acquisition setup in vg_mpu6500.c, read back */
    s_imu_on = cfg->imu.enabled && problems == 0;
    if (s_imu_on) {
        s_imu_ready = vg_mpu_init(&s_mpu, cfg->imu.sda_gpio, cfg->imu.scl_gpio, cfg->imu.address,
                                  cfg->imu.clock_hz, s_imu_detail, sizeof s_imu_detail);
        if (s_imu_ready && s_mpu.derived.output_rate_hz != imu_rate) {
            snprintf(s_imu_detail, sizeof s_imu_detail, "unexpected ODR %.0f Hz",
                     s_mpu.derived.output_rate_hz);
            s_imu_ready = false;
        }
    }
    snprintf(imu_detail, sizeof imu_detail, "%s", s_imu_on ? s_imu_detail : "disabled");
    vg_ring_init(&s_emg_ring, s_emg_slots, 512);
    vg_ring_init(&s_imu_ring, s_imu_slots, 256);
    vg_clock_est_init(&s_imu_clk, 1e6 / imu_rate);
    if (cfg->emg.enabled && problems == 0) {
        s_emg_on = vg_emg_adc_start(cfg->emg.gpio, &s_emg_ring, emg_detail, sizeof emg_detail);
    }
    ESP_LOGI(TAG, "device %s boot %s | EMG %s: %s | IMU %s (%s): %s", device_id, boot_id,
             s_emg_on ? "on" : "OFF", emg_detail, s_imu_on ? "on" : "OFF",
             s_imu_ready ? "ready" : "retrying", imu_detail);
    vg_batcher_cfg_t ec = {VG_STREAM_EMG, VG_SRC_LIVE, 1, EMG_CH, "adc_counts", EMG_RATE_HZ,
                           50, 256, 100000, 0.25};
    uint16_t imu_rows = (uint16_t)(imu_rate * 0.05 + 0.5);
    vg_batcher_cfg_t ic = {VG_STREAM_IMU, VG_SRC_LIVE, 3, IMU_CH, "raw_counts", imu_rate,
                           imu_rows ? imu_rows : 1, 256, 100000, 0.25};
    vg_batcher_init(&s_emg_b, &ec, &s_emg_ring);
    vg_batcher_init(&s_imu_b, &ic, &s_imu_ring);
    uint32_t fq_size = 65536;
    while (fq_size >= 8192 && (s_fq_buf = malloc(fq_size)) == NULL) fq_size /= 2;
    if (s_fq_buf == NULL || !vg_fq_init(&s_fq, s_fq_buf, fq_size, 2048)) {
        ESP_LOGE(TAG, "no memory for the frame queue");
        return;
    }
    ESP_LOGI(TAG, "frame queue %" PRIu32 " bytes", fq_size);

    vg_tm_t tm;
    if (vg_tm_init(&tm, device_id, boot_id, VG_SRC_LIVE, NULL) != VG_TM_OK) return;
    static vg_tm_kv_t hw[6];
    hw[0] = (vg_tm_kv_t){"board", VG_BOARD_NAME};
    hw[1] = (vg_tm_kv_t){"acquisition", "LIVE: BioAmp ADC + IMU accelerometer"};
    hw[2] = (vg_tm_kv_t){"imu", imu_detail};
    hw[3] = (vg_tm_kv_t){"imu_identity_source", "WHO_AM_I 0x70 = MPU-6500 from secondary sources"};
    hw[4] = (vg_tm_kv_t){"emg_frontend",
                         "BioAmp EXG Pill v1.0 at 3V3; BANDPASS jumper default (EEG/EOG band)"};
    hw[5] = (vg_tm_kv_t){"config_problems", problems_s};
    vg_tm_hello_t hello = {
        .fw_name = VG_FIRMWARE_NAME,
        .fw_version = VG_FIRMWARE_VERSION,
        .emg = {s_emg_on, EMG_RATE_HZ, 1, EMG_CH, "adc_counts", true, 0, 4095,
                "BioAmp EXG Pill v1.0 via ESP32 ADC1_CH6, 20 kHz continuous averaged to 1 kHz"},
        .imu = {s_imu_on, imu_rate, 3, IMU_CH, "raw_counts", true, -32768, 32767,
                "MPU-6500 accelerometer, +-8 g (4096 LSB/g), DLPF 218.1 Hz, 500 Hz"},
        .hardware = hw,
        .n_hardware = 6,
    };
    if (!s_emg_on && !s_imu_on) {
        ESP_LOGE(TAG, "no sensor available: nothing to stream");
        return;
    }
    if (!vg_link_init(&s_link, &tm, &hello, s_emg_on ? &s_emg_b : NULL,
                      s_imu_on ? &s_imu_b : NULL, &s_fq, 1000000, 5000000)) {
        return;
    }
    /* An ADC value cannot prove the electrodes are on the skin: EMG stays unknown. */
    vg_link_set_sensor(&s_link, VG_STREAM_EMG, VG_SENSOR_UNKNOWN, esp_timer_get_time());

    vg_wifi_cfg_t wc = {VG_WIFI_SSID, VG_WIFI_PASSWORD, VG_BRIDGE_HOST, 8765, 8766, device_id};
    if (!vg_wifi_link_start(&wc, &s_fq, &s_link.resend_hello)) {
        ESP_LOGE(TAG, "Wi-Fi start failed");
        return;
    }
    if (s_imu_on) {
        xTaskCreatePinnedToCore(imu_task, "vg_imu", 4096, NULL, configMAX_PRIORITIES - 3, NULL, 1);
    }
    xTaskCreatePinnedToCore(serializer_task, "vg_ser", 6144, NULL, 6, NULL, 1);
}
