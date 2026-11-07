#include "vg/batcher.h"

#include <math.h>
#include <string.h>

bool vg_batcher_init(vg_batcher_t *b, const vg_batcher_cfg_t *cfg, vg_sample_ring_t *ring)
{
    if (b == NULL || cfg == NULL || ring == NULL || cfg->n_channels < 1 ||
        cfg->n_channels > VG_MAX_CHANNELS || cfg->max_rows < 1 ||
        cfg->max_rows > VG_TM_MAX_ROWS || cfg->target_rows < 1 ||
        cfg->target_rows > cfg->max_rows || !(cfg->nominal_rate_hz > 0) ||
        cfg->jitter_tol_frac <= 0 || cfg->max_latency_us <= 0) {
        return false;
    }
    memset(b, 0, sizeof *b);
    b->cfg = *cfg;
    b->ring = ring;
    return true;
}

static double nominal_us(const vg_batcher_t *b) { return 1e6 / b->cfg.nominal_rate_hz; }

/* Remove the rows handed out by the previous emit (deferred so the returned
 * batch stayed valid until this call). Remaining rows keep their order. */
static void apply_shift(vg_batcher_t *b)
{
    uint16_t k = b->shift;
    if (k == 0) {
        return;
    }
    uint16_t rest = (uint16_t)(b->n - k);
    uint8_t nc = b->cfg.n_channels;
    memmove(b->values, &b->values[(size_t)k * nc], (size_t)rest * nc * sizeof(int32_t));
    memmove(b->null_mask, &b->null_mask[k], rest);
    memmove(b->row_flags, &b->row_flags[k], rest);
    memmove(b->t_us, &b->t_us[k], (size_t)rest * sizeof(int64_t));
    b->first_index += k;
    b->dropped_before = 0;      /* carried rows directly follow the emitted ones */
    b->n = rest;
    b->shift = 0;
}

static bool must_cut(const vg_batcher_t *b, const vg_sample_t *s)
{
    if (b->n == 0) {
        return false;
    }
    if (b->n >= b->cfg.target_rows || s->index != b->last_index + 1) {
        return true;
    }
    if ((s->flags & (VG_SF_DISCONNECTED | VG_SF_STALE)) != b->state_flags) {
        return true;
    }
    double nom = nominal_us(b);
    double step = (double)(s->t_us - b->t_us[b->n - 1]);
    return step < 0.5 * nom || step > 1.5 * nom;
}

static void append(vg_batcher_t *b, const vg_sample_t *s)
{
    if (b->n == 0) {
        b->first_index = s->index;
        b->dropped_before = s->index - b->expect_index;
        b->state_flags = s->flags & (VG_SF_DISCONNECTED | VG_SF_STALE);
    }
    uint8_t nc = b->cfg.n_channels;
    memcpy(&b->values[(size_t)b->n * nc], s->v, nc * sizeof(int32_t));
    uint8_t all = (uint8_t)((1u << nc) - 1u);
    /* a disconnected sensor has no values: every channel is null */
    b->null_mask[b->n] = (s->flags & VG_SF_DISCONNECTED) ? all : (uint8_t)(s->null_mask & all);
    b->row_flags[b->n] = s->flags;
    b->t_us[b->n] = s->t_us;
    b->last_index = s->index;
    b->expect_index = s->index + 1;
    b->n++;
}

/* Longest prefix whose mean interval is within the contract's +-10 %. */
static uint16_t valid_prefix(const vg_batcher_t *b)
{
    double nom = nominal_us(b);
    for (uint16_t k = b->n; k >= 2; k--) {
        double mean = (double)(b->t_us[k - 1] - b->t_us[0]) / (k - 1);
        if (fabs(mean - nom) <= 0.10 * nom) {
            return k;
        }
    }
    return 1;
}

static void emit(vg_batcher_t *b, vg_tm_batch_t *out)
{
    double nom = nominal_us(b);
    uint16_t n = valid_prefix(b);
    bool uniform = true;
    double interval = nom;
    if (n >= 2) {
        interval = (double)(b->t_us[n - 1] - b->t_us[0]) / (n - 1);
        for (uint16_t i = 0; i < n; i++) {
            double ideal = (double)b->t_us[0] + i * interval;
            if (fabs((double)b->t_us[i] - ideal) > b->cfg.jitter_tol_frac * nom) {
                uniform = false;
                break;
            }
        }
    }
    uint8_t flags = 0;
    if (b->state_flags & VG_SF_DISCONNECTED) flags |= VG_QF_SENSOR_DISCONNECTED;
    if (b->state_flags & VG_SF_STALE) flags |= VG_QF_STALE;
    for (uint16_t i = 0; i < n; i++) {
        if (b->row_flags[i] & VG_SF_CLIPPED) flags |= VG_QF_CLIPPED;
    }
    if (!uniform) flags |= VG_QF_TIMING_IRREGULAR;
    if (b->dropped_before) flags |= VG_QF_OVERRUN;

    *out = (vg_tm_batch_t){
        .stream = b->cfg.stream,
        .source = b->cfg.source,
        .sample_rate_hz = b->cfg.nominal_rate_hz,
        .first_sample_index = b->first_index,
        .explicit_timing = !uniform,
        .t0_us = b->t_us[0],
        .interval_us = interval,
        .t_us = b->t_us,
        .n_channels = b->cfg.n_channels,
        .channels = b->cfg.channels,
        .unit = b->cfg.unit,
        .n_rows = n,
        .values = b->values,
        .null_mask = b->null_mask,
        .flags = flags,
        .dropped_before = b->dropped_before,
    };
    b->shift = n;
    b->batches++;
}

bool vg_batcher_take(vg_batcher_t *b, int64_t now_us, bool flush, vg_tm_batch_t *out)
{
    apply_shift(b);
    /* Rows carried over from a mean-limited emit are themselves a batch to
     * re-check before more samples are added. */
    if (b->n > 0 && b->carry) {
        b->carry = false;
        if (valid_prefix(b) < b->n) {
            emit(b, out);
            b->carry = b->shift < b->n;
            return true;
        }
    }
    vg_sample_t s;
    while (vg_ring_peek(b->ring, &s)) {
        if (must_cut(b, &s)) {
            emit(b, out);
            b->carry = b->shift < b->n;
            return true;
        }
        append(b, &s);
        vg_ring_pop(b->ring);
    }
    if (b->n > 0 && (flush || b->n >= b->cfg.target_rows ||
                     now_us - b->t_us[0] >= b->cfg.max_latency_us)) {
        emit(b, out);
        b->carry = b->shift < b->n;
        return true;
    }
    return false;
}
