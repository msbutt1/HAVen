#include "vg/fake_source.h"

/* xorshift32: deterministic, portable, no libc RNG state. */
static uint32_t next_u32(uint32_t *s)
{
    uint32_t x = *s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *s = x;
    return x;
}

/* Sum of 4 uniforms in [-r, r]: cheap bell-shaped synthetic noise. */
static int32_t noise(uint32_t *s, int32_t r)
{
    int32_t acc = 0;
    for (int i = 0; i < 4; i++) {
        acc += (int32_t)(next_u32(s) % (uint32_t)(2 * r + 1)) - r;
    }
    return acc / 2;
}

void vg_fake_emg_init(vg_fake_emg_t *f, uint32_t seed)
{
    f->state = seed ? seed : 0x9e3779b9u;
    f->index = 0;
}

int32_t vg_fake_emg_next(vg_fake_emg_t *f)
{
    int32_t v = 2048 + noise(&f->state, 40);
    f->index++;
    if (v < 0) v = 0;
    if (v > 4095) v = 4095;
    return v;
}

void vg_fake_imu_init(vg_fake_imu_t *f, uint32_t seed)
{
    f->state = seed ? seed : 0x85ebca6bu;
    f->index = 0;
}

void vg_fake_imu_next(vg_fake_imu_t *f, int32_t out[3])
{
    out[0] = noise(&f->state, 8);
    out[1] = noise(&f->state, 8);
    out[2] = 4096 + noise(&f->state, 8); /* synthetic "1 g" in synthetic counts */
    f->index++;
}
