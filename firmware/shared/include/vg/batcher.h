/* Groups acquired samples from one stream's ring into contract batches.
 *
 * A batch is closed (before the offending sample) when:
 *  - it reaches target_rows, or max latency passes with no new sample;
 *  - the next sample's index is not last+1 (device-side loss: the next
 *    batch carries dropped_samples_before = the index jump, flag overrun);
 *  - the sensor's disconnected/stale state changes (so a disconnected batch
 *    is all-null, as the contract requires);
 *  - the time step to the next sample is outside 0.5..1.5 x nominal.
 * On emit, only the longest prefix whose mean interval is within +-10 % of
 * nominal (the contract's timing tolerance) is sent; remaining rows stay and
 * start the next batch. So every emitted batch validates and no sample is
 * discarded for timing reasons.
 *
 * Timing: uniform (t0 + i * measured mean interval) when every timestamp is
 * within jitter_tol_frac x nominal of that line, else explicit timestamps
 * with the timing_irregular flag. A 1-row batch uses the nominal interval.
 */
#ifndef VG_BATCHER_H
#define VG_BATCHER_H

#include "vg/sample_ring.h"
#include "vg/telemetry.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    vg_stream_t stream;
    vg_source_t source;              /* provenance of the attached acquisition */
    uint8_t n_channels;
    const char *const *channels;
    const char *unit;
    double nominal_rate_hz;
    uint16_t target_rows;            /* e.g. 50 ms worth */
    uint16_t max_rows;               /* <= VG_TM_MAX_ROWS */
    int64_t max_latency_us;          /* flush a partial batch after this */
    double jitter_tol_frac;          /* uniform timing tolerance, fraction of nominal */
} vg_batcher_cfg_t;

typedef struct {
    vg_batcher_cfg_t cfg;
    vg_sample_ring_t *ring;
    int32_t values[VG_TM_MAX_ROWS * VG_MAX_CHANNELS];
    uint8_t null_mask[VG_TM_MAX_ROWS];
    int64_t t_us[VG_TM_MAX_ROWS];
    uint16_t n;
    uint64_t first_index;
    uint64_t last_index;
    uint8_t row_flags[VG_TM_MAX_ROWS];
    uint8_t state_flags;             /* VG_SF_DISCONNECTED | VG_SF_STALE of this batch */
    uint16_t shift;                  /* rows handed out by the last emit, removed on next call */
    bool carry;                      /* rows left over after a mean-limited emit */
    uint64_t expect_index;           /* index after the last consumed sample */
    uint64_t dropped_before;         /* for the batch being built */
    uint64_t batches;
} vg_batcher_t;

bool vg_batcher_init(vg_batcher_t *b, const vg_batcher_cfg_t *cfg, vg_sample_ring_t *ring);

/* Returns true and fills *out when a batch is ready. out points into the
 * batcher's buffers and stays valid until the next call. flush = emit any
 * partial batch now (end of stream / shutdown). */
bool vg_batcher_take(vg_batcher_t *b, int64_t now_us, bool flush, vg_tm_batch_t *out);

#ifdef __cplusplus
}
#endif
#endif
