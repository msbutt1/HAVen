/* vg-fake-device: deterministic SIMULATED wearable built from the firmware's
 * own shared code (rings, batcher, encoder, link, frame queue). Emits
 * contract-v1 NDJSON on stdout; summary on stderr. Provenance is always
 * "simulated": this program has no way to emit live records.
 *
 * Simulated device time advances in 1 ms ticks. Options model the real
 * firmware's failure modes:
 *   --baud N          transmit budget N/10 bytes/s (8N1); 0 = unlimited
 *   --stall A:B       transmitter accepts nothing between A and B seconds
 *   --pause A:B       serializer does not run (acquisition continues)
 *   --disconnect A:B  EMG sensor reports disconnected (values null)
 *   --nulls A:B       IMU channel ay unavailable (null) in this window
 *   --imu-jitter US   deterministic +-US jitter on IMU timestamps
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "vg/fake_source.h"
#include "vg/link.h"

typedef struct {
    double a, b;
} window_t;

static bool in(window_t w, double t) { return t >= w.a && t < w.b; }

static window_t parse_window(const char *s)
{
    window_t w = {-1, -1};
    if (sscanf(s, "%lf:%lf", &w.a, &w.b) != 2 || w.b < w.a) {
        fprintf(stderr, "bad window '%s' (want A:B seconds)\n", s);
        exit(2);
    }
    return w;
}

static const char *const EMG_CH[] = {"emg0"};
static const char *const IMU_CH[] = {"ax", "ay", "az"};

int main(int argc, char **argv)
{
    double seconds = 10, emg_rate = 1000, imu_rate = 500, batch_ms = 50;
    long long baud = 0;
    unsigned seed = 1;
    int imu_jitter = 0;
    uint32_t ring_cap = 1024;
    const char *device_id = "fake-wearable-01", *boot_id = "fake-boot-0001";
    window_t stall = {-1, -1}, pause = {-1, -1}, disc = {-1, -1}, nulls = {-1, -1};

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = i + 1 < argc ? argv[i + 1] : NULL;
#define OPT(name) (strcmp(a, name) == 0 && v && ++i)
        if (OPT("--seconds")) seconds = atof(v);
        else if (OPT("--seed")) seed = (unsigned)strtoul(v, NULL, 10);
        else if (OPT("--emg-rate")) emg_rate = atof(v);
        else if (OPT("--imu-rate")) imu_rate = atof(v);
        else if (OPT("--batch-ms")) batch_ms = atof(v);
        else if (OPT("--baud")) baud = atoll(v);
        else if (OPT("--ring")) ring_cap = (uint32_t)strtoul(v, NULL, 10);
        else if (OPT("--device-id")) device_id = v;
        else if (OPT("--boot-id")) boot_id = v;
        else if (OPT("--stall")) stall = parse_window(v);
        else if (OPT("--pause")) pause = parse_window(v);
        else if (OPT("--disconnect")) disc = parse_window(v);
        else if (OPT("--nulls")) nulls = parse_window(v);
        else if (OPT("--imu-jitter")) imu_jitter = atoi(v);
        else {
            fprintf(stderr, "usage: vg-fake-device [--seconds S] [--seed N] [--emg-rate HZ] "
                            "[--imu-rate HZ] [--batch-ms MS] [--baud N] [--ring N] "
                            "[--device-id ID] [--boot-id ID] [--stall A:B] [--pause A:B] "
                            "[--disconnect A:B] [--nulls A:B] [--imu-jitter US]\n");
            return 2;
        }
#undef OPT
    }

    static vg_sample_t emg_slots[4096], imu_slots[4096];
    if (ring_cap > 4096) ring_cap = 4096;
    vg_sample_ring_t emg_ring, imu_ring;
    if (!vg_ring_init(&emg_ring, emg_slots, ring_cap) || !vg_ring_init(&imu_ring, imu_slots, ring_cap)) {
        fprintf(stderr, "ring size must be a power of two in 2..4096\n");
        return 2;
    }

    uint16_t emg_rows = (uint16_t)(emg_rate * batch_ms / 1000.0 + 0.5);
    uint16_t imu_rows = (uint16_t)(imu_rate * batch_ms / 1000.0 + 0.5);
    if (emg_rows < 1) emg_rows = 1;
    if (imu_rows < 1) imu_rows = 1;
    if (emg_rows > 256) emg_rows = 256;
    if (imu_rows > 256) imu_rows = 256;
    int64_t latency = (int64_t)(2 * batch_ms * 1000);

    vg_batcher_t emg_b, imu_b;
    vg_batcher_cfg_t ec = {VG_STREAM_EMG, VG_SRC_SIMULATED, 1, EMG_CH, "adc_counts", emg_rate,
                           emg_rows, 256, latency, 0.25};
    vg_batcher_cfg_t ic = {VG_STREAM_IMU, VG_SRC_SIMULATED, 3, IMU_CH, "raw_counts", imu_rate,
                           imu_rows, 256, latency, 0.25};
    if (!vg_batcher_init(&emg_b, &ec, &emg_ring) || !vg_batcher_init(&imu_b, &ic, &imu_ring)) {
        fprintf(stderr, "bad batcher config\n");
        return 2;
    }

    static uint8_t fq_buf[32768];
    vg_frame_queue_t fq;
    vg_fq_init(&fq, fq_buf, sizeof fq_buf, 2048);

    char rec_id[65];
    snprintf(rec_id, sizeof rec_id, "%s", boot_id);
    vg_tm_t tm;
    if (vg_tm_init(&tm, device_id, boot_id, VG_SRC_SIMULATED, rec_id) != VG_TM_OK) {
        fprintf(stderr, "invalid device/boot id\n");
        return 2;
    }
    static const vg_tm_kv_t hw[] = {
        {"board", "none (vg-fake-device on host; SIMULATED)"},
        {"firmware_layer", "firmware/shared (same code as ESP32 build)"},
    };
    vg_tm_hello_t hello = {
        .fw_name = "vibeguard-fw-fake",
        .fw_version = "0.2.0-prompt2",
        .emg = {true, emg_rate, 1, EMG_CH, "adc_counts", true, 0, 4095,
                "fake EMG source (SIMULATED; synthetic counts)"},
        .imu = {true, imu_rate, 3, IMU_CH, "raw_counts", true, -32768, 32767,
                "fake accelerometer source (SIMULATED; synthetic counts)"},
        .hardware = hw,
        .n_hardware = 2,
    };
    static vg_link_t link;
    if (!vg_link_init(&link, &tm, &hello, &emg_b, &imu_b, &fq, 1000000, 5000000)) {
        fprintf(stderr, "link init failed\n");
        return 2;
    }

    vg_fake_emg_t femg;
    vg_fake_imu_t fimu;
    vg_fake_emg_init(&femg, seed);
    vg_fake_imu_init(&fimu, seed + 1);

    const int64_t boot_us = 1000;   /* device time of first tick */
    double emg_period = 1e6 / emg_rate, imu_period = 1e6 / imu_rate;
    double next_emg = 5000, next_imu = 5000;
    double budget = 0;
    static uint8_t pending[VG_LINK_FRAME_CAP];
    uint32_t pending_len = 0, pending_off = 0;
    uint64_t bytes_out = 0, frames_out = 0;
    int64_t end_us = boot_us + (int64_t)(seconds * 1e6);
    uint32_t jit = seed * 2654435761u;

    for (int64_t t = boot_us; t <= end_us; t += 1000) {
        double ts = (double)(t - boot_us) / 1e6;
        /* ---- acquisition (never waits) ---- */
        bool emg_disc = in(disc, ts);
        vg_link_set_sensor(&link, VG_STREAM_EMG, emg_disc ? VG_SENSOR_DISCONNECTED : VG_SENSOR_OK, t);
        vg_link_set_sensor(&link, VG_STREAM_IMU, VG_SENSOR_OK, t);
        while (next_emg <= (double)t) {
            vg_sample_t s = {0};
            s.t_us = (int64_t)next_emg;
            s.v[0] = vg_fake_emg_next(&femg);
            if (emg_disc) s.flags = VG_SF_DISCONNECTED;
            vg_ring_push(&emg_ring, &s);
            next_emg += emg_period;
        }
        while (next_imu <= (double)t) {
            vg_sample_t s = {0};
            jit = jit * 1664525u + 1013904223u;
            int j = imu_jitter ? (int)(jit % (uint32_t)(2 * imu_jitter + 1)) - imu_jitter : 0;
            s.t_us = (int64_t)next_imu + j;
            vg_fake_imu_next(&fimu, s.v);
            if (in(nulls, ts)) s.null_mask = 1u << 1;
            vg_ring_push(&imu_ring, &s);
            next_imu += imu_period;
        }
        /* ---- serialization (every 10 ms unless paused) ---- */
        if ((t - boot_us) % 10000 == 0 && !in(pause, ts)) {
            vg_link_step(&link, t);
        }
        /* ---- transmission: byte budget per tick ---- */
        if (in(stall, ts)) {
            budget = 0;  /* sink accepts nothing */
        } else {
            budget = baud ? budget + (double)baud / 10.0 / 1000.0 : 1e12;
        }
        for (;;) {
            if (pending_off == pending_len) {
                pending_len = vg_fq_pop(&fq, pending, sizeof pending);
                pending_off = 0;
                if (pending_len == 0) break;
            }
            uint32_t want = pending_len - pending_off;
            uint32_t can = budget >= want ? want : (uint32_t)budget;
            if (can == 0) break;
            fwrite(pending + pending_off, 1, can, stdout);
            pending_off += can;
            budget -= can;
            bytes_out += can;
            if (pending_off == pending_len) frames_out++;
        }
        if (baud && budget > (double)baud / 10.0 * 0.01) {
            budget = (double)baud / 10.0 * 0.01; /* UART does not bank idle time */
        }
    }
    vg_link_flush(&link, end_us);
    for (;;) { /* drain the queue at end (unlimited) */
        if (pending_off < pending_len) {
            fwrite(pending + pending_off, 1, pending_len - pending_off, stdout);
            bytes_out += pending_len - pending_off;
            frames_out++;
            pending_off = pending_len;
        }
        pending_len = vg_fq_pop(&fq, pending, sizeof pending);
        pending_off = 0;
        if (pending_len == 0) break;
    }
    fflush(stdout);

    fprintf(stderr,
            "vg-fake-device: SIMULATED %.3gs emg=%gHz imu=%gHz batch=%gms baud=%lld\n"
            "  frames_written=%" PRIu64 " bytes_written=%" PRIu64 " (%.0f B/s)\n"
            "  seq_assigned=%" PRIu64 " tx_dropped_records=%" PRIu64 " tx_dropped_bytes=%" PRIu64 "\n"
            "  emg_dropped_samples=%" PRIu64 " imu_dropped_samples=%" PRIu64
            " encode_errors=%" PRIu64 "\n",
            seconds, emg_rate, imu_rate, batch_ms, baud, frames_out, bytes_out,
            (double)bytes_out / seconds, link.tm.next_seq, fq.dropped_frames, fq.dropped_bytes,
            vg_ring_dropped(&emg_ring), vg_ring_dropped(&imu_ring), link.encode_errors);
    return link.encode_errors ? 1 : 0;
}
