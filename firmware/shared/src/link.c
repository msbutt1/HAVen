#include "vg/link.h"

#include <string.h>

bool vg_link_init(vg_link_t *l, const vg_tm_t *tm, const vg_tm_hello_t *hello,
                  vg_batcher_t *emg, vg_batcher_t *imu, vg_frame_queue_t *fq,
                  int64_t status_period_us, int64_t hello_period_us)
{
    if (l == NULL || tm == NULL || hello == NULL || fq == NULL || (emg == NULL && imu == NULL) ||
        status_period_us <= 0 || hello_period_us <= 0) {
        return false;
    }
    if ((emg && emg->cfg.source != tm->source) || (imu && imu->cfg.source != tm->source)) {
        return false; /* fake acquisition can never be attached to a live link */
    }
    memset(l, 0, sizeof *l);
    l->tm = *tm;
    l->hello = *hello;
    l->batchers[VG_STREAM_EMG] = emg;
    l->batchers[VG_STREAM_IMU] = imu;
    l->fq = fq;
    l->sensor[0] = l->sensor[1] = VG_SENSOR_UNKNOWN;
    l->device_state = VG_DEV_STREAMING;
    l->status_period_us = status_period_us;
    l->hello_period_us = hello_period_us;
    return true;
}

static void send(vg_link_t *l, int n, bool priority)
{
    if (n == VG_TM_ERR_EXHAUSTED) {
        l->exhausted = true;
    }
    if (n <= 0) {
        l->encode_errors++;
        return;
    }
    /* A dropped frame already consumed its seq: the host sees a seq gap. */
    vg_fq_push(l->fq, (const uint8_t *)l->frame, (uint32_t)n, priority);
}

static void send_hello(vg_link_t *l, int64_t now_us)
{
    l->hello.t_us = now_us;
    send(l, vg_tm_hello(&l->tm, &l->hello, l->frame, sizeof l->frame), true);
    l->next_hello_us = now_us + l->hello_period_us;
}

static void send_status(vg_link_t *l, int64_t now_us)
{
    vg_batcher_t *e = l->batchers[VG_STREAM_EMG], *i = l->batchers[VG_STREAM_IMU];
    vg_tm_counter_t c[6];
    uint8_t n = 0;
    if (e) c[n++] = (vg_tm_counter_t){"emg_dropped_samples", vg_ring_dropped(e->ring)};
    if (i) c[n++] = (vg_tm_counter_t){"imu_dropped_samples", vg_ring_dropped(i->ring)};
    c[n++] = (vg_tm_counter_t){"tx_dropped_records", l->fq->dropped_frames};
    c[n++] = (vg_tm_counter_t){"tx_dropped_bytes", l->fq->dropped_bytes};
    c[n++] = (vg_tm_counter_t){"encode_errors", l->encode_errors};
    c[n++] = (vg_tm_counter_t){"provenance_rejects", l->provenance_rejects};
    vg_tm_status_t s = {
        .t_us = now_us,
        .state = l->device_state,
        .emg = l->sensor[VG_STREAM_EMG],
        .imu = l->sensor[VG_STREAM_IMU],
        .counters = c,
        .n_counters = n,
        .message = NULL,
    };
    send(l, vg_tm_status(&l->tm, &s, l->frame, sizeof l->frame), true);
    l->next_status_us = now_us + l->status_period_us;
}

void vg_link_set_sensor(vg_link_t *l, vg_stream_t s, vg_sensor_state_t st, int64_t now_us)
{
    if (l->sensor[s] != st) {
        l->sensor[s] = st;
        if (l->started) {
            send_status(l, now_us);
        }
    }
}

static void drain(vg_link_t *l, int64_t now_us, bool flush)
{
    for (int k = 0; k < 2; k++) {
        vg_batcher_t *b = l->batchers[k];
        vg_tm_batch_t batch;
        while (b && vg_batcher_take(b, now_us, flush, &batch)) {
            int n = vg_tm_batch(&l->tm, &batch, l->frame, sizeof l->frame);
            if (n == VG_TM_ERR_PROVENANCE) {
                l->provenance_rejects++;
                continue;
            }
            send(l, n, false);
        }
    }
}

void vg_link_step(vg_link_t *l, int64_t now_us)
{
    if (l->exhausted) {
        return;
    }
    if (!l->started) {
        l->started = true;
        send_hello(l, now_us);
        send_status(l, now_us);
    }
    drain(l, now_us, false);
    if (now_us >= l->next_status_us) {
        send_status(l, now_us);
    }
    if (now_us >= l->next_hello_us) {
        send_hello(l, now_us);
    }
}

void vg_link_flush(vg_link_t *l, int64_t now_us)
{
    if (l->exhausted) {
        return;
    }
    drain(l, now_us, true);
    send_status(l, now_us);
}
