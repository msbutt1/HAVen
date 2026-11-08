#define _POSIX_C_SOURCE 200809L
/* vg-fake-station: the station controller (same code as the ESP32 build) with
 * the FAKE haptic driver, speaking station protocol v1 on stdin/stdout in real
 * time. Every event is labelled "simulated". For host-side development
 * (Fahad's test engine, Abduljabbar's adapter) before or without hardware.
 *
 *   vg-fake-station [--max-ms N] [--heartbeat-ms N] [--fault-after-ms N]
 * Fake motor state changes are logged to stderr. */
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "vg/station.h"

static int64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

static void emit(void *ctx, const char *line, size_t len)
{
    (void)ctx;
    fwrite(line, 1, len, stdout);
    fflush(stdout);
}

int main(int argc, char **argv)
{
    vg_st_limits_t lim = {1000, 1500, 5000, 50};
    long fault_after_ms = -1;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (!strcmp(argv[i], "--max-ms")) lim.max_duration_ms = (uint32_t)atol(argv[i + 1]);
        else if (!strcmp(argv[i], "--heartbeat-ms")) lim.heartbeat_timeout_ms = (uint32_t)atol(argv[i + 1]);
        else if (!strcmp(argv[i], "--fault-after-ms")) fault_after_ms = atol(argv[i + 1]);
        else { fprintf(stderr, "unknown option %s\n", argv[i]); return 2; }
    }
    static vg_fake_haptic_t fk;
    static vg_station_t st;
    vg_haptic_t h = vg_fake_haptic(&fk);
    char boot[32];
    snprintf(boot, sizeof boot, "fake-%08lx", (unsigned long)(now_us() & 0xffffffff));
    int64_t t0 = now_us();
    if (!vg_station_init(&st, "fake-station-01", boot, "0.3.0-fake", h, lim, true, emit, NULL, t0)) {
        fprintf(stderr, "invalid limits\n");
        return 2;
    }
    bool was_on = false;
    struct pollfd p = {STDIN_FILENO, POLLIN, 0};
    for (;;) {
        int r = poll(&p, 1, 1);
        int64_t t = now_us();
        if (r > 0) {
            uint8_t buf[256];
            ssize_t n = read(STDIN_FILENO, buf, sizeof buf);
            if (n == 0) break;                         /* EOF: host closed the link */
            if (n > 0) vg_station_rx(&st, buf, (size_t)n, t);
            else if (errno != EINTR) break;
        }
        if (fault_after_ms >= 0 && t - t0 >= fault_after_ms * 1000) fk.fail_check = true;
        vg_station_tick(&st, now_us());
        if (fk.output_on != was_on) {
            fprintf(stderr, "[fake motor] %s level=%u\n", fk.output_on ? "ON " : "OFF", fk.level);
            was_on = fk.output_on;
        }
    }
    vg_station_stop(&st, VG_OUT_LINK_TIMEOUT, now_us());   /* stdin closed: output off */
    return 0;
}
