/* Host tests: sample ring, frame queue, encoder, batcher, link, time extension.
 * With "--emit FILE", also writes every successfully encoded edge-case record
 * to FILE so check_fake_streams.py can validate them with the Python validator. */
#include <stdio.h>
#include <stdlib.h>

#include "vg/batcher.h"
#include "vg/frame_queue.h"
#include "vg/link.h"
#include "vg/sample_ring.h"
#include "vg/telemetry.h"
#include "vg/time_ext.h"
#include "vg/clock_est.h"
#include "vg_test.h"

static FILE *emit_file;
static char line[VG_LINK_FRAME_CAP];

static void keep(int n)
{
    if (n > 0 && emit_file) fwrite(line, 1, (size_t)n, emit_file);
}

static const char *const EMG_CH[] = {"emg0"};
static const char *const IMU_CH[] = {"ax", "ay", "az"};

static vg_tm_batch_t emg_batch(vg_source_t src, uint16_t n, const int32_t *v)
{
    return (vg_tm_batch_t){.stream = VG_STREAM_EMG, .source = src, .sample_rate_hz = 1000.0,
                           .first_sample_index = 0, .t0_us = 5000, .interval_us = 1000.0,
                           .n_channels = 1, .channels = EMG_CH, .unit = "adc_counts",
                           .n_rows = n, .values = v};
}

/* ---- ring ---- */
static void test_ring_bounded_and_counted(void)
{
    vg_sample_t slots[4];
    vg_sample_ring_t r;
    CHECK(!vg_ring_init(&r, slots, 3));
    CHECK(vg_ring_init(&r, slots, 4));
    vg_sample_t s = {0}, o;
    for (int i = 0; i < 6; i++) {
        s.t_us = 1000 + i;
        bool ok = vg_ring_push(&r, &s);
        CHECK(ok == (i < 4));
    }
    CHECK(vg_ring_count(&r) == 4 && vg_ring_dropped(&r) == 2);
    vg_ring_skip(&r);                       /* failed read: index consumed, counted */
    CHECK(vg_ring_dropped(&r) == 3);
    CHECK(vg_ring_peek(&r, &o) && o.index == 0);
    for (int i = 0; i < 4; i++) vg_ring_pop(&r);
    CHECK(vg_ring_push(&r, &s));
    CHECK(vg_ring_peek(&r, &o) && o.index == 7); /* 0-3 kept, 4-5 dropped, 6 skipped */

    /* 32-bit position counters wrap without losing order */
    vg_ring_init(&r, slots, 4);
    atomic_store(&r.head, UINT32_MAX - 1);
    atomic_store(&r.tail, UINT32_MAX - 1);
    for (int i = 0; i < 4; i++) { s.v[0] = i; CHECK(vg_ring_push(&r, &s)); }
    CHECK(!vg_ring_push(&r, &s));
    for (int i = 0; i < 4; i++) {
        CHECK(vg_ring_peek(&r, &o) && o.v[0] == i);
        vg_ring_pop(&r);
    }
    CHECK(vg_ring_count(&r) == 0);
}

/* ---- frame queue ---- */
static void test_frame_queue(void)
{
    uint8_t buf[128], out[128];
    vg_frame_queue_t q;
    CHECK(!vg_fq_init(&q, buf, 100, 10));   /* not a power of two */
    CHECK(vg_fq_init(&q, buf, 128, 40));
    uint8_t f[50];
    for (int i = 0; i < 50; i++) f[i] = (uint8_t)i;
    CHECK(vg_fq_push(&q, f, 50, false));   /* 52 used */
    CHECK(!vg_fq_push(&q, f, 50, false));  /* 104 > 88 normal limit: dropped */
    CHECK(vg_fq_push(&q, f, 50, true));    /* priority may use the reserve */
    CHECK(!vg_fq_push(&q, f, 50, true));   /* full: even priority drops */
    CHECK(q.dropped_frames == 2 && q.dropped_bytes == 100);
    CHECK(vg_fq_pop(&q, out, sizeof out) == 50 && out[49] == 49);
    CHECK(vg_fq_pop(&q, out, sizeof out) == 50);
    CHECK(vg_fq_pop(&q, out, sizeof out) == 0);

    /* byte counters wrap at 2^32 */
    vg_fq_init(&q, buf, 128, 0);
    atomic_store(&q.head, UINT32_MAX - 20);
    atomic_store(&q.tail, UINT32_MAX - 20);
    for (int k = 0; k < 5; k++) {
        f[0] = (uint8_t)k;
        CHECK(vg_fq_push(&q, f, 50, false));
        CHECK(vg_fq_pop(&q, out, sizeof out) == 50 && out[0] == k && out[49] == 49);
    }
}

/* ---- encoder ---- */
static void test_encoder_records_and_seq(void)
{
    vg_tm_t tm;
    CHECK(vg_tm_init(&tm, "bad id!", "b1", VG_SRC_LIVE, NULL) == VG_TM_ERR_ARG);
    CHECK(vg_tm_init(&tm, "dev", "b1", VG_SRC_LIVE, "rec") == VG_TM_ERR_ARG); /* recording id: simulated only */
    CHECK(vg_tm_init(&tm, "edge-dev", "edge-boot-1", VG_SRC_SIMULATED, "edge-rec") == VG_TM_OK);

    vg_tm_hello_t h = {
        .t_us = 1000, .fw_name = "vibeguard-fw-test", .fw_version = "0",
        .emg = {true, 1000.0, 1, EMG_CH, "adc_counts", false, 0, 0, NULL},
        .imu = {true, 0.0, 3, IMU_CH, "raw_counts", false, 0, 0, NULL}, /* rate unknown -> null */
    };
    int n = vg_tm_hello(&tm, &h, line, sizeof line);
    CHECK(n > 0 && tm.next_seq == 1);
    CHECK(strstr(line, "\"nominal_rate_hz\":null") != NULL);
    CHECK(strstr(line, "\"sensor_model\":null") && strstr(line, "\"raw_limits\":null"));
    keep(n);

    vg_tm_status_t st = {.t_us = 2000, .state = VG_DEV_STREAMING}; /* sensors default unknown */
    n = vg_tm_status(&tm, &st, line, sizeof line);
    CHECK(n > 0 && strstr(line, "\"emg\":\"unknown\"") && strstr(line, "\"imu\":\"unknown\""));
    keep(n);

    int32_t v[3] = {1, 2, 3};
    vg_tm_batch_t b = emg_batch(VG_SRC_SIMULATED, 3, v);
    n = vg_tm_batch(&tm, &b, line, 40);           /* does not fit */
    CHECK(n == VG_TM_ERR_SPACE && tm.next_seq == 2); /* no seq consumed */
    n = vg_tm_batch(&tm, &b, line, sizeof line);
    CHECK(n > 0 && tm.next_seq == 3 && line[n - 1] == '\n');
    keep(n);

    b.source = VG_SRC_LIVE;                       /* live-labelled samples into a simulated stream */
    CHECK(vg_tm_batch(&tm, &b, line, sizeof line) == VG_TM_ERR_PROVENANCE);
    CHECK(tm.next_seq == 3);
}

static void test_encoder_quality_rules(void)
{
    vg_tm_t tm;
    vg_tm_init(&tm, "edge-dev", "edge-boot-2", VG_SRC_SIMULATED, NULL);
    int32_t v[3] = {10, 20, 30};
    uint8_t nm[3] = {0, 1, 0};
    vg_tm_batch_t b = emg_batch(VG_SRC_SIMULATED, 3, v);
    b.null_mask = nm;
    int n = vg_tm_batch(&tm, &b, line, sizeof line);
    CHECK(n > 0 && strstr(line, "[null]") && strstr(line, "missing_values")); /* flag derived */
    keep(n);

    b.flags = VG_QF_SENSOR_DISCONNECTED;          /* not all null: refused */
    CHECK(vg_tm_batch(&tm, &b, line, sizeof line) == VG_TM_ERR_ARG);
    uint8_t all[3] = {1, 1, 1};
    b.null_mask = all;
    n = vg_tm_batch(&tm, &b, line, sizeof line);
    CHECK(n > 0 && strstr(line, "sensor_disconnected") && strstr(line, "missing_values"));
    keep(n);

    b.null_mask = NULL;
    b.flags = VG_QF_MISSING_VALUES;               /* flag without nulls: removed */
    b.dropped_before = 7;                         /* requires overrun: added */
    b.first_sample_index = 10;
    n = vg_tm_batch(&tm, &b, line, sizeof line);
    CHECK(n > 0 && !strstr(line, "missing_values") && strstr(line, "\"overrun\"") &&
          strstr(line, "\"dropped_samples_before\":7"));
    keep(n);

    b.dropped_before = 0;
    b.interval_us = 1200.0;                       /* > +10 % of nominal: refused */
    CHECK(vg_tm_batch(&tm, &b, line, sizeof line) == VG_TM_ERR_ARG);
    b.interval_us = 1000.0;
    int64_t ts[3] = {5000, 5000, 7000};           /* not strictly increasing */
    b.explicit_timing = true;
    b.t_us = ts;
    CHECK(vg_tm_batch(&tm, &b, line, sizeof line) == VG_TM_ERR_ARG);
    ts[1] = 6100;
    n = vg_tm_batch(&tm, &b, line, sizeof line);
    CHECK(n > 0 && strstr(line, "\"t_us\":[5000,6100,7000]"));
    keep(n);
}

static void test_limits_and_restart(void)
{
    vg_tm_t tm;
    vg_tm_init(&tm, "edge-dev", "edge-boot-3", VG_SRC_SIMULATED, NULL);
    tm.next_seq = (uint64_t)VG_TM_MAX_SAFE_INT;   /* last representable seq */
    vg_tm_status_t st = {.t_us = VG_TM_MAX_SAFE_INT, .state = VG_DEV_STREAMING};
    int n = vg_tm_status(&tm, &st, line, sizeof line);
    CHECK(n > 0 && strstr(line, "\"seq\":9007199254740991") &&
          strstr(line, "\"t_us\":9007199254740991"));
    keep(n);
    CHECK(vg_tm_status(&tm, &st, line, sizeof line) == VG_TM_ERR_EXHAUSTED); /* never wraps */
    st.t_us = VG_TM_MAX_SAFE_INT + 1;
    tm.next_seq = 5;
    CHECK(vg_tm_status(&tm, &st, line, sizeof line) == VG_TM_ERR_EXHAUSTED);

    /* restart = new boot_id, seq from 0 again; never the same boot twice */
    vg_tm_t tm2;
    vg_tm_init(&tm2, "edge-dev", "edge-boot-4", VG_SRC_SIMULATED, NULL);
    CHECK(tm2.next_seq == 0 && strcmp(tm2.boot_id, tm.boot_id) != 0);
    st.t_us = 1;
    n = vg_tm_status(&tm2, &st, line, sizeof line);
    CHECK(n > 0 && strstr(line, "\"seq\":0"));
    keep(n);

    /* large counts and negative raw values format exactly */
    int32_t v[3] = {INT32_MIN + 1, 0, INT32_MAX};
    vg_tm_batch_t b = {.stream = VG_STREAM_IMU, .source = VG_SRC_SIMULATED, .sample_rate_hz = 400.0,
                       .first_sample_index = (uint64_t)VG_TM_MAX_SAFE_INT, .t0_us = 1,
                       .interval_us = 2500.0, .n_channels = 3, .channels = IMU_CH,
                       .unit = "raw_counts", .n_rows = 1, .values = v};
    n = vg_tm_batch(&tm2, &b, line, sizeof line);
    CHECK(n > 0 && strstr(line, "[-2147483647,0,2147483647]"));
    keep(n);
    b.first_sample_index++;
    CHECK(vg_tm_batch(&tm2, &b, line, sizeof line) == VG_TM_ERR_EXHAUSTED);
}

static void test_time_extension_wrap(void)
{
    vg_time_ext_t e = {0};
    CHECK(vg_time_extend32(&e, 0xFFFFFF00u) == 0xFFFFFF00u);
    CHECK(vg_time_extend32(&e, 0x00000010u) == 0x100000010ull);
    CHECK(vg_time_extend32(&e, 0x00000020u) == 0x100000020ull);
    CHECK(vg_time_extend32(&e, 0xFFFFFFFFu) == 0x1FFFFFFFFull);
    CHECK(vg_time_extend32(&e, 0x00000000u) == 0x200000000ull);
}

static void test_clock_estimator(void)
{
    /* IMU at 500 Hz whose clock is 1 % slow, polled with up to +1 ms read delay
     * (deterministic pseudo-jitter): reconstructed steps stay near the true
     * period and the timestamps track the true sample times. */
    vg_clock_est_t c;
    vg_clock_est_init(&c, 2000.0);
    uint32_t r = 12345;
    double true_period = 2020.0, worst_step_dev = 0, worst_err = 0;
    int64_t prev = 0;
    for (int k = 0; k < 5000; k++) {
        double t_true = 100000.0 + k * true_period;
        r = r * 1664525u + 1013904223u;
        int64_t read = (int64_t)(t_true + (double)(r % 1000u));       /* 0..999 us late */
        int64_t ts = vg_clock_est_update(&c, read);
        if (k > 1000) {
            double step = (double)(ts - prev);
            double dev = step > true_period ? step - true_period : true_period - step;
            if (dev > worst_step_dev) worst_step_dev = dev;
            double err = (double)ts - (t_true + 500.0);               /* mean read delay 500 us */
            if (err < 0) err = -err;
            if (err > worst_err) worst_err = err;
        }
        prev = ts;
    }
    CHECK(worst_step_dev < 100.0);       /* raw read steps vary by up to 1000 us */
    CHECK(worst_err < 400.0);
    CHECK(c.resyncs == 0);
    /* a real gap (missed samples) re-locks instead of smearing */
    int64_t ts = vg_clock_est_update(&c, prev + 10 * 2020);
    CHECK(c.resyncs == 1 && ts == prev + 10 * 2020);
}

/* ---- batcher ---- */
static vg_sample_t slots[512];
static vg_sample_ring_t ring;
static vg_batcher_t bat;

static void setup_batcher(uint16_t target)
{
    vg_ring_init(&ring, slots, 512);
    vg_batcher_cfg_t c = {VG_STREAM_EMG, VG_SRC_SIMULATED, 1, EMG_CH, "adc_counts", 1000.0,
                          target, 256, 100000, 0.25};
    CHECK(vg_batcher_init(&bat, &c, &ring));
}

static void push(int64_t t, uint8_t flags)
{
    vg_sample_t s = {0};
    s.t_us = t;
    s.flags = flags;
    s.v[0] = (int32_t)t;
    vg_ring_push(&ring, &s);
}

static void test_batcher_gap_and_states(void)
{
    vg_tm_batch_t b;
    setup_batcher(10);
    for (int i = 0; i < 10; i++) push(1000 + i * 1000, 0);
    CHECK(vg_batcher_take(&bat, 0, false, &b));
    CHECK(b.n_rows == 10 && b.first_sample_index == 0 && !b.explicit_timing &&
          b.interval_us == 1000.0 && b.dropped_before == 0 && b.flags == 0);

    /* acquisition overrun: 3 samples dropped at the ring (indices 13-15 lost) */
    push(11000, 0);
    push(12000, 0);
    push(13000, 0);
    vg_ring_skip(&ring);
    vg_ring_skip(&ring);
    vg_ring_skip(&ring);
    push(17000, 0);
    CHECK(vg_batcher_take(&bat, 0, false, &b) && b.n_rows == 3 && b.first_sample_index == 10);
    CHECK(!vg_batcher_take(&bat, 0, false, &b));
    CHECK(vg_batcher_take(&bat, 0, true, &b));
    CHECK(b.first_sample_index == 16 && b.dropped_before == 3 && (b.flags & VG_QF_OVERRUN));

    /* disconnect transition cuts the batch; the disconnected batch is all-null */
    push(18000, 0);
    push(19000, VG_SF_DISCONNECTED);
    push(20000, VG_SF_DISCONNECTED);
    CHECK(vg_batcher_take(&bat, 0, false, &b) && b.n_rows == 1 && b.flags == 0);
    CHECK(vg_batcher_take(&bat, 0, true, &b) && b.n_rows == 2 &&
          (b.flags & VG_QF_SENSOR_DISCONNECTED) && b.null_mask[0] == 1 && b.null_mask[1] == 1);
}

static void test_batcher_timing(void)
{
    vg_tm_batch_t b;
    /* small jitter: uniform with measured mean interval */
    setup_batcher(5);
    int64_t t[5] = {1000, 2010, 2990, 4005, 5000};
    for (int i = 0; i < 5; i++) push(t[i], 0);
    CHECK(vg_batcher_take(&bat, 0, false, &b) && !b.explicit_timing && b.interval_us == 1000.0);

    /* large jitter within the batch: explicit timestamps + timing_irregular */
    setup_batcher(5);
    int64_t u[5] = {1000, 2400, 3100, 4300, 5000}; /* steps within 0.5-1.5x, residual 400 > 250 */
    for (int i = 0; i < 5; i++) push(u[i], 0);
    CHECK(vg_batcher_take(&bat, 0, false, &b) && b.explicit_timing &&
          (b.flags & VG_QF_TIMING_IRREGULAR) && b.t_us[1] == 2400);

    /* a time gap (step > 1.5 x nominal) cuts the batch */
    setup_batcher(10);
    push(1000, 0);
    push(2000, 0);
    push(9000, 0);
    CHECK(vg_batcher_take(&bat, 0, false, &b) && b.n_rows == 2);

    /* drifting rate: cut before the batch mean leaves +-10 % */
    setup_batcher(50);
    int64_t tt = 1000;
    for (int i = 0; i < 50; i++) { push(tt, 0); tt += 1140; } /* 14 % slow */
    CHECK(vg_batcher_take(&bat, 0, false, &b));
    CHECK(b.n_rows == 1);  /* first step already 14 % off: single-row batches */

    /* latency flush of a partial batch */
    setup_batcher(50);
    push(1000, 0);
    CHECK(!vg_batcher_take(&bat, 50000, false, &b));
    CHECK(vg_batcher_take(&bat, 101000, false, &b) && b.n_rows == 1 && b.interval_us == 1000.0);

    /* mean-limited emit keeps the rest: no sample discarded, indices contiguous */
    setup_batcher(20);
    int64_t w[6] = {1000, 2400, 3800, 5200, 6000, 7000}; /* first steps 40 % slow, then normal */
    for (int i = 0; i < 6; i++) push(w[i], 0);
    uint64_t next = 0;
    int rows = 0;
    while (vg_batcher_take(&bat, 0, true, &b)) {
        CHECK(b.first_sample_index == next);
        double mean = b.n_rows > 1 ? (double)(b.t_us[b.n_rows - 1] - b.t_us[0]) / (b.n_rows - 1)
                                   : 1000.0;
        if (!b.explicit_timing) mean = b.interval_us;
        CHECK(mean >= 900.0 && mean <= 1100.0);
        CHECK(b.dropped_before == 0);
        next += b.n_rows;
        rows += b.n_rows;
    }
    CHECK(rows == 6);
}

/* ---- link ---- */
static void test_link_provenance_guard(void)
{
    vg_tm_t live;
    vg_tm_init(&live, "dev", "boot", VG_SRC_LIVE, NULL);
    setup_batcher(10); /* batcher fed by SIMULATED acquisition */
    uint8_t buf[4096];
    vg_frame_queue_t q;
    vg_fq_init(&q, buf, sizeof buf, 512);
    vg_tm_hello_t h = {.fw_name = "x", .fw_version = "y",
                       .emg = {true, 1000.0, 1, EMG_CH, "adc_counts", false, 0, 0, NULL}};
    static vg_link_t l;
    CHECK(!vg_link_init(&l, &live, &h, &bat, NULL, &q, 1000000, 5000000));
}

int main(int argc, char **argv)
{
    if (argc == 3 && strcmp(argv[1], "--emit") == 0) {
        emit_file = fopen(argv[2], "w");
        if (!emit_file) return 2;
    }
    RUN(test_ring_bounded_and_counted);
    RUN(test_frame_queue);
    RUN(test_encoder_records_and_seq);
    RUN(test_encoder_quality_rules);
    RUN(test_limits_and_restart);
    RUN(test_time_extension_wrap);
    RUN(test_clock_estimator);
    RUN(test_batcher_gap_and_states);
    RUN(test_batcher_timing);
    RUN(test_link_provenance_guard);
    if (emit_file) fclose(emit_file);
    return DONE();
}
