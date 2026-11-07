/* Bounded single-producer / single-consumer sample ring between ACQUISITION
 * (producer: timer/ISR/task that reads sensors) and SERIALIZATION (consumer).
 *
 * The producer never waits. When the ring is full the new sample is dropped
 * and counted; its sample index is still consumed, so the gap is visible
 * downstream as a first_sample_index jump with dropped_samples_before.
 *
 * Sample index semantics (contract v1 "first_sample_index"):
 *   index = number of acquisition attempts for this stream since boot,
 *   counting every scheduled sample slot, whether it was delivered, dropped
 *   because the ring was full (overrun), or failed to read. It advances by
 *   exactly 1 per attempt, never resets within a boot, and is never reused.
 *
 * Portable C11. On ESP32 the producer/consumer indices are accessed with
 * C11 atomics, which is sufficient for one producer and one consumer.
 */
#ifndef VG_SAMPLE_RING_H
#define VG_SAMPLE_RING_H

#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VG_MAX_CHANNELS 8

typedef struct {
    uint64_t index;                 /* acquisition attempt index (see above) */
    int64_t t_us;                   /* device-monotonic µs at acquisition */
    int32_t v[VG_MAX_CHANNELS];
    uint8_t null_mask;              /* bit c set: channel c unavailable (null) */
    uint8_t flags;                  /* VG_SF_* */
} vg_sample_t;

enum {
    VG_SF_CLIPPED = 1u << 0,        /* value at a confirmed raw limit */
    VG_SF_DISCONNECTED = 1u << 1,   /* sensor reported disconnected: all values null */
    VG_SF_STALE = 1u << 2,          /* sensor reported no new data */
};

typedef struct {
    vg_sample_t *slots;
    uint32_t capacity;              /* power of two */
    atomic_uint_fast32_t head;      /* written by producer */
    atomic_uint_fast32_t tail;      /* written by consumer */
    uint64_t next_index;            /* producer-owned */
    atomic_uint_fast64_t dropped;   /* producer-owned counter, readable by consumer */
} vg_sample_ring_t;

/* capacity must be a power of two >= 2; slots must hold capacity entries. */
bool vg_ring_init(vg_sample_ring_t *r, vg_sample_t *slots, uint32_t capacity);

/* Producer: claims the next sample index, stores the sample if there is
 * room. Returns false (sample dropped and counted) when full. The caller
 * fills t_us/v/null_mask/flags; index is assigned here. */
bool vg_ring_push(vg_sample_ring_t *r, const vg_sample_t *s);

/* Producer: account for an attempt that produced no sample at all (e.g. a
 * failed sensor read). Consumes an index and counts it as dropped. */
void vg_ring_skip(vg_sample_ring_t *r);

/* Consumer. */
bool vg_ring_peek(vg_sample_ring_t *r, vg_sample_t *out);
void vg_ring_pop(vg_sample_ring_t *r);
uint32_t vg_ring_count(vg_sample_ring_t *r);
uint64_t vg_ring_dropped(vg_sample_ring_t *r);

#ifdef __cplusplus
}
#endif
#endif
