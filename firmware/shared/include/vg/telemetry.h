/* Device telemetry contract v1 encoder (signals/contract/TELEMETRY_CONTRACT.md).
 *
 * Writes one record as one compact NDJSON line (terminated by '\n') into a
 * caller buffer. Never truncates: if a record does not fit, nothing is
 * returned and no seq is consumed. Provenance is fixed per encoder: a batch
 * whose samples came from a different source kind is refused, so fake
 * samples can never enter a live stream.
 */
#ifndef VG_TELEMETRY_H
#define VG_TELEMETRY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "vg/sample_ring.h"

#ifdef __cplusplus
extern "C" {
#endif

#define VG_TM_MAX_SAFE_INT 9007199254740991LL   /* 2^53 - 1 */
#define VG_TM_MAX_ROWS 256
#define VG_TM_MAX_LINE 65536                      /* contract frame limit incl. '\n' */

typedef enum {
    VG_SRC_LIVE = 1,        /* real firmware acquiring real sensors */
    VG_SRC_SIMULATED = 2,   /* fake/synthetic acquisition */
} vg_source_t;

typedef enum { VG_STREAM_EMG = 0, VG_STREAM_IMU = 1 } vg_stream_t;

typedef enum {
    VG_SENSOR_UNKNOWN = 0,  /* default: never upgraded without evidence */
    VG_SENSOR_OK,
    VG_SENSOR_DISCONNECTED,
    VG_SENSOR_STALE,
} vg_sensor_state_t;

typedef enum {
    VG_DEV_BOOTING = 0,
    VG_DEV_READY,
    VG_DEV_STREAMING,
    VG_DEV_ERROR,
} vg_device_state_t;

typedef enum {
    VG_TM_OK = 0,
    VG_TM_ERR_SPACE = -1,        /* buffer too small: nothing written */
    VG_TM_ERR_ARG = -2,          /* invalid record content */
    VG_TM_ERR_PROVENANCE = -3,   /* sample source differs from stream provenance */
    VG_TM_ERR_EXHAUSTED = -4,    /* seq or time would exceed 2^53-1: restart stream */
} vg_tm_err_t;

enum {
    VG_QF_CLIPPED = 1u << 0,
    VG_QF_MISSING_VALUES = 1u << 1,
    VG_QF_SENSOR_DISCONNECTED = 1u << 2,
    VG_QF_OVERRUN = 1u << 3,
    VG_QF_STALE = 1u << 4,
    VG_QF_TIMING_IRREGULAR = 1u << 5,
    VG_QF_WARMUP = 1u << 6,
};

typedef struct {
    char device_id[65];
    char boot_id[65];
    vg_source_t source;
    char recording_id[65];       /* optional, simulated only; "" = omitted */
    uint64_t next_seq;           /* one counter per (device_id, boot_id), all types */
} vg_tm_t;

typedef struct {
    bool present;
    double nominal_rate_hz;      /* <= 0: unknown -> null */
    uint8_t n_channels;
    const char *const *channels;
    const char *unit;            /* same raw unit for all channels */
    bool has_raw_limits;
    int32_t raw_min, raw_max;
    const char *sensor_model;    /* NULL -> null (unresolved) */
} vg_tm_stream_decl_t;

typedef struct {
    const char *key;             /* id string */
    const char *value;           /* NULL -> null */
} vg_tm_kv_t;

typedef struct {
    int64_t t_us;
    const char *fw_name;
    const char *fw_version;
    vg_tm_stream_decl_t emg, imu;
    const vg_tm_kv_t *hardware;  /* up to 16 */
    uint8_t n_hardware;
} vg_tm_hello_t;

typedef struct {
    const char *key;
    uint64_t value;
} vg_tm_counter_t;

typedef struct {
    int64_t t_us;
    vg_device_state_t state;
    vg_sensor_state_t emg, imu;
    const vg_tm_counter_t *counters;  /* up to 16 */
    uint8_t n_counters;
    const char *message;              /* NULL -> omitted; <= 200 chars */
} vg_tm_status_t;

typedef struct {
    vg_stream_t stream;
    vg_source_t source;          /* where these samples came from */
    double sample_rate_hz;
    uint64_t first_sample_index;
    bool explicit_timing;
    int64_t t0_us;               /* uniform */
    double interval_us;          /* uniform */
    const int64_t *t_us;         /* explicit: n_rows entries */
    uint8_t n_channels;
    const char *const *channels;
    const char *unit;
    uint16_t n_rows;
    const int32_t *values;       /* n_rows * n_channels, row-major */
    const uint8_t *null_mask;    /* n_rows entries, bit c = channel c null; may be NULL */
    uint8_t flags;               /* VG_QF_* */
    uint64_t dropped_before;
} vg_tm_batch_t;

/* device_id / boot_id: 1-64 chars of [A-Za-z0-9._:-]. */
vg_tm_err_t vg_tm_init(vg_tm_t *tm, const char *device_id, const char *boot_id,
                       vg_source_t source, const char *recording_id);
bool vg_tm_valid_id(const char *s);

/* Each returns the line length (> 0) on success, or a negative vg_tm_err_t. */
int vg_tm_hello(vg_tm_t *tm, const vg_tm_hello_t *h, char *out, size_t cap);
int vg_tm_status(vg_tm_t *tm, const vg_tm_status_t *s, char *out, size_t cap);
int vg_tm_batch(vg_tm_t *tm, const vg_tm_batch_t *b, char *out, size_t cap);

/* Upper bound on a batch line's size, for sizing buffers and row limits. */
size_t vg_tm_batch_max_bytes(uint16_t n_rows, uint8_t n_channels, bool explicit_timing);

#ifdef __cplusplus
}
#endif
#endif
