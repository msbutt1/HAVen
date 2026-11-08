/* Station controller: station command protocol v1 (PROPOSED,
 * firmware/contract/STATION_PROTOCOL.md) and bounded stimulus control.
 *
 * Single-threaded: call vg_station_rx() for received bytes and
 * vg_station_tick() frequently (e.g. every 1-5 ms) from ONE task. Output is
 * off at init, on expiry, abort, link timeout, fault and any error path.
 * The trial sequence (staircase) is the host's job (Fahad), not this one.
 */
#ifndef VG_STATION_H
#define VG_STATION_H

#include <stdbool.h>
#include <stdint.h>

#include "vg/haptic.h"
#include "vg/jsonflat.h"

#ifdef __cplusplus
extern "C" {
#endif

#define VG_ST_LINE_MAX 512          /* command line limit incl. '\n' */
#define VG_ST_EVENT_MAX 640
#define VG_ST_RECENT 32             /* request ids remembered per boot */
#define VG_ST_HARD_MAX_MS 2000u     /* absolute ceiling, independent of config/host */

typedef enum { VG_ST_IDLE = 0, VG_ST_ACTIVE, VG_ST_FAULT, VG_ST_DISABLED } vg_st_state_t;

typedef enum {
    VG_OUT_NONE = 0,
    VG_OUT_IN_PROGRESS,
    VG_OUT_COMPLETED,
    VG_OUT_ABORTED,
    VG_OUT_LINK_TIMEOUT,
    VG_OUT_FAULT,
    VG_OUT_REJECTED,
} vg_st_outcome_t;

typedef struct {
    uint32_t max_duration_ms;       /* <= VG_ST_HARD_MAX_MS */
    uint32_t heartbeat_timeout_ms;  /* active stimulus ends if no valid line for this long */
    uint32_t hello_period_ms;
    uint32_t fault_poll_ms;
} vg_st_limits_t;

typedef struct {
    char id[65];
    uint8_t kind;                   /* 1 stimulus, 2 abort, 3 status */
    uint16_t level;
    uint32_t duration_ms;
    vg_st_outcome_t outcome;
} vg_st_req_t;

typedef void (*vg_st_emit_t)(void *ctx, const char *line, size_t len);

typedef struct {
    /* identity */
    char device_id[65], boot_id[65], fw_version[33];
    vg_haptic_t hap;
    vg_st_limits_t lim;
    vg_st_emit_t emit;
    void *emit_ctx;

    /* state */
    vg_st_state_t state;
    vg_st_req_t recent[VG_ST_RECENT];
    int recent_next;
    vg_st_req_t *active;            /* points into recent[] while ACTIVE */
    int64_t started_us, end_us, last_rx_us, next_hello_us, next_poll_us;
    uint64_t seq;
    bool hello_sent;
    char fault_detail[96];

    /* counters */
    uint32_t n_commands, n_rejected, n_malformed, n_oversized, n_aborted, n_link_timeouts,
        n_faults, n_completed;

    /* line assembly and scratch (kept here, not on small task stacks) */
    char line[VG_ST_LINE_MAX];
    size_t line_len;
    bool discarding;
    vg_jf_obj_t obj;
    char ev[VG_ST_EVENT_MAX];
    size_t ev_len;
} vg_station_t;

/* motor_permitted: from vg_motor_output_permitted(board config). If false or
 * the driver init fails, the controller is DISABLED (stimuli rejected). */
bool vg_station_init(vg_station_t *st, const char *device_id, const char *boot_id,
                     const char *fw_version, vg_haptic_t hap, vg_st_limits_t lim,
                     bool motor_permitted, vg_st_emit_t emit, void *emit_ctx, int64_t now_us);

void vg_station_rx(vg_station_t *st, const uint8_t *data, size_t n, int64_t now_us);
void vg_station_tick(vg_station_t *st, int64_t now_us);

/* Force output off immediately (e.g. on shutdown). Ends any active stimulus. */
void vg_station_stop(vg_station_t *st, vg_st_outcome_t why, int64_t now_us);

const char *vg_st_state_name(vg_st_state_t s);
const char *vg_st_outcome_name(vg_st_outcome_t o);

#ifdef __cplusplus
}
#endif
#endif
