/* Host tests for the station controller (fake haptic driver, simulated time).
 * With "--emit FILE", every event line produced is also written to FILE so
 * check_station.py can validate it against the protocol schema. */
#include <stdio.h>
#include <stdlib.h>

#include "vg/station.h"
#include "vg_test.h"

static FILE *emit_file;
static char events[64][VG_ST_EVENT_MAX];
static int n_ev;

static void sink(void *ctx, const char *line, size_t len)
{
    (void)ctx;
    if (emit_file) fwrite(line, 1, len, emit_file);
    snprintf(events[n_ev % 64], sizeof events[0], "%.*s", (int)len, line);   /* keep last 64 */
    n_ev++;
}

static int count(const char *needle)
{
    int c = 0;
    for (int i = 0; i < n_ev && i < 64; i++) c += strstr(events[i], needle) != NULL;
    return c;
}

static const char *last(void) { return n_ev ? events[(n_ev - 1) % 64] : ""; }

static vg_station_t st;
static vg_fake_haptic_t fk;
static int64_t now;

static const vg_st_limits_t LIM = {1000, 1500, 5000, 50};

static void boot(bool permitted)
{
    n_ev = 0;
    now = 1000;
    vg_haptic_t h = vg_fake_haptic(&fk);
    static int boots;
    char bid[32];
    snprintf(bid, sizeof bid, "boot-test-%d", ++boots);   /* every boot: a new boot_id */
    CHECK(vg_station_init(&st, "fake-station-01", bid, "0.3.0", h, LIM, permitted, sink, NULL,
                          now));
    vg_station_tick(&st, now);
}

static void send(const char *line)
{
    vg_station_rx(&st, (const uint8_t *)line, strlen(line), now);
}

static void advance(int64_t us, int64_t step)
{
    for (int64_t t = 0; t < us; t += step) {
        now += step;
        vg_station_tick(&st, now);
    }
}

#define CMD(type, id, extra)                                                               \
    "{\"schema\":\"vibeguard.station_command\",\"version\":1,\"type\":\"" type             \
    "\",\"request_id\":\"" id "\"" extra "}\n"
#define STIM(id, lvl, dur) CMD("stimulus", id, ",\"level\":" #lvl ",\"duration_ms\":" #dur)

static void test_boot_state(void)
{
    boot(true);
    CHECK(fk.init_calls == 1 && fk.off_calls >= 1 && !fk.output_on);   /* off at boot */
    CHECK(count("\"type\":\"hello\"") == 1 && count("\"type\":\"status\"") == 1);
    CHECK(strstr(events[0], "\"seq\":0") && strstr(events[1], "\"seq\":1"));
    CHECK(n_ev <= 64 && count("\"source\":\"simulated\"") == n_ev);          /* fake driver -> simulated */
    CHECK(st.state == VG_ST_IDLE);
}

static void test_stimulus_completes_on_time(void)
{
    boot(true);
    send(STIM("t1", 50, 300));
    CHECK(count("\"result\":\"accepted\"") == 1 && count("stimulus_started") == 1);
    CHECK(fk.output_on && fk.level == 64);                       /* 50 % of 127, rounded */
    advance(299000, 1000);
    CHECK(fk.output_on);
    advance(1000, 1000);
    CHECK(!fk.output_on && st.state == VG_ST_IDLE);
    CHECK(strstr(last(), "\"outcome\":\"completed\"") && strstr(last(), "\"on_ms\":300.000"));
}

static void test_abort_stops_promptly(void)
{
    boot(true);
    send(STIM("t2", 100, 1000));
    CHECK(fk.output_on);
    advance(100000, 1000);
    send(CMD("abort", "a1", ""));                                /* handled within this call */
    CHECK(!fk.output_on && st.state == VG_ST_IDLE);
    CHECK(strstr(last(), "\"outcome\":\"aborted\"") && strstr(last(), "\"on_ms\":100.000"));
    send(CMD("abort", "a2", ""));                                /* abort when idle: harmless */
    CHECK(strstr(last(), "\"result\":\"accepted\"") && !fk.output_on);
}

static void test_duplicates_never_restart_or_extend(void)
{
    boot(true);
    send(STIM("t3", 40, 500));
    advance(400000, 1000);
    uint32_t ons = fk.on_calls;
    send(STIM("t3", 40, 500));                                   /* duplicate while active */
    CHECK(strstr(last(), "\"result\":\"duplicate\"") && strstr(last(), "\"outcome\":\"in_progress\""));
    CHECK(fk.on_calls == ons);
    advance(100000, 1000);
    CHECK(!fk.output_on && strstr(last(), "\"on_ms\":500.000"));   /* not extended */
    send(STIM("t3", 40, 500));                                   /* duplicate after completion */
    CHECK(strstr(last(), "\"outcome\":\"completed\"") && !fk.output_on && fk.on_calls == ons);
    send(STIM("t3", 90, 500));                                   /* same id, other params */
    CHECK(strstr(last(), "\"result\":\"rejected\"") && strstr(last(), "request_id_reused"));
    send(CMD("abort", "a3", ""));
    send(STIM("t4", 40, 300));
    advance(50000, 1000);
    int ended = count("stimulus_ended");
    send(CMD("abort", "a3", ""));                                /* duplicate abort: no action */
    CHECK(fk.output_on && count("stimulus_ended") == ended);
}

static void test_busy_rejected(void)
{
    boot(true);
    send(STIM("t5", 30, 800));
    send(STIM("t6", 30, 800));
    CHECK(strstr(last(), "\"request_id\":\"t6\"") && strstr(last(), "\"reason\":\"busy\""));
    CHECK(fk.on_calls == 1);
}

static void test_bounds(void)
{
    boot(true);
    send(STIM("b1", 0, 100));
    CHECK(strstr(last(), "level_out_of_range"));
    send(STIM("b2", 101, 100));
    CHECK(strstr(last(), "level_out_of_range"));
    send(STIM("b3", 50, 1001));                                  /* > configured max */
    CHECK(strstr(last(), "duration_out_of_range"));
    send(STIM("b4", 50, 0));
    CHECK(strstr(last(), "duration_out_of_range"));
    CHECK(fk.on_calls == 0);
    /* limits above the hard ceiling are refused at init */
    vg_st_limits_t bad = {VG_ST_HARD_MAX_MS + 1, 1500, 5000, 50};
    vg_haptic_t h = vg_fake_haptic(&fk);
    CHECK(!vg_station_init(&st, "x", "y", "0", h, bad, true, sink, NULL, 0));
}

static void test_malformed_and_oversized(void)
{
    boot(true);
    const char *bad[] = {
        "not json\n",
        "{\"schema\":\"vibeguard.station_command\",\"version\":1,\"type\":\"stimulus\"\n",
        "{\"schema\":\"other\",\"version\":1,\"type\":\"status\",\"request_id\":\"m1\"}\n",
        "{\"schema\":\"vibeguard.station_command\",\"version\":2,\"type\":\"status\",\"request_id\":\"m2\"}\n",
        "{\"schema\":\"vibeguard.station_command\",\"version\":1,\"type\":\"fire\",\"request_id\":\"m3\"}\n",
        "{\"schema\":\"vibeguard.station_command\",\"version\":1,\"type\":\"status\",\"request_id\":\"bad id\"}\n",
        CMD("status", "m4", ",\"extra\":1"),
        CMD("stimulus", "m5", ",\"level\":\"50\",\"duration_ms\":100"),
        CMD("stimulus", "m6", ",\"level\":50.5,\"duration_ms\":100"),
        CMD("stimulus", "m7", ",\"level\":50"),
        CMD("status", "m8", ",\"nested\":{\"a\":1}"),
        "{\"schema\":\"vibeguard.station_command\",\"schema\":\"x\"}\n",
        "{\"a\":\"x\\\"y\"}\n",
    };
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        int before = count("\"type\":\"error\"");
        send(bad[i]);
        CHECK(count("\"type\":\"error\"") == before + 1);
    }
    CHECK(fk.on_calls == 0 && st.n_malformed == sizeof bad / sizeof bad[0]);
    char big[700];
    memset(big, 'x', sizeof big);
    big[sizeof big - 1] = '\n';
    vg_station_rx(&st, (const uint8_t *)big, sizeof big, now);
    CHECK(strstr(last(), "\"reason\":\"oversized\"") && st.n_oversized == 1);
    send(STIM("ok1", 20, 100));                                  /* parser recovers */
    CHECK(fk.output_on);
    /* fragmented delivery: bytes split across rx calls */
    boot(true);
    const char *s = STIM("frag", 20, 100);
    vg_station_rx(&st, (const uint8_t *)s, 10, now);
    CHECK(!fk.output_on);
    vg_station_rx(&st, (const uint8_t *)s + 10, strlen(s) - 10, now);
    CHECK(fk.output_on);
}

static void test_link_timeout(void)
{
    /* heartbeat shorter than the pulse: link loss ends the stimulus */
    vg_st_limits_t lim = {1000, 200, 5000, 50};
    n_ev = 0;
    now = 1000;
    vg_haptic_t h = vg_fake_haptic(&fk);
    vg_station_init(&st, "fake-station-01", "boot-lt", "0.3.0", h, lim, true, sink, NULL, now);
    vg_station_tick(&st, now);
    send(STIM("lt1", 60, 1000));
    advance(150000, 1000);
    send(CMD("status", "hb1", ""));                              /* heartbeat keeps it alive */
    advance(150000, 1000);
    CHECK(fk.output_on);
    advance(60000, 1000);                                        /* > 200 ms since hb1 */
    CHECK(!fk.output_on && count("\"outcome\":\"link_timeout\"") == 1);
    CHECK(st.state == VG_ST_IDLE);
}

static void test_faults_force_off(void)
{
    boot(true);
    send(STIM("f1", 60, 800));
    fk.fail_check = true;
    advance(60000, 1000);
    CHECK(!fk.output_on && st.state == VG_ST_FAULT && count("\"outcome\":\"fault\"") == 1);
    send(STIM("f2", 60, 100));
    CHECK(strstr(last(), "\"reason\":\"fault\"") && !fk.output_on);   /* latched */

    boot(true);
    fk.fail_on = true;
    send(STIM("f3", 60, 100));
    CHECK(!fk.output_on && st.state == VG_ST_FAULT);

    boot(true);
    send(STIM("f4", 60, 100));
    fk.fail_off = true;
    advance(100000, 1000);
    CHECK(st.state == VG_ST_FAULT);                              /* off failed: visible fault */
}

static void test_disabled_and_init_failure(void)
{
    boot(false);
    CHECK(st.state == VG_ST_DISABLED);
    send(STIM("d1", 50, 100));
    CHECK(strstr(last(), "motor_disabled") && fk.on_calls == 0);
    send(CMD("status", "d2", ""));
    CHECK(strstr(last(), "\"state\":\"disabled\""));

    n_ev = 0;
    vg_haptic_t h = vg_fake_haptic(&fk);
    fk.fail_init = true;
    vg_station_init(&st, "fake-station-01", "boot-if", "0.3.0", h, LIM, true, sink, NULL, now);
    CHECK(st.state == VG_ST_DISABLED && fk.off_calls >= 1);
}

static void test_request_memory_keeps_active(void)
{
    boot(true);
    send(STIM("keep", 50, 1000));
    char id[16];
    for (int i = 0; i < VG_ST_RECENT + 5; i++) {
        snprintf(id, sizeof id, "s%d", i);
        char line[160];
        snprintf(line, sizeof line, CMD("status", "%s", ""), id);
        send(line);
    }
    CHECK(st.active && strcmp(st.active->id, "keep") == 0);
    send(CMD("abort", "end", ""));
    CHECK(!fk.output_on && strstr(last(), "\"request_id\":\"keep\""));
}

int main(int argc, char **argv)
{
    if (argc == 3 && strcmp(argv[1], "--emit") == 0) {
        emit_file = fopen(argv[2], "w");
        if (!emit_file) return 2;
    }
    RUN(test_boot_state);
    RUN(test_stimulus_completes_on_time);
    RUN(test_abort_stops_promptly);
    RUN(test_duplicates_never_restart_or_extend);
    RUN(test_busy_rejected);
    RUN(test_bounds);
    RUN(test_malformed_and_oversized);
    RUN(test_link_timeout);
    RUN(test_faults_force_off);
    RUN(test_disabled_and_init_failure);
    RUN(test_request_memory_keeps_active);
    if (emit_file) fclose(emit_file);
    return DONE();
}
