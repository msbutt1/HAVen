#include "vg/sample_ring.h"

#include <stddef.h>

bool vg_ring_init(vg_sample_ring_t *r, vg_sample_t *slots, uint32_t capacity)
{
    if (r == NULL || slots == NULL || capacity < 2 || (capacity & (capacity - 1)) != 0) {
        return false;
    }
    r->slots = slots;
    r->capacity = capacity;
    atomic_init(&r->head, 0);
    atomic_init(&r->tail, 0);
    r->next_index = 0;
    atomic_init(&r->dropped, 0);
    return true;
}

bool vg_ring_push(vg_sample_ring_t *r, const vg_sample_t *s)
{
    uint64_t index = r->next_index++;
    uint32_t head = (uint32_t)atomic_load_explicit(&r->head, memory_order_relaxed);
    uint32_t tail = (uint32_t)atomic_load_explicit(&r->tail, memory_order_acquire);
    if ((uint32_t)(head - tail) >= r->capacity) {
        atomic_fetch_add_explicit(&r->dropped, 1, memory_order_relaxed);
        return false;
    }
    vg_sample_t *slot = &r->slots[head & (r->capacity - 1)];
    *slot = *s;
    slot->index = index;
    atomic_store_explicit(&r->head, head + 1, memory_order_release);
    return true;
}

void vg_ring_skip(vg_sample_ring_t *r)
{
    r->next_index++;
    atomic_fetch_add_explicit(&r->dropped, 1, memory_order_relaxed);
}

bool vg_ring_peek(vg_sample_ring_t *r, vg_sample_t *out)
{
    uint32_t tail = (uint32_t)atomic_load_explicit(&r->tail, memory_order_relaxed);
    uint32_t head = (uint32_t)atomic_load_explicit(&r->head, memory_order_acquire);
    if (head == tail) {
        return false;
    }
    *out = r->slots[tail & (r->capacity - 1)];
    return true;
}

void vg_ring_pop(vg_sample_ring_t *r)
{
    uint32_t tail = (uint32_t)atomic_load_explicit(&r->tail, memory_order_relaxed);
    atomic_store_explicit(&r->tail, tail + 1, memory_order_release);
}

uint32_t vg_ring_count(vg_sample_ring_t *r)
{
    uint32_t head = (uint32_t)atomic_load_explicit(&r->head, memory_order_acquire);
    uint32_t tail = (uint32_t)atomic_load_explicit(&r->tail, memory_order_acquire);
    return head - tail;
}

uint64_t vg_ring_dropped(vg_sample_ring_t *r)
{
    return (uint64_t)atomic_load_explicit(&r->dropped, memory_order_relaxed);
}
