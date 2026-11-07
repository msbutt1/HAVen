#include "vg/frame_queue.h"

bool vg_fq_init(vg_frame_queue_t *q, uint8_t *buf, uint32_t capacity, uint32_t reserve)
{
    /* Power-of-two capacity keeps (counter % capacity) continuous when the
     * 32-bit byte counters wrap. */
    if (q == NULL || buf == NULL || capacity < 64 || (capacity & (capacity - 1)) != 0 ||
        reserve >= capacity) {
        return false;
    }
    q->buf = buf;
    q->capacity = capacity;
    q->reserve = reserve;
    atomic_init(&q->head, 0);
    atomic_init(&q->tail, 0);
    q->dropped_frames = 0;
    q->dropped_bytes = 0;
    q->queued_frames = 0;
    return true;
}

static void put(vg_frame_queue_t *q, uint32_t pos, const uint8_t *src, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) {
        q->buf[(pos + i) % q->capacity] = src[i];
    }
}

static void get(const vg_frame_queue_t *q, uint32_t pos, uint8_t *dst, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) {
        dst[i] = q->buf[(pos + i) % q->capacity];
    }
}

uint32_t vg_fq_used(vg_frame_queue_t *q)
{
    uint32_t head = (uint32_t)atomic_load_explicit(&q->head, memory_order_acquire);
    uint32_t tail = (uint32_t)atomic_load_explicit(&q->tail, memory_order_acquire);
    return head - tail;
}

bool vg_fq_push(vg_frame_queue_t *q, const uint8_t *frame, uint32_t len, bool priority)
{
    uint32_t need = len + 2;
    uint32_t limit = priority ? q->capacity : q->capacity - q->reserve;
    if (len == 0 || len > 0xFFFFu || vg_fq_used(q) + need > limit) {
        q->dropped_frames++;
        q->dropped_bytes += len;
        return false;
    }
    uint32_t head = (uint32_t)atomic_load_explicit(&q->head, memory_order_relaxed);
    uint32_t pos = head % q->capacity;
    uint8_t hdr[2] = {(uint8_t)(len & 0xFF), (uint8_t)(len >> 8)};
    put(q, pos, hdr, 2);
    put(q, pos + 2, frame, len);
    atomic_store_explicit(&q->head, head + need, memory_order_release);
    q->queued_frames++;
    return true;
}

uint32_t vg_fq_pop(vg_frame_queue_t *q, uint8_t *out, uint32_t cap)
{
    if (vg_fq_used(q) == 0) {
        return 0;
    }
    uint32_t tail = (uint32_t)atomic_load_explicit(&q->tail, memory_order_relaxed);
    uint32_t pos = tail % q->capacity;
    uint8_t hdr[2];
    get(q, pos, hdr, 2);
    uint32_t len = (uint32_t)hdr[0] | ((uint32_t)hdr[1] << 8);
    if (len <= cap) {
        get(q, pos + 2, out, len);
    }
    atomic_store_explicit(&q->tail, tail + len + 2, memory_order_release);
    return len <= cap ? len : 0;
}
