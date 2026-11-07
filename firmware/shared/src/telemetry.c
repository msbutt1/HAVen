#include "vg/telemetry.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* ---- bounded JSON writer ------------------------------------------------ */

typedef struct {
    char *buf;
    size_t cap;
    size_t len;
    bool overflow;
} jw_t;

static void jw_raw(jw_t *w, const char *s, size_t n)
{
    if (w->overflow || w->len + n >= w->cap) { /* keep room for NUL */
        w->overflow = true;
        return;
    }
    memcpy(w->buf + w->len, s, n);
    w->len += n;
}

static void jw_s(jw_t *w, const char *s) { jw_raw(w, s, strlen(s)); }

static void jw_fmt(jw_t *w, const char *fmt, ...)
{
    char tmp[48];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof tmp, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= sizeof tmp) {
        w->overflow = true;
        return;
    }
    jw_raw(w, tmp, (size_t)n);
}

/* JSON string with escaping. Control characters are escaped as \u00XX. */
static void jw_str(jw_t *w, const char *s)
{
    jw_raw(w, "\"", 1);
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if (*p == '"' || *p == '\\') {
            char e[2] = {'\\', (char)*p};
            jw_raw(w, e, 2);
        } else if (*p < 0x20) {
            jw_fmt(w, "\\u%04x", *p);
        } else {
            jw_raw(w, (const char *)p, 1);
        }
    }
    jw_raw(w, "\"", 1);
}

static void jw_key(jw_t *w, const char *k)
{
    jw_str(w, k);
    jw_raw(w, ":", 1);
}

static void jw_i64(jw_t *w, int64_t v) { jw_fmt(w, "%lld", (long long)v); }
static void jw_u64(jw_t *w, uint64_t v) { jw_fmt(w, "%llu", (unsigned long long)v); }

/* Finite double, at most 3 decimals, always with a fractional part. */
static bool jw_num(jw_t *w, double v)
{
    if (!isfinite(v)) {
        return false;
    }
    char tmp[48];
    int n = snprintf(tmp, sizeof tmp, "%.3f", v);
    if (n <= 0 || (size_t)n >= sizeof tmp) {
        w->overflow = true;
        return true;
    }
    while (n > 2 && tmp[n - 1] == '0' && tmp[n - 2] != '.') {
        tmp[--n] = '\0';
    }
    jw_raw(w, tmp, (size_t)n);
    return true;
}

/* ---- common ----------------------------------------------------------- */

bool vg_tm_valid_id(const char *s)
{
    if (s == NULL) {
        return false;
    }
    size_t n = strlen(s);
    if (n < 1 || n > 64) {
        return false;
    }
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
                  c == '.' || c == '_' || c == ':' || c == '-';
        if (!ok) {
            return false;
        }
    }
    return true;
}

static bool valid_text(const char *s)
{
    return s != NULL && s[0] != '\0' && strlen(s) <= 200;
}

vg_tm_err_t vg_tm_init(vg_tm_t *tm, const char *device_id, const char *boot_id,
                       vg_source_t source, const char *recording_id)
{
    if (tm == NULL || !vg_tm_valid_id(device_id) || !vg_tm_valid_id(boot_id) ||
        (source != VG_SRC_LIVE && source != VG_SRC_SIMULATED)) {
        return VG_TM_ERR_ARG;
    }
    if (recording_id != NULL && recording_id[0] != '\0') {
        if (source != VG_SRC_SIMULATED || !vg_tm_valid_id(recording_id)) {
            return VG_TM_ERR_ARG;
        }
    }
    memset(tm, 0, sizeof *tm);
    strcpy(tm->device_id, device_id);
    strcpy(tm->boot_id, boot_id);
    tm->source = source;
    if (recording_id != NULL) {
        strcpy(tm->recording_id, recording_id);
    }
    tm->next_seq = 0;
    return VG_TM_OK;
}

static void header(jw_t *w, const vg_tm_t *tm, const char *type)
{
    jw_s(w, "{\"schema\":\"vibeguard.device_telemetry\",\"version\":1,\"type\":");
    jw_str(w, type);
    jw_s(w, ",\"device_id\":");
    jw_str(w, tm->device_id);
    jw_s(w, ",\"boot_id\":");
    jw_str(w, tm->boot_id);
    jw_s(w, ",\"seq\":");
    jw_u64(w, tm->next_seq);
    jw_s(w, ",\"provenance\":{\"source\":");
    jw_str(w, tm->source == VG_SRC_LIVE ? "live" : "simulated");
    if (tm->recording_id[0]) {
        jw_s(w, ",\"recording_id\":");
        jw_str(w, tm->recording_id);
    }
    jw_s(w, "}");
}

static int finish(vg_tm_t *tm, jw_t *w)
{
    jw_raw(w, "}\n", 2);
    if (w->overflow || w->len > VG_TM_MAX_LINE) {
        return VG_TM_ERR_SPACE;
    }
    w->buf[w->len] = '\0';
    tm->next_seq++;              /* consumed only for a complete record */
    return (int)w->len;
}

static bool seq_ok(const vg_tm_t *tm)
{
    return tm->next_seq <= (uint64_t)VG_TM_MAX_SAFE_INT;
}

static bool time_ok(int64_t t) { return t >= 0 && t <= VG_TM_MAX_SAFE_INT; }

/* ---- hello ------------------------------------------------------------ */

static bool channel_ok(vg_stream_t st, const char *ch)
{
    if (ch == NULL) {
        return false;
    }
    if (st == VG_STREAM_EMG) {
        return strlen(ch) == 4 && strncmp(ch, "emg", 3) == 0 && ch[3] >= '0' && ch[3] <= '7';
    }
    static const char *const imu[] = {"ax", "ay", "az", "gx", "gy", "gz"};
    for (size_t i = 0; i < 6; i++) {
        if (strcmp(ch, imu[i]) == 0) {
            return true;
        }
    }
    return false;
}

static bool raw_unit_ok(vg_stream_t st, const char *unit)
{
    /* This firmware sends raw counts only; physical units need a conversion
     * that is decided on the host from confirmed hardware facts. */
    return unit != NULL &&
           strcmp(unit, st == VG_STREAM_EMG ? "adc_counts" : "raw_counts") == 0;
}

static bool channels_ok(vg_stream_t st, uint8_t n, const char *const *ch, const char *unit)
{
    if (n < 1 || n > VG_MAX_CHANNELS || ch == NULL || !raw_unit_ok(st, unit)) {
        return false;
    }
    for (uint8_t i = 0; i < n; i++) {
        if (!channel_ok(st, ch[i])) {
            return false;
        }
        for (uint8_t j = 0; j < i; j++) {
            if (strcmp(ch[i], ch[j]) == 0) {
                return false;
            }
        }
    }
    return true;
}

static void channels_units(jw_t *w, uint8_t n, const char *const *ch, const char *unit)
{
    jw_s(w, "\"channels\":[");
    for (uint8_t i = 0; i < n; i++) {
        if (i) jw_raw(w, ",", 1);
        jw_str(w, ch[i]);
    }
    jw_s(w, "],\"units\":{");
    for (uint8_t i = 0; i < n; i++) {
        if (i) jw_raw(w, ",", 1);
        jw_key(w, ch[i]);
        jw_str(w, unit);
    }
    jw_s(w, "}");
}

static bool stream_decl(jw_t *w, vg_stream_t st, const vg_tm_stream_decl_t *d)
{
    if (!channels_ok(st, d->n_channels, d->channels, d->unit)) {
        return false;
    }
    if (d->has_raw_limits && !(d->raw_min < d->raw_max)) {
        return false;
    }
    if (d->sensor_model != NULL && !valid_text(d->sensor_model)) {
        return false;
    }
    jw_key(w, st == VG_STREAM_EMG ? "emg" : "imu");
    jw_s(w, "{\"nominal_rate_hz\":");
    if (d->nominal_rate_hz > 0 && d->nominal_rate_hz <= 20000.0) {
        jw_num(w, d->nominal_rate_hz);
    } else {
        jw_s(w, "null");
    }
    jw_raw(w, ",", 1);
    channels_units(w, d->n_channels, d->channels, d->unit);
    jw_s(w, ",\"raw_limits\":");
    if (d->has_raw_limits) {
        jw_s(w, "{\"min\":");
        jw_i64(w, d->raw_min);
        jw_s(w, ",\"max\":");
        jw_i64(w, d->raw_max);
        jw_s(w, "}");
    } else {
        jw_s(w, "null");
    }
    jw_s(w, ",\"sensor_model\":");
    if (d->sensor_model) {
        jw_str(w, d->sensor_model);
    } else {
        jw_s(w, "null");
    }
    jw_s(w, "}");
    return true;
}

int vg_tm_hello(vg_tm_t *tm, const vg_tm_hello_t *h, char *out, size_t cap)
{
    if (!seq_ok(tm) || !time_ok(h->t_us)) {
        return VG_TM_ERR_EXHAUSTED;
    }
    if (!valid_text(h->fw_name) || !valid_text(h->fw_version) ||
        (!h->emg.present && !h->imu.present) || h->n_hardware > 16) {
        return VG_TM_ERR_ARG;
    }
    jw_t w = {out, cap, 0, false};
    header(&w, tm, "hello");
    jw_s(&w, ",\"t_us\":");
    jw_i64(&w, h->t_us);
    jw_s(&w, ",\"firmware\":{\"name\":");
    jw_str(&w, h->fw_name);
    jw_s(&w, ",\"version\":");
    jw_str(&w, h->fw_version);
    jw_s(&w, "},\"streams\":{");
    bool first = true;
    if (h->emg.present) {
        if (!stream_decl(&w, VG_STREAM_EMG, &h->emg)) return VG_TM_ERR_ARG;
        first = false;
    }
    if (h->imu.present) {
        if (!first) jw_raw(&w, ",", 1);
        if (!stream_decl(&w, VG_STREAM_IMU, &h->imu)) return VG_TM_ERR_ARG;
    }
    jw_s(&w, "}");
    if (h->n_hardware) {
        jw_s(&w, ",\"hardware\":{");
        for (uint8_t i = 0; i < h->n_hardware; i++) {
            const vg_tm_kv_t *kv = &h->hardware[i];
            if (!vg_tm_valid_id(kv->key) || (kv->value && !valid_text(kv->value))) {
                return VG_TM_ERR_ARG;
            }
            if (i) jw_raw(&w, ",", 1);
            jw_key(&w, kv->key);
            if (kv->value) jw_str(&w, kv->value); else jw_s(&w, "null");
        }
        jw_s(&w, "}");
    }
    return finish(tm, &w);
}

/* ---- status ----------------------------------------------------------- */

static const char *sensor_name(vg_sensor_state_t s)
{
    switch (s) {
    case VG_SENSOR_OK: return "ok";
    case VG_SENSOR_DISCONNECTED: return "disconnected";
    case VG_SENSOR_STALE: return "stale";
    default: return "unknown";
    }
}

static const char *device_name(vg_device_state_t s)
{
    switch (s) {
    case VG_DEV_READY: return "ready";
    case VG_DEV_STREAMING: return "streaming";
    case VG_DEV_ERROR: return "error";
    default: return "booting";
    }
}

int vg_tm_status(vg_tm_t *tm, const vg_tm_status_t *s, char *out, size_t cap)
{
    if (!seq_ok(tm) || !time_ok(s->t_us)) {
        return VG_TM_ERR_EXHAUSTED;
    }
    if (s->n_counters > 16 || (s->message != NULL && !valid_text(s->message))) {
        return VG_TM_ERR_ARG;
    }
    jw_t w = {out, cap, 0, false};
    header(&w, tm, "status");
    jw_s(&w, ",\"t_us\":");
    jw_i64(&w, s->t_us);
    jw_s(&w, ",\"state\":");
    jw_str(&w, device_name(s->state));
    jw_s(&w, ",\"sensors\":{\"emg\":");
    jw_str(&w, sensor_name(s->emg));
    jw_s(&w, ",\"imu\":");
    jw_str(&w, sensor_name(s->imu));
    jw_s(&w, "}");
    if (s->n_counters) {
        jw_s(&w, ",\"counters\":{");
        for (uint8_t i = 0; i < s->n_counters; i++) {
            if (!vg_tm_valid_id(s->counters[i].key) ||
                s->counters[i].value > (uint64_t)VG_TM_MAX_SAFE_INT) {
                return VG_TM_ERR_ARG;
            }
            if (i) jw_raw(&w, ",", 1);
            jw_key(&w, s->counters[i].key);
            jw_u64(&w, s->counters[i].value);
        }
        jw_s(&w, "}");
    }
    if (s->message) {
        jw_s(&w, ",\"message\":");
        jw_str(&w, s->message);
    }
    return finish(tm, &w);
}

/* ---- batches ---------------------------------------------------------- */

static const char *const FLAG_NAMES[] = {"clipped", "missing_values", "sensor_disconnected",
                                         "overrun", "stale", "timing_irregular", "warmup"};

int vg_tm_batch(vg_tm_t *tm, const vg_tm_batch_t *b, char *out, size_t cap)
{
    if (b->source != tm->source) {
        return VG_TM_ERR_PROVENANCE;
    }
    if (!seq_ok(tm) || b->first_sample_index > (uint64_t)VG_TM_MAX_SAFE_INT) {
        return VG_TM_ERR_EXHAUSTED;
    }
    if (b->n_rows < 1 || b->n_rows > VG_TM_MAX_ROWS ||
        !channels_ok(b->stream, b->n_channels, b->channels, b->unit) ||
        !(b->sample_rate_hz > 0 && b->sample_rate_hz <= 20000.0) || b->values == NULL) {
        return VG_TM_ERR_ARG;
    }
    /* Timing: everything the validator checks, checked here first. */
    double nominal = 1e6 / b->sample_rate_hz;
    if (b->explicit_timing) {
        if (b->t_us == NULL) return VG_TM_ERR_ARG;
        for (uint16_t i = 0; i < b->n_rows; i++) {
            if (!time_ok(b->t_us[i])) return VG_TM_ERR_EXHAUSTED;
            if (i && b->t_us[i] <= b->t_us[i - 1]) return VG_TM_ERR_ARG;
        }
        if (b->n_rows >= 2) {
            double mean = (double)(b->t_us[b->n_rows - 1] - b->t_us[0]) / (b->n_rows - 1);
            if (fabs(mean - nominal) > 0.10 * nominal) return VG_TM_ERR_ARG;
        }
    } else {
        if (!time_ok(b->t0_us) || !isfinite(b->interval_us) || b->interval_us <= 0 ||
            fabs(b->interval_us - nominal) > 0.10 * nominal) {
            return VG_TM_ERR_ARG;
        }
        if ((double)b->t0_us + (b->n_rows - 1) * b->interval_us > (double)VG_TM_MAX_SAFE_INT) {
            return VG_TM_ERR_EXHAUSTED;
        }
    }
    /* Quality consistency (contract rules), derived from the data. */
    uint32_t missing = 0, total = (uint32_t)b->n_rows * b->n_channels;
    uint8_t all = (uint8_t)((1u << b->n_channels) - 1u);
    for (uint16_t r = 0; r < b->n_rows; r++) {
        uint8_t m = b->null_mask ? (uint8_t)(b->null_mask[r] & all) : 0;
        for (uint8_t c = 0; c < b->n_channels; c++) {
            missing += (m >> c) & 1u;
        }
    }
    uint8_t flags = b->flags;
    flags = missing ? (uint8_t)(flags | VG_QF_MISSING_VALUES)
                    : (uint8_t)(flags & ~VG_QF_MISSING_VALUES);
    if ((flags & VG_QF_SENSOR_DISCONNECTED) && missing != total) {
        return VG_TM_ERR_ARG;  /* disconnected batches must be all-null */
    }
    if (b->dropped_before) {
        flags |= VG_QF_OVERRUN;
    }

    jw_t w = {out, cap, 0, false};
    header(&w, tm, b->stream == VG_STREAM_EMG ? "emg_batch" : "imu_batch");
    jw_s(&w, ",\"sample_rate_hz\":");
    jw_num(&w, b->sample_rate_hz);
    jw_s(&w, ",\"first_sample_index\":");
    jw_u64(&w, b->first_sample_index);
    if (b->explicit_timing) {
        jw_s(&w, ",\"timing\":{\"mode\":\"explicit\",\"t_us\":[");
        for (uint16_t i = 0; i < b->n_rows; i++) {
            if (i) jw_raw(&w, ",", 1);
            jw_i64(&w, b->t_us[i]);
        }
        jw_s(&w, "]}");
    } else {
        jw_s(&w, ",\"timing\":{\"mode\":\"uniform\",\"t0_us\":");
        jw_i64(&w, b->t0_us);
        jw_s(&w, ",\"interval_us\":");
        jw_num(&w, b->interval_us);
        jw_s(&w, "}");
    }
    jw_raw(&w, ",", 1);
    channels_units(&w, b->n_channels, b->channels, b->unit);
    jw_s(&w, ",\"samples\":[");
    for (uint16_t r = 0; r < b->n_rows; r++) {
        if (r) jw_raw(&w, ",", 1);
        jw_raw(&w, "[", 1);
        uint8_t m = b->null_mask ? b->null_mask[r] : 0;
        for (uint8_t c = 0; c < b->n_channels; c++) {
            if (c) jw_raw(&w, ",", 1);
            if ((m >> c) & 1u) {
                jw_s(&w, "null");
            } else {
                jw_i64(&w, b->values[(size_t)r * b->n_channels + c]);
            }
        }
        jw_raw(&w, "]", 1);
        if (w.overflow) {
            return VG_TM_ERR_SPACE;
        }
    }
    jw_s(&w, "],\"quality\":{\"flags\":[");
    bool first = true;
    for (unsigned i = 0; i < 7; i++) {
        if (flags & (1u << i)) {
            if (!first) jw_raw(&w, ",", 1);
            jw_str(&w, FLAG_NAMES[i]);
            first = false;
        }
    }
    jw_s(&w, "]");
    if (b->dropped_before) {
        jw_s(&w, ",\"dropped_samples_before\":");
        jw_u64(&w, b->dropped_before);
    }
    jw_s(&w, "}");
    return finish(tm, &w);
}

size_t vg_tm_batch_max_bytes(uint16_t n_rows, uint8_t n_channels, bool explicit_timing)
{
    /* header + ids + provenance + fixed fields: generous fixed allowance */
    size_t fixed = 700;
    size_t per_value = 12;               /* "-2147483648," */
    size_t per_row = 3 + (size_t)n_channels * per_value;
    size_t per_ts = explicit_timing ? 18 : 0;   /* 16 digits + comma */
    size_t per_channel_decl = 2 * (8 + 14);     /* name in channels + units */
    return fixed + (size_t)n_rows * (per_row + per_ts) + (size_t)n_channels * per_channel_decl;
}
