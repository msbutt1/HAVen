#include "vg_neopixel.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "driver/rmt_encoder.h"
#include "driver/rmt_tx.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define VG_LED_BRIGHTNESS 0.35f      /* of full scale; WS2812 at full white draws about 60 mA per LED */
#define VG_LED_MAX 16

static const char *TAG = "vg_led";
static rmt_channel_handle_t s_ch;
static rmt_encoder_handle_t s_enc;
static int s_count;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

typedef enum { MODE_NONE, MODE_OFF, MODE_SOLID, MODE_BREATHE } mode_t_;
static volatile mode_t_ s_mode = MODE_NONE;    /* NONE = no command yet */
static uint8_t s_a[3], s_b[3];                  /* RGB */
static uint32_t s_period_ms = 2400;
static volatile bool s_link;
static volatile int64_t s_last_cmd_us;

static bool hex(const char *p, uint8_t rgb[3])
{
    unsigned r, g, b;
    if (!p || strlen(p) < 6 || sscanf(p, "%2x%2x%2x", &r, &g, &b) != 3) return false;
    rgb[0] = (uint8_t)r; rgb[1] = (uint8_t)g; rgb[2] = (uint8_t)b;
    return true;
}

bool vg_neopixel_command(const char *line)
{
    char mode[12] = "", c1[8] = "", c2[8] = "";
    unsigned ms = 0;
    if (strncmp(line, "VGLED 1 ", 8) != 0) return false;
    int n = sscanf(line + 8, "%11s %7s %7s %u", mode, c1, c2, &ms);
    uint8_t a[3], b[3];
    taskENTER_CRITICAL(&s_mux);
    if (n >= 1 && strcmp(mode, "off") == 0) {
        s_mode = MODE_OFF;
    } else if (n >= 2 && strcmp(mode, "solid") == 0 && hex(c1, a)) {
        memcpy(s_a, a, 3); s_mode = MODE_SOLID;
    } else if (n >= 3 && strcmp(mode, "breathe") == 0 && hex(c1, a) && hex(c2, b)) {
        memcpy(s_a, a, 3); memcpy(s_b, b, 3);
        s_period_ms = (n >= 4 && ms >= 400 && ms <= 20000) ? ms : 2400;
        s_mode = MODE_BREATHE;
    }
    s_last_cmd_us = esp_timer_get_time();
    taskEXIT_CRITICAL(&s_mux);
    return true;
}

void vg_neopixel_link(bool connected) { s_link = connected; }

static void show(float r, float g, float b)
{
    uint8_t px[VG_LED_MAX * 3];
    uint8_t G = (uint8_t)(g * VG_LED_BRIGHTNESS), R = (uint8_t)(r * VG_LED_BRIGHTNESS), B = (uint8_t)(b * VG_LED_BRIGHTNESS);
    for (int i = 0; i < s_count; i++) { px[3 * i] = G; px[3 * i + 1] = R; px[3 * i + 2] = B; }   /* WS2812 order: GRB */
    rmt_transmit_config_t tc = {.loop_count = 0};
    if (rmt_transmit(s_ch, s_enc, px, (size_t)s_count * 3, &tc) == ESP_OK) rmt_tx_wait_all_done(s_ch, 20);
}

static void led_task(void *arg)
{
    (void)arg;
    for (;;) {
        float t = (float)(esp_timer_get_time() / 1000 % 1000000) / 1000.0f;   /* seconds */
        mode_t_ mode;
        uint8_t a[3], b[3];
        uint32_t period;
        taskENTER_CRITICAL(&s_mux);
        mode = s_mode; memcpy(a, s_a, 3); memcpy(b, s_b, 3); period = s_period_ms;
        bool fresh = esp_timer_get_time() - s_last_cmd_us < 15 * 1000000;   /* the phone repeats every few s */
        taskEXIT_CRITICAL(&s_mux);
        if (!s_link || mode == MODE_NONE || !fresh) {
            /* looking for the phone: dim blue, slow breathing */
            float k = 0.15f + 0.35f * (0.5f - 0.5f * cosf(2.0f * (float)M_PI * t / 3.0f));
            show(0, 60 * k, 255 * k);
        } else if (mode == MODE_OFF) {
            show(0, 0, 0);
        } else if (mode == MODE_SOLID) {
            show(a[0], a[1], a[2]);
        } else {
            float k = 0.5f - 0.5f * cosf(2.0f * (float)M_PI * t * 1000.0f / (float)period);
            show(a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k);
        }
        vTaskDelay(pdMS_TO_TICKS(20));   /* 50 Hz */
    }
}

bool vg_neopixel_start(int gpio, int count)
{
    s_count = count < 1 ? 1 : count > VG_LED_MAX ? VG_LED_MAX : count;
    rmt_tx_channel_config_t cfg = {
        .gpio_num = gpio, .clk_src = RMT_CLK_SRC_DEFAULT, .resolution_hz = 10 * 1000 * 1000,
        .mem_block_symbols = 64, .trans_queue_depth = 2,
    };
    /* WS2812 timing at 10 MHz: 0 = 0.3 us high + 0.9 us low, 1 = 0.9 us high + 0.3 us low, MSB first */
    rmt_bytes_encoder_config_t bc = {
        .bit0 = {.level0 = 1, .duration0 = 3, .level1 = 0, .duration1 = 9},
        .bit1 = {.level0 = 1, .duration0 = 9, .level1 = 0, .duration1 = 3},
        .flags.msb_first = 1,
    };
    if (rmt_new_tx_channel(&cfg, &s_ch) != ESP_OK || rmt_new_bytes_encoder(&bc, &s_enc) != ESP_OK ||
        rmt_enable(s_ch) != ESP_OK) {
        ESP_LOGE(TAG, "RMT setup on GPIO %d failed", gpio);
        return false;
    }
    ESP_LOGI(TAG, "status light: %d WS2812 on GPIO %d", s_count, gpio);
    /* low priority, on the acquisition-free core with the network */
    return xTaskCreatePinnedToCore(led_task, "vg_led", 3072, NULL, 2, NULL, 0) == pdPASS;
}
