/* Wearable telemetry link: SERIALIZATION stage between the acquisition rings
 * and the transmit frame queue. Single-threaded: call vg_link_step() from one
 * task. It never waits on acquisition or on transmission.
 *
 * Record schedule per boot (one seq counter for all record types):
 *   seq 0  hello, then status (device "streaming")
 *   batches as they fill; status every status_period_us and on any sensor
 *   state change; hello re-sent every hello_period_us so a host that
 *   (re)connects mid-boot learns the stream declaration. seq never repeats
 *   within a boot; a new boot (or stream restart) needs a new boot_id.
 */
#ifndef VG_LINK_H
#define VG_LINK_H

#include "vg/batcher.h"
#include "vg/frame_queue.h"
#include "vg/telemetry.h"

#ifdef __cplusplus
extern "C" {
#endif

#define VG_LINK_FRAME_CAP 8192

typedef struct {
    vg_tm_t tm;
    vg_tm_hello_t hello;             /* copied; referenced strings must outlive the link */
    vg_batcher_t *batchers[2];       /* [VG_STREAM_EMG], [VG_STREAM_IMU]; NULL if absent */
    vg_frame_queue_t *fq;
    char frame[VG_LINK_FRAME_CAP];

    vg_sensor_state_t sensor[2];
    vg_device_state_t device_state;
    int64_t status_period_us, hello_period_us;
    int64_t next_status_us, next_hello_us;
    bool started;
    volatile bool resend_hello;      /* set by the transport on (re)connect */

    /* counters (also reported in status records) */
    uint64_t encode_errors;          /* record could not be encoded: data lost */
    uint64_t provenance_rejects;     /* batch refused: source mismatch */
    bool exhausted;                  /* seq/time limit: stream must restart */
} vg_link_t;

bool vg_link_init(vg_link_t *l, const vg_tm_t *tm, const vg_tm_hello_t *hello,
                  vg_batcher_t *emg, vg_batcher_t *imu, vg_frame_queue_t *fq,
                  int64_t status_period_us, int64_t hello_period_us);

/* Sensor states start UNKNOWN and change only when the acquisition layer
 * reports evidence. A change emits a status record immediately. */
void vg_link_set_sensor(vg_link_t *l, vg_stream_t s, vg_sensor_state_t st, int64_t now_us);

void vg_link_step(vg_link_t *l, int64_t now_us);

/* End of stream: emit partial batches and a final status. */
void vg_link_flush(vg_link_t *l, int64_t now_us);

#ifdef __cplusplus
}
#endif
#endif
