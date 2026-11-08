/* Station role (ESP32 #2): bounded haptic stimuli on host command.
 *
 * Protocol: firmware/contract/STATION_PROTOCOL.md (station command/event v1,
 * PROPOSED). UART0 (CP2102) at 115200 carries ONLY this protocol; the
 * console is disabled. One high-priority control task reads commands and
 * runs the stimulus timer, so abort and expiry are handled within ~1 ms.
 *
 * Motor: DRV2605L over I2C, only if the board config permits it
 * (vg_motor_output_permitted). Otherwise the controller is DISABLED and
 * every stimulus is rejected. The driver powers up in standby (TI SLOS854D)
 * and this firmware forces standby again at boot, on every stop path and on
 * any fault. No GPIO ever drives the motor.
 *
 * The trial sequence (staircase, responses) is the host's job (Fahad).
 */
#include <stdio.h>
#include <string.h>

#include "bootloader_random.h"
#include "driver/i2c_master.h"
#include "driver/uart.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "vg/pin_check.h"
#include "vg/station.h"
#include "vg_drv2605l.h"
#include "vg_target.h"
#include "vg_uart_tx.h"

#define LINK_UART UART_NUM_0
#define LINK_BAUD 115200   /* station link; wearable telemetry uses 921600 */

static vg_station_t s_st;
static vg_drv2605l_t s_drv;
static vg_fake_haptic_t s_fake;
static i2c_master_bus_handle_t s_bus;

static uint8_t s_fq_buf[8192];
static vg_frame_queue_t s_fq;

static void emit(void *ctx, const char *line, size_t len)
{
    (void)ctx;
    /* A dropped event (queue full) already has its seq: visible as a gap. */
    vg_fq_push(&s_fq, (const uint8_t *)line, (uint32_t)len, true);
}

static void control_task(void *arg)
{
    (void)arg;
    uint8_t buf[128];
    for (;;) {
        int n = uart_read_bytes(LINK_UART, buf, sizeof buf, pdMS_TO_TICKS(1));
        int64_t now = esp_timer_get_time();
        if (n > 0) vg_station_rx(&s_st, buf, (size_t)n, now);
        vg_station_tick(&s_st, esp_timer_get_time());
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
    static char device_id[32], boot_id[32];
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_BASE);
    snprintf(device_id, sizeof device_id, "vg-s-%02x%02x%02x%02x%02x%02x", mac[0], mac[1],
             mac[2], mac[3], mac[4], mac[5]);
    make_boot_id(boot_id, sizeof boot_id);
    vg_target_startup(&vg_board_station);   /* console disabled: result only via status */

    /* Events go through a bounded frame queue to the TX task (vg_uart_tx):
     * the control task never waits on the UART, so abort stays prompt. */
    vg_fq_init(&s_fq, s_fq_buf, sizeof s_fq_buf, 1024);
    if (!vg_uart_tx_start(&s_fq, LINK_BAUD)) {
        return;
    }

    const vg_hw_config_t *cfg = &vg_board_station;
    bool permitted = vg_motor_output_permitted(cfg) && cfg->motor.driver == VG_MOTOR_DRIVER_DRV2605L;
    vg_haptic_t hap;
    if (permitted) {
        i2c_master_bus_config_t bc = {.i2c_port = I2C_NUM_0, .sda_io_num = cfg->imu.sda_gpio,
                                      .scl_io_num = cfg->imu.scl_gpio,
                                      .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7,
                                      .flags.enable_internal_pullup = true};
        if (i2c_new_master_bus(&bc, &s_bus) == ESP_OK) {
            hap = vg_drv2605l_haptic(&s_drv, s_bus, cfg->motor.i2c_address, cfg->motor.od_clamp);
        } else {
            permitted = false;
            hap = vg_fake_haptic(&s_fake);   /* disabled; events labelled simulated */
        }
    } else {
        hap = vg_fake_haptic(&s_fake);       /* no confirmed driver: never drives hardware */
    }
    vg_st_limits_t lim = {.max_duration_ms = cfg->motor.max_pulse_ms ? cfg->motor.max_pulse_ms : 1000,
                          .heartbeat_timeout_ms = 1500, .hello_period_ms = 5000,
                          .fault_poll_ms = 50};
    if (lim.max_duration_ms > VG_ST_HARD_MAX_MS) lim.max_duration_ms = VG_ST_HARD_MAX_MS;
    vg_station_init(&s_st, device_id, boot_id, VG_FIRMWARE_VERSION, hap, lim, permitted, emit,
                    NULL, esp_timer_get_time());
    xTaskCreate(control_task, "vg_station", 6144, NULL, configMAX_PRIORITIES - 2, NULL);
}
