#include "vg_uart_tx.h"

#include <stdatomic.h>

#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "vg/link.h"

#define TX_PORT UART_NUM_0          /* GPIO1/3 -> CP2102 USB bridge */

static vg_frame_queue_t *s_fq;
static uint8_t s_frame[VG_LINK_FRAME_CAP];
static atomic_uint_fast64_t s_bytes;

static void tx_task(void *arg)
{
    (void)arg;
    for (;;) {
        uint32_t n = vg_fq_pop(s_fq, s_frame, sizeof s_frame);
        if (n == 0) {
            vTaskDelay(1);
            continue;
        }
        /* Blocks only this task until the bytes are in the driver's TX ring. */
        int w = uart_write_bytes(TX_PORT, s_frame, n);
        if (w > 0) {
            atomic_fetch_add(&s_bytes, (uint64_t)w);
        }
    }
}

bool vg_uart_tx_start(vg_frame_queue_t *fq, int baud)
{
    uart_config_t cfg = {
        .baud_rate = baud,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    if (uart_driver_install(TX_PORT, 256, 8192, 0, NULL, 0) != ESP_OK ||
        uart_param_config(TX_PORT, &cfg) != ESP_OK) {
        return false;
    }
    s_fq = fq;
    return xTaskCreate(tx_task, "vg_tx", 3072, NULL, 5, NULL) == pdPASS;
}

uint64_t vg_uart_tx_bytes(void)
{
    return atomic_load(&s_bytes);
}
