/* Wi-Fi transport for device telemetry v1 (wearable).
 *
 * - Joins a 2.4 GHz WPA2 network as a station (power save off: lowest latency).
 * - Finds the laptop bridge automatically: UDP broadcast
 *   "VGDISCOVER 1 <device_id>" to port disc_port; the bridge answers
 *   "VGBRIDGE 1 <tcp_port>" and its source address is used. A fixed host
 *   can be configured instead.
 * - Streams NDJSON frames from the frame queue over ONE persistent TCP
 *   connection (TCP_NODELAY). On (re)connect it raises *hello_request so the
 *   serializer immediately sends hello + status with the next seq.
 * - While disconnected, frames are dropped (counted; visible as seq gaps).
 *   Acquisition never waits for the network.
 */
#ifndef VG_WIFI_LINK_H
#define VG_WIFI_LINK_H

#include <stdbool.h>
#include <stdint.h>

#include "vg/frame_queue.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    const char *ssid;
    const char *password;
    const char *fixed_host;     /* NULL or "" = discover by UDP broadcast */
    uint16_t tcp_port;          /* default bridge port 8765 */
    uint16_t discovery_port;    /* default 8766 */
    const char *device_id;
} vg_wifi_cfg_t;

typedef struct {
    volatile bool wifi_up;
    volatile bool connected;
    volatile uint32_t connects;
    volatile uint64_t bytes_sent;
    volatile uint64_t frames_dropped_offline;
    char bridge_ip[16];
} vg_wifi_stats_t;

bool vg_wifi_link_start(const vg_wifi_cfg_t *cfg, vg_frame_queue_t *fq,
                        volatile bool *hello_request);
const vg_wifi_stats_t *vg_wifi_link_stats(void);

#ifdef __cplusplus
}
#endif
#endif
