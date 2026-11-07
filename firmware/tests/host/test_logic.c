/* Host tests for the portable logic: chip table, pin rules, motor gating,
 * fake sources. Configurations here are test fixtures, not board facts. */
#include <stdbool.h>

#include "vg/chip_caps.h"
#include "vg/fake_source.h"
#include "vg/pin_check.h"
#include "vg_test.h"

static vg_hw_config_t base(vg_role_t role)
{
    vg_hw_config_t c = {
        .board_name = "test",
        .description = "test fixture",
        .role = role,
        .chip = VG_CHIP_ESP32,
        .chip_status = VG_FACT_CONFIRMED,
        .chip_evidence = "fixture",
        .gpio16_17_free_status = VG_FACT_UNKNOWN,
        .emg = {.gpio = VG_PIN_NONE},
        .imu = {.sda_gpio = VG_PIN_NONE, .scl_gpio = VG_PIN_NONE, .address = 0x68},
        .motor = {.gpio = VG_PIN_NONE},
        .velostat = {.gpio = VG_PIN_NONE},
        .dht11 = {.gpio = VG_PIN_NONE},
    };
    return c;
}

static vg_hw_config_t confirmed_motor(void)
{
    vg_hw_config_t c = base(VG_ROLE_STATION);
    c.motor = (vg_motor_cfg_t){.enabled = true, .gpio = 25, .active_high = true,
                               .actuator = VG_ACTUATOR_ERM_BRUSHED,
                               .driver_confirmed = true, .max_pulse_ms = 500};
    return c;
}

static vg_issue_t issues[32];

static bool has(int n, const char *code, vg_severity_t sev)
{
    for (int i = 0; i < n && i < 32; i++) {
        if (strcmp(issues[i].code, code) == 0 && issues[i].severity == sev) {
            return true;
        }
    }
    return false;
}

static int check(const vg_hw_config_t *c)
{
    return vg_pin_check(c, issues, 32);
}

static void test_chip_table_matches_docs(void)
{
    vg_gpio_caps_t k;
    /* ADC1 mapping, ESP-IDF v6.1 ESP32 GPIO table */
    const int adc1[][2] = {{32, 4}, {33, 5}, {34, 6}, {35, 7}, {36, 0}, {37, 1}, {38, 2}, {39, 3}};
    for (unsigned i = 0; i < sizeof adc1 / sizeof adc1[0]; i++) {
        CHECK(vg_chip_gpio_caps(VG_CHIP_ESP32, adc1[i][0], &k));
        CHECK(k.adc_unit == 1 && k.adc_channel == adc1[i][1]);
    }
    const int adc2[][2] = {{0, 1}, {2, 2}, {4, 0}, {12, 5}, {13, 4},
                           {14, 6}, {15, 3}, {25, 8}, {26, 9}, {27, 7}};
    for (unsigned i = 0; i < sizeof adc2 / sizeof adc2[0]; i++) {
        CHECK(vg_chip_gpio_caps(VG_CHIP_ESP32, adc2[i][0], &k));
        CHECK(k.adc_unit == 2 && k.adc_channel == adc2[i][1]);
    }
    for (int g = 34; g <= 39; g++) {
        vg_chip_gpio_caps(VG_CHIP_ESP32, g, &k);
        CHECK(k.flags & VG_GPIO_INPUT_ONLY);
    }
    const int straps[] = {0, 2, 5, 12, 15};
    for (unsigned i = 0; i < 5; i++) {
        vg_chip_gpio_caps(VG_CHIP_ESP32, straps[i], &k);
        CHECK(k.flags & VG_GPIO_STRAPPING);
    }
    for (int g = 6; g <= 11; g++) {
        vg_chip_gpio_caps(VG_CHIP_ESP32, g, &k);
        CHECK(k.flags & VG_GPIO_FLASH);
    }
    const int absent[] = {20, 24, 28, 29, 30, 31};
    for (unsigned i = 0; i < 6; i++) {
        vg_chip_gpio_caps(VG_CHIP_ESP32, absent[i], &k);
        CHECK(!(k.flags & VG_GPIO_EXISTS));
    }
    CHECK(!vg_chip_gpio_caps(VG_CHIP_ESP32, 40, &k));
    CHECK(!vg_chip_gpio_caps(VG_CHIP_UNKNOWN, 21, &k));
}

static void test_clean_config_has_no_errors(void)
{
    vg_hw_config_t c = base(VG_ROLE_WEARABLE);
    c.emg = (vg_adc_input_cfg_t){.enabled = true, .gpio = 36, .atten = VG_ATTEN_DB_12};
    c.imu = (vg_i2c_imu_cfg_t){.enabled = true, .sda_gpio = 21, .scl_gpio = 22,
                               .address = 0x68, .clock_hz = 400000};
    int n = check(&c);
    CHECK(vg_issue_error_count(issues, n) == 0);
    CHECK(n == 0);
}

static void test_pin_rules(void)
{
    vg_hw_config_t c = base(VG_ROLE_WEARABLE);
    c.imu = (vg_i2c_imu_cfg_t){.enabled = true, .sda_gpio = 34, .scl_gpio = 7, .address = 0x68};
    int n = check(&c);
    CHECK(has(n, "pin_input_only", VG_SEV_ERROR));
    CHECK(has(n, "pin_flash", VG_SEV_ERROR));

    c = base(VG_ROLE_WEARABLE);
    c.emg = (vg_adc_input_cfg_t){.enabled = true, .gpio = 21, .atten = VG_ATTEN_DB_12};
    n = check(&c);
    CHECK(has(n, "pin_not_adc", VG_SEV_ERROR));

    c.emg.gpio = 25;
    n = check(&c);
    CHECK(has(n, "adc2_wifi_conflict", VG_SEV_WARNING));
    CHECK(vg_issue_error_count(issues, n) == 0);

    c.emg.gpio = 1;
    n = check(&c);
    CHECK(has(n, "pin_uart0", VG_SEV_ERROR));

    c.emg.gpio = 20;
    n = check(&c);
    CHECK(has(n, "pin_absent", VG_SEV_ERROR));

    c = base(VG_ROLE_WEARABLE);
    c.imu = (vg_i2c_imu_cfg_t){.enabled = true, .sda_gpio = 16, .scl_gpio = 2, .address = 0x68};
    n = check(&c);
    CHECK(has(n, "pin_psram_maybe", VG_SEV_WARNING));
    CHECK(has(n, "strapping", VG_SEV_WARNING));
    c.gpio16_17_free_status = VG_FACT_CONFIRMED;
    n = check(&c);
    CHECK(!has(n, "pin_psram_maybe", VG_SEV_WARNING));
}

static void test_conflicts_and_unset(void)
{
    vg_hw_config_t c = base(VG_ROLE_STATION);
    c.imu = (vg_i2c_imu_cfg_t){.enabled = true, .sda_gpio = 21, .scl_gpio = 22, .address = 0x68};
    c.velostat = (vg_adc_input_cfg_t){.enabled = true, .gpio = 21, .atten = VG_ATTEN_DB_12};
    int n = check(&c);
    CHECK(has(n, "pin_conflict", VG_SEV_ERROR));

    c = base(VG_ROLE_WEARABLE);
    c.emg.enabled = true; /* no pin */
    n = check(&c);
    CHECK(has(n, "pin_unset", VG_SEV_ERROR));
    CHECK(has(n, "atten_unset", VG_SEV_ERROR));

    c = base(VG_ROLE_STATION);
    c.imu = (vg_i2c_imu_cfg_t){.enabled = true, .sda_gpio = 21, .scl_gpio = 22, .address = 0x3c};
    c.oled = (vg_oled_cfg_t){.enabled = true, .address = 0x3c};
    n = check(&c);
    CHECK(has(n, "i2c_address_conflict", VG_SEV_ERROR));
    c.imu.enabled = false;
    n = check(&c);
    CHECK(has(n, "oled_no_bus", VG_SEV_ERROR));
}

static void test_unknown_chip(void)
{
    vg_hw_config_t c = base(VG_ROLE_WEARABLE);
    c.chip = VG_CHIP_UNKNOWN;
    int n = check(&c);
    CHECK(n == 0); /* nothing assigned, nothing to validate */
    c.emg.gpio = 36;
    n = check(&c);
    CHECK(has(n, "chip_unknown", VG_SEV_ERROR));

    c = base(VG_ROLE_WEARABLE);
    c.chip_status = VG_FACT_PROVISIONAL;
    n = check(&c);
    CHECK(has(n, "chip_unconfirmed", VG_SEV_WARNING));
}

static void test_motor_gating(void)
{
    vg_hw_config_t c = confirmed_motor();
    CHECK(vg_motor_output_permitted(&c));

    c = confirmed_motor();
    c.motor.enabled = false;
    CHECK(!vg_motor_output_permitted(&c));

    c = confirmed_motor();
    c.motor.driver_confirmed = false;
    CHECK(!vg_motor_output_permitted(&c));
    CHECK(has(check(&c), "driver_unconfirmed", VG_SEV_ERROR));

    c = confirmed_motor();
    c.motor.actuator = VG_ACTUATOR_UNKNOWN;
    CHECK(!vg_motor_output_permitted(&c));

    c = confirmed_motor();
    c.motor.actuator = VG_ACTUATOR_LRA;
    CHECK(!vg_motor_output_permitted(&c));
    CHECK(has(check(&c), "actuator_lra", VG_SEV_ERROR));

    c = confirmed_motor();
    c.motor.max_pulse_ms = VG_MOTOR_MAX_PULSE_CEILING_MS + 1;
    CHECK(!vg_motor_output_permitted(&c));
    c.motor.max_pulse_ms = 0;
    CHECK(!vg_motor_output_permitted(&c));

    c = confirmed_motor();
    c.motor.gpio = 12; /* strapping */
    CHECK(!vg_motor_output_permitted(&c));
    c.motor.gpio = 34; /* input-only */
    CHECK(!vg_motor_output_permitted(&c));

    c = confirmed_motor();
    c.role = VG_ROLE_WEARABLE;
    CHECK(!vg_motor_output_permitted(&c));

    c = confirmed_motor();
    c.chip_status = VG_FACT_PROVISIONAL; /* warning only: chip status alone does not gate */
    CHECK(vg_motor_output_permitted(&c));
    c.chip = VG_CHIP_UNKNOWN;
    CHECK(!vg_motor_output_permitted(&c));
}

static void test_fake_sources_deterministic_and_bounded(void)
{
    vg_fake_emg_t a, b;
    vg_fake_emg_init(&a, 42);
    vg_fake_emg_init(&b, 42);
    bool same = true, in_range = true, varies = false;
    int32_t first = vg_fake_emg_next(&a);
    (void)vg_fake_emg_next(&b);
    for (int i = 0; i < 5000; i++) {
        int32_t x = vg_fake_emg_next(&a), y = vg_fake_emg_next(&b);
        same &= x == y;
        in_range &= x >= 0 && x <= 4095;
        varies |= x != first;
    }
    CHECK(same && in_range && varies);
    CHECK(a.index == 5001);

    vg_fake_imu_t f;
    vg_fake_imu_init(&f, 7);
    int32_t v[3];
    vg_fake_imu_next(&f, v);
    CHECK(v[2] > 4000 && v[2] < 4200);
    CHECK(strcmp(VG_FAKE_PROVENANCE, "simulated") == 0);
}

static void sink(void *ctx, const char *line)
{
    int *n = ctx;
    (*n)++;
    CHECK(line != NULL && strlen(line) < 160);
}

static void test_report_runs(void)
{
    vg_hw_config_t c = confirmed_motor();
    int lines = 0;
    vg_config_report(&c, sink, &lines);
    CHECK(lines >= 9);
}

int main(void)
{
    RUN(test_chip_table_matches_docs);
    RUN(test_clean_config_has_no_errors);
    RUN(test_pin_rules);
    RUN(test_conflicts_and_unset);
    RUN(test_unknown_chip);
    RUN(test_motor_gating);
    RUN(test_fake_sources_deterministic_and_bounded);
    RUN(test_report_runs);
    return DONE();
}
