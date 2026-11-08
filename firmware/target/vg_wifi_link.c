#include "vg_wifi_link.h"

#include <string.h>

#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"
#include "vg/link.h"

#define GOT_IP BIT0

static vg_wifi_cfg_t s_cfg;
static vg_frame_queue_t *s_fq;
static volatile bool *s_hello_req;
static vg_wifi_stats_t s_stats;
static EventGroupHandle_t s_ev;
static uint8_t s_frame[VG_LINK_FRAME_CAP];

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_stats.wifi_up = false;
        xEventGroupClearBits(s_ev, GOT_IP);
        esp_wifi_connect();                       /* keep retrying */
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        s_stats.wifi_up = true;
        xEventGroupSetBits(s_ev, GOT_IP);
    }
}

/* Broadcast a discovery request; return true and fill ip/port on a reply. */
static bool discover(char *ip, uint16_t *port)
{
    int s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s < 0) return false;
    int yes = 1;
    setsockopt(s, SOL_SOCKET, SO_BROADCAST, &yes, sizeof yes);
    struct timeval tv = {.tv_sec = 0, .tv_usec = 500000};
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    struct sockaddr_in dst = {.sin_family = AF_INET, .sin_port = htons(s_cfg.discovery_port),
                              .sin_addr.s_addr = htonl(INADDR_BROADCAST)};
    char msg[96];
    int n = snprintf(msg, sizeof msg, "VGDISCOVER 1 %s", s_cfg.device_id);
    sendto(s, msg, (size_t)n, 0, (struct sockaddr *)&dst, sizeof dst);
    char rx[64];
    struct sockaddr_in src;
    socklen_t sl = sizeof src;
    int r = recvfrom(s, rx, sizeof rx - 1, 0, (struct sockaddr *)&src, &sl);
    close(s);
    if (r <= 0) return false;
    rx[r] = '\0';
    unsigned p = 0;
    if (sscanf(rx, "VGBRIDGE 1 %u", &p) != 1 || p == 0 || p > 65535) return false;
    inet_ntoa_r(src.sin_addr, ip, 16);
    *port = (uint16_t)p;
    return true;
}

/* The Wi-Fi gateway's address. When the phone itself is the hotspot, the phone is the
 * gateway: the VibeGuard app listens on tcp_port there (iOS apps cannot receive the
 * discovery broadcast without a special entitlement, so the app never answers it). */
static bool gateway(char *ip)
{
    esp_netif_t *nif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t info;
    if (!nif || esp_netif_get_ip_info(nif, &info) != ESP_OK || info.gw.addr == 0) return false;
    esp_ip4addr_ntoa(&info.gw, ip, 16);
    return true;
}

static int open_tcp(const char *ip, uint16_t port)
{
    int s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s < 0) return -1;
    int yes = 1;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof yes);
    struct timeval tv = {.tv_sec = 1, .tv_usec = 0};
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(port)};
    inet_aton(ip, &a.sin_addr);
    if (connect(s, (struct sockaddr *)&a, sizeof a) != 0) {
        close(s);
        return -1;
    }
    return s;
}

static bool send_all(int s, const uint8_t *p, size_t n)
{
    while (n) {
        int w = send(s, p, n, 0);
        if (w <= 0) return false;
        p += w;
        n -= (size_t)w;
        s_stats.bytes_sent += (uint64_t)w;
    }
    return true;
}

static void drain_offline(void)
{
    /* Not connected: discard queued frames so the queue keeps flowing; each
     * already has its seq, so the host sees exactly how much was lost. */
    while (vg_fq_pop(s_fq, s_frame, sizeof s_frame) > 0) s_stats.frames_dropped_offline++;
}

static void net_task(void *arg)
{
    (void)arg;
    for (;;) {
        if (!(xEventGroupGetBits(s_ev) & GOT_IP)) {
            drain_offline();
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        char ip[16] = "";
        uint16_t port = s_cfg.tcp_port;
        if (s_cfg.fixed_host && s_cfg.fixed_host[0]) {
            strncpy(ip, s_cfg.fixed_host, sizeof ip - 1);
        } else if (!discover(ip, &port)) {
            /* No laptop bridge answered: try the phone app at the gateway. A laptop
             * bridge, when present, always wins because discovery is tried first. */
            port = s_cfg.tcp_port;
            if (!gateway(ip)) {
                drain_offline();
                continue;                          /* discover() waited 500 ms */
            }
        }
        int s = open_tcp(ip, port);
        if (s < 0) {
            drain_offline();
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        strncpy(s_stats.bridge_ip, ip, sizeof s_stats.bridge_ip - 1);
        s_stats.connected = true;
        s_stats.connects++;
        drain_offline();                           /* stale backlog: start fresh */
        *s_hello_req = true;                       /* hello + status right away */
        for (;;) {
            uint32_t n = vg_fq_pop(s_fq, s_frame, sizeof s_frame);
            if (n == 0) {
                vTaskDelay(1);
                if (!(xEventGroupGetBits(s_ev) & GOT_IP)) break;
                continue;
            }
            if (!send_all(s, s_frame, n)) break;
        }
        s_stats.connected = false;
        close(s);
    }
}

bool vg_wifi_link_start(const vg_wifi_cfg_t *cfg, vg_frame_queue_t *fq,
                        volatile bool *hello_request)
{
    s_cfg = *cfg;
    s_fq = fq;
    s_hello_req = hello_request;
    s_ev = xEventGroupCreate();

    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        e = nvs_flash_init();
    }
    if (e != ESP_OK || esp_netif_init() != ESP_OK || esp_event_loop_create_default() != ESP_OK) {
        return false;
    }
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t wc = WIFI_INIT_CONFIG_DEFAULT();
    if (esp_wifi_init(&wc) != ESP_OK) return false;
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL, NULL);
    wifi_config_t sta = {0};
    strncpy((char *)sta.sta.ssid, cfg->ssid, sizeof sta.sta.ssid - 1);
    strncpy((char *)sta.sta.password, cfg->password, sizeof sta.sta.password - 1);
    sta.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    if (esp_wifi_set_mode(WIFI_MODE_STA) != ESP_OK ||
        esp_wifi_set_config(WIFI_IF_STA, &sta) != ESP_OK || esp_wifi_start() != ESP_OK) {
        return false;
    }
    esp_wifi_set_ps(WIFI_PS_NONE);                 /* no modem sleep: lowest latency */
    /* network on core 0 (with the Wi-Fi stack); acquisition runs on core 1 */
    return xTaskCreatePinnedToCore(net_task, "vg_net", 4096, NULL, 4, NULL, 0) == pdPASS;
}

const vg_wifi_stats_t *vg_wifi_link_stats(void)
{
    return &s_stats;
}
