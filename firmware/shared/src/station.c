#include "vg/station.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "vg/telemetry.h"   /* vg_tm_valid_id */

#define CMD_SCHEMA "vibeguard.station_command"
#define EV_SCHEMA "vibeguard.station_event"

enum { K_STIMULUS = 1, K_ABORT = 2, K_STATUS = 3 };

const char *vg_st_state_name(vg_st_state_t s)
{
    switch (s) {
    case VG_ST_ACTIVE: return "active";
    case VG_ST_FAULT: return "fault";
    case VG_ST_DISABLED: return "disabled";
    default: return "idle";
    }
}

const char *vg_st_outcome_name(vg_st_outcome_t o)
{
    switch (o) {
    case VG_OUT_IN_PROGRESS: return "in_progress";
    case VG_OUT_COMPLETED: return "completed";
    case VG_OUT_ABORTED: return "aborted";
    case VG_OUT_LINK_TIMEOUT: return "link_timeout";
    case VG_OUT_FAULT: return "fault";
    case VG_OUT_REJECTED: return "rejected";
    default: return "none";
    }
}

static const char *kind_name(uint8_t k)
{
    return k == K_STIMULUS ? "stimulus" : k == K_ABORT ? "abort" : "status";
}

/* ---- event writer (bounded; truncation impossible by construction) ---- */



static void add(vg_station_t *st, const char *fmt, ...)
{
    if (st->ev_len >= sizeof st->ev) return;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(st->ev + st->ev_len, sizeof st->ev - st->ev_len, fmt, ap);
    va_end(ap);
    st->ev_len = n < 0 ? sizeof st->ev : st->ev_len + (size_t)n;
}

static void begin(vg_station_t *st, const char *type, int64_t now_us)
{
    st->ev_len = 0;
    add(st,
        "{\"schema\":\"" EV_SCHEMA "\",\"version\":1,\"type\":\"%s\",\"device_id\":\"%s\","
        "\"boot_id\":\"%s\",\"seq\":%llu,\"t_us\":%lld,\"provenance\":{\"source\":\"%s\"}",
        type, st->device_id, st->boot_id, (unsigned long long)st->seq, (long long)now_us,
        st->hap.simulated ? "simulated" : "live");
}

static void finish(vg_station_t *st)
{
    add(st, "}\n");
    if (st->ev_len < sizeof st->ev) {
        st->seq++;
        st->emit(st->emit_ctx, st->ev, st->ev_len);
    }
}

/* Request ids are validated to [A-Za-z0-9._:-], so they need no escaping.
 * Free-text details are restricted to the same safe set before output. */
static void safe_text(char *dst, size_t n, const char *src)
{
    size_t k = 0;
    for (; src && *src && k + 1 < n; src++) {
        char c = *src;
        dst[k++] = (c >= 0x20 && c < 0x7f && c != '"' && c != '\\') ? c : '?';
    }
    dst[k] = '\0';
}

static void ev_hello(vg_station_t *st, int64_t now)
{
    begin(st, "hello", now);
    add(st,
        ",\"firmware\":{\"name\":\"vibeguard-fw-station\",\"version\":\"%s\"},"
        "\"driver\":{\"model\":\"%s\",\"actuator\":\"%s\",\"drive\":\"%s\",\"full_scale_v\":%.3f},"
        "\"limits\":{\"level_min\":1,\"level_max\":100,\"max_duration_ms\":%lu,"
        "\"heartbeat_timeout_ms\":%lu,\"command_max_bytes\":%d},\"state\":\"%s\"",
        st->fw_version, st->hap.model, st->hap.actuator, st->hap.drive, st->hap.full_scale_v,
        (unsigned long)st->lim.max_duration_ms, (unsigned long)st->lim.heartbeat_timeout_ms,
        VG_ST_LINE_MAX, vg_st_state_name(st->state));
    finish(st);
    st->hello_sent = true;
    st->next_hello_us = now + (int64_t)st->lim.hello_period_ms * 1000;
}

static void ev_status(vg_station_t *st, int64_t now)
{
    char fd[96];
    safe_text(fd, sizeof fd, st->fault_detail);
    begin(st, "status", now);
    add(st, ",\"state\":\"%s\",\"motor\":\"%s\"", vg_st_state_name(st->state),
        st->state == VG_ST_ACTIVE ? "on" : "off");
    if (st->active) add(st, ",\"active_request_id\":\"%s\"", st->active->id);
    else add(st, ",\"active_request_id\":null");
    if (fd[0]) add(st, ",\"fault_detail\":\"%s\"", fd);
    else add(st, ",\"fault_detail\":null");
    add(st,
        ",\"counters\":{\"commands\":%lu,\"rejected\":%lu,\"malformed\":%lu,\"oversized\":%lu,"
        "\"completed\":%lu,\"aborted\":%lu,\"link_timeouts\":%lu,\"faults\":%lu}",
        (unsigned long)st->n_commands, (unsigned long)st->n_rejected,
        (unsigned long)st->n_malformed, (unsigned long)st->n_oversized,
        (unsigned long)st->n_completed, (unsigned long)st->n_aborted,
        (unsigned long)st->n_link_timeouts, (unsigned long)st->n_faults);
    finish(st);
}

static void ev_error(vg_station_t *st, int64_t now, const char *reason, const char *detail,
                     const char *request_id)
{
    char d[120];
    safe_text(d, sizeof d, detail);
    begin(st, "error", now);
    add(st, ",\"reason\":\"%s\",\"detail\":\"%s\"", reason, d);
    if (request_id) add(st, ",\"request_id\":\"%s\"", request_id);
    else add(st, ",\"request_id\":null");
    finish(st);
}

static void ev_ack(vg_station_t *st, int64_t now, const vg_st_req_t *r, const char *result,
                   const char *reason)
{
    begin(st, "ack", now);
    add(st, ",\"request_id\":\"%s\",\"command\":\"%s\",\"result\":\"%s\"", r->id,
        kind_name(r->kind), result);
    if (reason) add(st, ",\"reason\":\"%s\"", reason);
    else add(st, ",\"reason\":null");
    if (strcmp(result, "duplicate") == 0) add(st, ",\"outcome\":\"%s\"", vg_st_outcome_name(r->outcome));
    finish(st);
}

static void ev_started(vg_station_t *st, int64_t now, const vg_st_req_t *r)
{
    begin(st, "stimulus_started", now);
    add(st, ",\"request_id\":\"%s\",\"level\":%u,\"duration_ms\":%lu", r->id, r->level,
        (unsigned long)r->duration_ms);
    finish(st);
}

static void ev_ended(vg_station_t *st, int64_t now, const vg_st_req_t *r, double on_ms)
{
    begin(st, "stimulus_ended", now);
    add(st, ",\"request_id\":\"%s\",\"outcome\":\"%s\",\"requested_ms\":%lu,\"on_ms\":%.3f",
        r->id, vg_st_outcome_name(r->outcome), (unsigned long)r->duration_ms, on_ms);
    finish(st);
}

/* ---- control ---------------------------------------------------------- */

static void enter_fault(vg_station_t *st, const char *detail)
{
    st->state = VG_ST_FAULT;
    st->n_faults++;
    snprintf(st->fault_detail, sizeof st->fault_detail, "%s", detail);
}

void vg_station_stop(vg_station_t *st, vg_st_outcome_t why, int64_t now_us)
{
    bool ok = st->hap.off(st->hap.ctx);
    if (!ok) ok = st->hap.off(st->hap.ctx);          /* one immediate retry */
    if (st->active) {
        vg_st_req_t *r = st->active;
        r->outcome = why;
        st->active = NULL;
        if (why == VG_OUT_COMPLETED) st->n_completed++;
        if (why == VG_OUT_ABORTED) st->n_aborted++;
        if (why == VG_OUT_LINK_TIMEOUT) st->n_link_timeouts++;
        if (st->state == VG_ST_ACTIVE) st->state = VG_ST_IDLE;
        ev_ended(st, now_us, r, (double)(now_us - st->started_us) / 1000.0);
    }
    if (!ok) {
        enter_fault(st, "driver off() failed");
        ev_status(st, now_us);
    }
}

bool vg_station_init(vg_station_t *st, const char *device_id, const char *boot_id,
                     const char *fw_version, vg_haptic_t hap, vg_st_limits_t lim,
                     bool motor_permitted, vg_st_emit_t emit, void *emit_ctx, int64_t now_us)
{
    if (!st || !emit || !hap.init || !hap.on || !hap.off || !hap.check ||
        !vg_tm_valid_id(device_id) || !vg_tm_valid_id(boot_id) || lim.max_duration_ms == 0 ||
        lim.max_duration_ms > VG_ST_HARD_MAX_MS || lim.heartbeat_timeout_ms == 0 ||
        lim.hello_period_ms == 0 || lim.fault_poll_ms == 0) {
        return false;
    }
    memset(st, 0, sizeof *st);
    snprintf(st->device_id, sizeof st->device_id, "%s", device_id);
    snprintf(st->boot_id, sizeof st->boot_id, "%s", boot_id);
    snprintf(st->fw_version, sizeof st->fw_version, "%s", fw_version);
    st->hap = hap;
    st->lim = lim;
    st->emit = emit;
    st->emit_ctx = emit_ctx;
    st->last_rx_us = now_us;

    char detail[96] = "";
    bool init_ok = hap.init(hap.ctx, detail, sizeof detail);
    bool off_ok = hap.off(hap.ctx);                   /* output off at boot, always */
    if (!motor_permitted) {
        st->state = VG_ST_DISABLED;
        snprintf(st->fault_detail, sizeof st->fault_detail, "motor not permitted by board config");
    } else if (!init_ok || !off_ok) {
        st->state = VG_ST_DISABLED;
        snprintf(st->fault_detail, sizeof st->fault_detail, "driver init: %.70s",
                 init_ok ? "off() failed" : detail);
    } else {
        st->state = VG_ST_IDLE;
    }
    return true;
}

static vg_st_req_t *find(vg_station_t *st, const char *id)
{
    for (int k = 0; k < VG_ST_RECENT; k++) {
        if (st->recent[k].id[0] && strcmp(st->recent[k].id, id) == 0) return &st->recent[k];
    }
    return NULL;
}

static vg_st_req_t *record(vg_station_t *st, const char *id, uint8_t kind, uint16_t level,
                           uint32_t dur)
{
    int idx = st->recent_next;
    if (&st->recent[idx] == st->active) idx = (idx + 1) % VG_ST_RECENT;  /* never evict active */
    st->recent_next = (idx + 1) % VG_ST_RECENT;
    vg_st_req_t *r = &st->recent[idx];
    memset(r, 0, sizeof *r);
    snprintf(r->id, sizeof r->id, "%s", id);
    r->kind = kind;
    r->level = level;
    r->duration_ms = dur;
    return r;
}

static bool allowed_key(const char *k, uint8_t kind)
{
    static const char *const common[] = {"schema", "version", "type", "request_id"};
    for (size_t i = 0; i < 4; i++) if (strcmp(k, common[i]) == 0) return true;
    return kind == K_STIMULUS && (strcmp(k, "level") == 0 || strcmp(k, "duration_ms") == 0);
}

static void handle_line(vg_station_t *st, const char *line, size_t len, int64_t now)
{
    const char *err = NULL;
    if (!vg_jf_parse(line, len, &st->obj, &err)) {
        st->n_malformed++;
        ev_error(st, now, "malformed", err, NULL);
        return;
    }
    const vg_jf_obj_t *o = &st->obj;
    const vg_jf_kv_t *rid = vg_jf_get(o, "request_id");
    const char *id = (rid && rid->type == VG_JF_STRING && vg_tm_valid_id(rid->str)) ? rid->str : NULL;
    const vg_jf_kv_t *v = vg_jf_get(o, "schema");
    if (!v || v->type != VG_JF_STRING || strcmp(v->str, CMD_SCHEMA) != 0) {
        st->n_malformed++;
        ev_error(st, now, "wrong_schema", "schema must be " CMD_SCHEMA, id);
        return;
    }
    v = vg_jf_get(o, "version");
    if (!v || v->type != VG_JF_INT || v->i != 1) {
        st->n_malformed++;
        ev_error(st, now, "unsupported_version", "this device supports version 1", id);
        return;
    }
    v = vg_jf_get(o, "type");
    uint8_t kind = 0;
    if (v && v->type == VG_JF_STRING) {
        kind = strcmp(v->str, "stimulus") == 0 ? K_STIMULUS
             : strcmp(v->str, "abort") == 0    ? K_ABORT
             : strcmp(v->str, "status") == 0   ? K_STATUS
                                               : 0;
    }
    if (!kind) {
        st->n_malformed++;
        ev_error(st, now, "unknown_type", "type must be stimulus, abort or status", id);
        return;
    }
    if (!id) {
        st->n_malformed++;
        ev_error(st, now, "invalid_request_id", "request_id must be 1-64 chars of [A-Za-z0-9._:-]", NULL);
        return;
    }
    for (int k = 0; k < o->n; k++) {
        if (!allowed_key(o->kv[k].key, kind)) {
            char d[64];
            snprintf(d, sizeof d, "unknown field '%.40s'", o->kv[k].key);
            st->n_malformed++;
            ev_error(st, now, "unknown_field", d, id);
            return;
        }
    }
    int64_t level = 0, dur = 0;
    if (kind == K_STIMULUS) {
        const vg_jf_kv_t *lv = vg_jf_get(o, "level"), *dv = vg_jf_get(o, "duration_ms");
        if (!lv || lv->type != VG_JF_INT || !dv || dv->type != VG_JF_INT) {
            st->n_malformed++;
            ev_error(st, now, "malformed", "stimulus needs integer level and duration_ms", id);
            return;
        }
        level = lv->i;
        dur = dv->i;
    }

    /* A well-formed command: counts as host activity (heartbeat). */
    st->last_rx_us = now;
    st->n_commands++;

    vg_st_req_t *prev = find(st, id);
    if (prev) {
        bool same = prev->kind == kind &&
                    (kind != K_STIMULUS || (prev->level == level && prev->duration_ms == dur));
        if (same) {
            ev_ack(st, now, prev, "duplicate", NULL);   /* never restarts or extends */
        } else {
            vg_st_req_t tmp = *prev;
            tmp.kind = kind;
            st->n_rejected++;
            ev_ack(st, now, &tmp, "rejected", "request_id_reused");
        }
        return;
    }

    if (kind == K_STATUS) {
        vg_st_req_t *r = record(st, id, kind, 0, 0);
        r->outcome = VG_OUT_COMPLETED;
        ev_ack(st, now, r, "accepted", NULL);
        ev_status(st, now);
        return;
    }
    if (kind == K_ABORT) {
        vg_st_req_t *r = record(st, id, kind, 0, 0);
        r->outcome = VG_OUT_COMPLETED;
        ev_ack(st, now, r, "accepted", NULL);
        if (st->active) vg_station_stop(st, VG_OUT_ABORTED, now);
        return;
    }

    /* stimulus */
    const char *why = NULL;
    if (st->state == VG_ST_DISABLED) why = "motor_disabled";
    else if (st->state == VG_ST_FAULT) why = "fault";
    else if (st->state == VG_ST_ACTIVE) why = "busy";
    else if (level < 1 || level > 100) why = "level_out_of_range";
    else if (dur < 1 || dur > (int64_t)st->lim.max_duration_ms) why = "duration_out_of_range";
    uint16_t lv16 = (level >= 0 && level <= 1000) ? (uint16_t)level : 0;
    uint32_t du32 = (dur >= 0 && dur <= 3600000) ? (uint32_t)dur : 0;
    vg_st_req_t *r = record(st, id, kind, lv16, du32);
    if (why) {
        r->outcome = VG_OUT_REJECTED;
        st->n_rejected++;
        ev_ack(st, now, r, "rejected", why);
        return;
    }
    r->outcome = VG_OUT_IN_PROGRESS;
    ev_ack(st, now, r, "accepted", NULL);
    uint8_t rtp = (uint8_t)((level * VG_HAPTIC_LEVEL_MAX + 50) / 100);
    if (!st->hap.on(st->hap.ctx, rtp)) {
        st->hap.off(st->hap.ctx);
        r->outcome = VG_OUT_FAULT;
        enter_fault(st, "driver on() failed");
        ev_ended(st, now, r, 0.0);
        ev_status(st, now);
        return;
    }
    st->active = r;
    st->state = VG_ST_ACTIVE;
    st->started_us = now;
    st->end_us = now + (int64_t)dur * 1000;   /* fixed once: never extended */
    st->next_poll_us = now + (int64_t)st->lim.fault_poll_ms * 1000;
    ev_started(st, now, r);
}

void vg_station_rx(vg_station_t *st, const uint8_t *data, size_t n, int64_t now_us)
{
    for (size_t i = 0; i < n; i++) {
        char c = (char)data[i];
        if (c == '\n') {
            if (st->discarding) {
                st->n_oversized++;
                ev_error(st, now_us, "oversized", "command line exceeds 512 bytes; discarded", NULL);
            } else if (st->line_len > 0) {
                handle_line(st, st->line, st->line_len, now_us);
            }
            st->line_len = 0;
            st->discarding = false;
            continue;
        }
        if (c == '\r' || st->discarding) continue;
        if (st->line_len + 1 >= VG_ST_LINE_MAX) {   /* +1: the '\n' counts toward the limit */
            st->discarding = true;
            st->line_len = 0;
            continue;
        }
        st->line[st->line_len++] = c;
    }
}

void vg_station_tick(vg_station_t *st, int64_t now)
{
    if (!st->hello_sent || now >= st->next_hello_us) {
        ev_hello(st, now);
        if (st->seq == 1) ev_status(st, now);   /* boot: hello then status */
    }
    if (st->state != VG_ST_ACTIVE) {
        return;
    }
    if (now >= st->end_us) {
        vg_station_stop(st, VG_OUT_COMPLETED, now);
        return;
    }
    if (now - st->last_rx_us > (int64_t)st->lim.heartbeat_timeout_ms * 1000) {
        vg_station_stop(st, VG_OUT_LINK_TIMEOUT, now);
        return;
    }
    if (now >= st->next_poll_us) {
        char d[96] = "";
        st->next_poll_us = now + (int64_t)st->lim.fault_poll_ms * 1000;
        if (!st->hap.check(st->hap.ctx, d, sizeof d)) {
            vg_station_stop(st, VG_OUT_FAULT, now);
            enter_fault(st, d);
            ev_status(st, now);
        }
    }
}
