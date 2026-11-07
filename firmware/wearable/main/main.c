/* Wearable role (ESP32 #1): EMG + IMU acquisition -> device telemetry v1 on UART0.
 *
 * Prompt 2: FAKE acquisition only. Every record is provenance "simulated" and
 * the hello says so. Real BioAmp/IMU acquisition (Prompt 4) will be a separate
 * adapter whose batchers carry VG_SRC_LIVE; the link refuses to mix sources.
 *
 * Stages (each owns its own state; none waits on the next):
 *   acquisition   esp_timer callbacks push samples into bounded rings
 *   serialization task every 10 ms: rings -> batches -> NDJSON -> frame queue
 *   transmission  task: frame queue -> UART0 (921600 baud, CP2102)
 * The console is disabled in sdkconfig.defaults: UART0 carries only telemetry.
 */
#include <stdio.h>
#include <string.h>

#include "bootloader_random.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "vg/fake_source.h"
#include "vg/link.h"
#include "vg/pin_check.h"
#include "vg_target.h"
#include "vg_uart_tx.h"

#define EMG_RATE_HZ 1000.0       /* fake source rates; real rates come from confirmed hardware */
#define IMU_RATE_HZ 500.0

static const char *const EMG_CH[] = {"emg0"};
static const char *const IMU_CH[] = {"ax", "ay", "az"};

static vg_sample_t s_emg_slots[1024], s_imu_slots[512];
static vg_sample_ring_t s_emg_ring, s_imu_ring;
static vg_batcher_t s_emg_b, s_imu_b;
static uint8_t s_fq_buf[32768];
static vg_frame_queue_t s_fq;
static vg_link_t s_link;
static vg_fake_emg_t s_femg;
static vg_fake_imu_t s_fimu;

/* ---- acquisition (fake): never blocks; a full ring drops and counts ---- */
static void emg_tick(void *arg)
{
    (void)arg;
    vg_sample_t s = {0};
    s.t_us = esp_timer_get_time();   /* 64-bit µs since boot: no wrap handling needed */
    s.v[0] = vg_fake_emg_next(&s_femg);
    vg_ring_push(&s_emg_ring, &s);
}

static void imu_tick(void *arg)
{
    (void)arg;
    vg_sample_t s = {0};
    s.t_us = esp_timer_get_time();
    vg_fake_imu_next(&s_fimu, s.v);
    vg_ring_push(&s_imu_ring, &s);
}

static void start_timer(esp_timer_cb_t cb, const char *name, double rate_hz)
{
    esp_timer_create_args_t a = {.callback = cb, .name = name};
    esp_timer_handle_t h;
    ESP_ERROR_CHECK(esp_timer_create(&a, &h));
    ESP_ERROR_CHECK(esp_timer_start_periodic(h, (uint64_t)(1e6 / rate_hz)));
}

/* ---- serialization ---- */
static void serializer_task(void *arg)
{
    (void)arg;
    TickType_t last = xTaskGetTickCount();
    for (;;) {
        vg_link_step(&s_link, esp_timer_get_time());
        vTaskDelayUntil(&last, pdMS_TO_TICKS(10));
    }
}

/* Fresh boot_id on every boot: hardware RNG with the bootloader entropy source
 * enabled (ESP-IDF v6.1 "Random Number Generation": esp_random is only
 * pseudo-random with RF off unless this source is enabled). Must run before
 * any ADC use. */
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
    static char device_id[32], boot_id[32], problems_s[24];
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_BASE);
    snprintf(device_id, sizeof device_id, "vg-w-%02x%02x%02x%02x%02x%02x", mac[0], mac[1],
             mac[2], mac[3], mac[4], mac[5]);
    make_boot_id(boot_id, sizeof boot_id);

    /* Startup report goes to the (disabled) console; its result goes in hello. */
    int problems = vg_target_startup(&vg_board_wearable);
    snprintf(problems_s, sizeof problems_s, "%d", problems);

    vg_ring_init(&s_emg_ring, s_emg_slots, 1024);
    vg_ring_init(&s_imu_ring, s_imu_slots, 512);
    vg_batcher_cfg_t ec = {VG_STREAM_EMG, VG_SRC_SIMULATED, 1, EMG_CH, "adc_counts", EMG_RATE_HZ,
                           50, 256, 100000, 0.25};
    vg_batcher_cfg_t ic = {VG_STREAM_IMU, VG_SRC_SIMULATED, 3, IMU_CH, "raw_counts", IMU_RATE_HZ,
                           25, 256, 100000, 0.25};
    vg_batcher_init(&s_emg_b, &ec, &s_emg_ring);
    vg_batcher_init(&s_imu_b, &ic, &s_imu_ring);
    vg_fq_init(&s_fq, s_fq_buf, sizeof s_fq_buf, 2048);

    vg_tm_t tm;
    if (vg_tm_init(&tm, device_id, boot_id, VG_SRC_SIMULATED, NULL) != VG_TM_OK) {
        return;
    }
    static const char *const unknown = NULL;
    static vg_tm_kv_t hw[6];
    hw[0] = (vg_tm_kv_t){"board", VG_BOARD_NAME};
    hw[1] = (vg_tm_kv_t){"acquisition", "FAKE: synthetic samples, no sensor read (SIMULATED)"};
    hw[2] = (vg_tm_kv_t){"config_problems", problems_s};
    hw[3] = (vg_tm_kv_t){"emg_frontend", unknown};
    hw[4] = (vg_tm_kv_t){"imu_chip", unknown};
    hw[5] = (vg_tm_kv_t){"link", "UART0 921600 8N1 via CP2102"};
    vg_tm_hello_t hello = {
        .fw_name = VG_FIRMWARE_NAME,
        .fw_version = VG_FIRMWARE_VERSION,
        .emg = {true, EMG_RATE_HZ, 1, EMG_CH, "adc_counts", true, 0, 4095,
                "fake EMG source (SIMULATED; synthetic counts)"},
        .imu = {true, IMU_RATE_HZ, 3, IMU_CH, "raw_counts", true, -32768, 32767,
                "fake accelerometer source (SIMULATED; synthetic counts)"},
        .hardware = hw,
        .n_hardware = 6,
    };
    if (!vg_link_init(&s_link, &tm, &hello, &s_emg_b, &s_imu_b, &s_fq, 1000000, 5000000)) {
        return;
    }
    /* The fake sources are always "ok"; a real adapter reports evidence or unknown. */
    vg_link_set_sensor(&s_link, VG_STREAM_EMG, VG_SENSOR_OK, esp_timer_get_time());
    vg_link_set_sensor(&s_link, VG_STREAM_IMU, VG_SENSOR_OK, esp_timer_get_time());

    vg_fake_emg_init(&s_femg, 1);
    vg_fake_imu_init(&s_fimu, 2);
    if (!vg_uart_tx_start(&s_fq, VG_TELEMETRY_UART_BAUD)) {
        return;
    }
    xTaskCreate(serializer_task, "vg_ser", 6144, NULL, 6, NULL);
    start_timer(emg_tick, "vg_emg", EMG_RATE_HZ);
    start_timer(imu_tick, "vg_imu", IMU_RATE_HZ);
}
