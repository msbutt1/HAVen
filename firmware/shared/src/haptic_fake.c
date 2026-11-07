#include <stdio.h>
#include <string.h>

#include "vg/haptic.h"

static bool f_init(void *ctx, char *d, size_t n)
{
    vg_fake_haptic_t *f = ctx;
    f->init_calls++;
    f->output_on = false;
    snprintf(d, n, f->fail_init ? "fake: init failure injected" : "fake driver (logic only)");
    return !f->fail_init;
}

static bool f_on(void *ctx, uint8_t level)
{
    vg_fake_haptic_t *f = ctx;
    f->on_calls++;
    if (f->fail_on) return false;
    f->output_on = true;
    f->level = level;
    return true;
}

static bool f_off(void *ctx)
{
    vg_fake_haptic_t *f = ctx;
    f->off_calls++;
    if (f->fail_off) return false;
    f->output_on = false;
    f->level = 0;
    return true;
}

static bool f_check(void *ctx, char *d, size_t n)
{
    vg_fake_haptic_t *f = ctx;
    f->check_calls++;
    snprintf(d, n, f->fail_check ? "fake: fault injected" : "ok");
    return !f->fail_check;
}

vg_haptic_t vg_fake_haptic(vg_fake_haptic_t *state)
{
    memset(state, 0, sizeof *state);
    return (vg_haptic_t){f_init, f_on, f_off, f_check, state, "fake", "none", "none", 0.0, true};
}
