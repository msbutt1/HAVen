/* Bounded queue of whole encoded frames between SERIALIZATION (producer) and
 * TRANSMISSION (consumer, e.g. the UART writer task).
 *
 * Never blocks. A frame that does not fit is dropped whole and counted; it
 * already carries its seq, so the host sees the loss as a seq gap. Frames are
 * never split or truncated. Normal frames may use capacity - reserve bytes;
 * priority frames (hello/status) may use the reserve too, so device state
 * still gets through while batches are being dropped.
 */
#ifndef VG_FRAME_QUEUE_H
#define VG_FRAME_QUEUE_H

#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint8_t *buf;
    uint32_t capacity;           /* bytes */
    uint32_t reserve;            /* bytes only priority frames may use */
    atomic_uint_fast32_t head;   /* producer: total bytes written (mod 2^32) */
    atomic_uint_fast32_t tail;   /* consumer */
    uint64_t dropped_frames;     /* producer-owned */
    uint64_t dropped_bytes;
    uint64_t queued_frames;
} vg_frame_queue_t;

/* capacity: power of two >= 64 bytes; reserve < capacity. */
bool vg_fq_init(vg_frame_queue_t *q, uint8_t *buf, uint32_t capacity, uint32_t reserve);

/* Each stored frame costs len + 2 bytes. Returns false if dropped. */
bool vg_fq_push(vg_frame_queue_t *q, const uint8_t *frame, uint32_t len, bool priority);

/* Copies the oldest frame into out (cap bytes). Returns its length, 0 if the
 * queue is empty. A frame larger than cap is discarded (cannot happen when
 * cap >= the producer's maximum frame size). */
uint32_t vg_fq_pop(vg_frame_queue_t *q, uint8_t *out, uint32_t cap);

uint32_t vg_fq_used(vg_frame_queue_t *q);

#ifdef __cplusplus
}
#endif
#endif
