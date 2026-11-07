/* TRANSMISSION stage on ESP32: a task that drains the frame queue to a UART.
 * Only this task ever waits on the UART; acquisition and serialization never
 * do. When the UART cannot keep up, the frame queue fills and the serializer
 * drops whole frames (counted, visible as seq gaps on the host). */
#ifndef VG_UART_TX_H
#define VG_UART_TX_H

#include <stdbool.h>
#include <stdint.h>

#include "vg/frame_queue.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Telemetry link rate on UART0 (CP2102 bridge on both boards). 921600 is the
 * CP2102 datasheet maximum; the reliable rate on these boards is still to be
 * measured at bring-up. */
#define VG_TELEMETRY_UART_BAUD 921600

bool vg_uart_tx_start(vg_frame_queue_t *fq, int baud);
uint64_t vg_uart_tx_bytes(void);

#ifdef __cplusplus
}
#endif
#endif
