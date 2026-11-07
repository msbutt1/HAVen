#include "vg/pin_check.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "vg/chip_caps.h"

typedef enum {
    USE_ADC_IN,      /* analog input */
    USE_OPEN_DRAIN,  /* I2C / DHT: needs output drive + external pull-up */
    USE_OUTPUT,      /* motor driver input */
} use_kind_t;

typedef struct {
    const char *name;
    int gpio;
    bool enabled;
    use_kind_t kind;
} pin_use_t;

typedef struct {
    vg_issue_t *out;
    int max;
    int n;
} acc_t;

static void add(acc_t *a, vg_severity_t sev, const char *code, const char *use, int gpio,
                const char *fmt, ...)
{
    if (a->n < a->max) {
        vg_issue_t *is = &a->out[a->n];
        is->severity = sev;
        is->code = code;
        is->use = use;
        is->gpio = gpio;
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(is->message, sizeof is->message, fmt, ap);
        va_end(ap);
    }
    a->n++;
}

static int collect_uses(const vg_hw_config_t *c, pin_use_t *u)
{
    int n = 0;
    u[n++] = (pin_use_t){"emg", c->emg.gpio, c->emg.enabled, USE_ADC_IN};
    u[n++] = (pin_use_t){"imu.sda", c->imu.sda_gpio, c->imu.enabled, USE_OPEN_DRAIN};
    u[n++] = (pin_use_t){"imu.scl", c->imu.scl_gpio, c->imu.enabled, USE_OPEN_DRAIN};
    u[n++] = (pin_use_t){"motor", c->motor.gpio, c->motor.enabled, USE_OUTPUT};
    u[n++] = (pin_use_t){"velostat", c->velostat.gpio, c->velostat.enabled, USE_ADC_IN};
    u[n++] = (pin_use_t){"dht11", c->dht11.gpio, c->dht11.enabled, USE_OPEN_DRAIN};
    return n;
}

static void check_pin(acc_t *a, const vg_hw_config_t *c, const pin_use_t *u)
{
    vg_gpio_caps_t caps;
    if (!vg_chip_gpio_caps(c->chip, u->gpio, &caps) || !(caps.flags & VG_GPIO_EXISTS)) {
        add(a, VG_SEV_ERROR, "pin_absent", u->name, u->gpio,
            "GPIO%d does not exist on %s", u->gpio, vg_chip_name(c->chip));
        return;
    }
    if (caps.flags & VG_GPIO_FLASH) {
        add(a, VG_SEV_ERROR, "pin_flash", u->name, u->gpio,
            "GPIO%d is wired to the module's SPI flash", u->gpio);
    }
    if (caps.flags & VG_GPIO_UART0) {
        add(a, VG_SEV_ERROR, "pin_uart0", u->name, u->gpio,
            "GPIO%d is UART0 (flashing / USB-serial telemetry)", u->gpio);
    }
    if ((caps.flags & VG_GPIO_PSRAM) && c->gpio16_17_free_status != VG_FACT_CONFIRMED) {
        add(a, VG_SEV_WARNING, "pin_psram_maybe", u->name, u->gpio,
            "GPIO%d is PSRAM on some modules; module PSRAM not confirmed absent", u->gpio);
    }
    if ((caps.flags & VG_GPIO_INPUT_ONLY) && u->kind != USE_ADC_IN) {
        add(a, VG_SEV_ERROR, "pin_input_only", u->name, u->gpio,
            "GPIO%d is input-only; %s needs output drive", u->gpio, u->name);
    }
    if (caps.flags & VG_GPIO_STRAPPING) {
        if (u->kind == USE_OUTPUT) {
            add(a, VG_SEV_ERROR, "strapping_output", u->name, u->gpio,
                "GPIO%d is a strapping pin; not allowed for actuator output", u->gpio);
        } else {
            add(a, VG_SEV_WARNING, "strapping", u->name, u->gpio,
                "GPIO%d is a strapping pin; external circuit must not change its reset level",
                u->gpio);
        }
    }
    if (u->kind == USE_ADC_IN) {
        if (caps.adc_unit == 0) {
            add(a, VG_SEV_ERROR, "pin_not_adc", u->name, u->gpio,
                "GPIO%d has no ADC channel", u->gpio);
        } else if (caps.adc_unit == 2) {
            add(a, VG_SEV_WARNING, "adc2_wifi_conflict", u->name, u->gpio,
                "GPIO%d is ADC2 (unusable while Wi-Fi is used); prefer ADC1", u->gpio);
        }
    }
}

int vg_pin_check(const vg_hw_config_t *c, vg_issue_t *issues, int max_issues)
{
    acc_t a = {issues, max_issues, 0};
    pin_use_t uses[8];
    int nu = collect_uses(c, uses);

    bool any_pin = false;
    for (int i = 0; i < nu; i++) {
        any_pin |= uses[i].gpio != VG_PIN_NONE;
        if (uses[i].enabled && uses[i].gpio == VG_PIN_NONE) {
            add(&a, VG_SEV_ERROR, "pin_unset", uses[i].name, VG_PIN_NONE,
                "%s is enabled but has no GPIO", uses[i].name);
        }
    }

    if (c->chip == VG_CHIP_UNKNOWN) {
        if (any_pin) {
            add(&a, VG_SEV_ERROR, "chip_unknown", "board", VG_PIN_NONE,
                "pins assigned but chip unknown: cannot validate");
        }
    } else {
        if (c->chip_status != VG_FACT_CONFIRMED) {
            add(&a, VG_SEV_WARNING, "chip_unconfirmed", "board", VG_PIN_NONE,
                "chip %s is %s, not confirmed on-device", vg_chip_name(c->chip),
                vg_fact_status_name(c->chip_status));
        }
        for (int i = 0; i < nu; i++) {
            if (uses[i].gpio != VG_PIN_NONE) {
                check_pin(&a, c, &uses[i]);
            }
        }
    }

    /* The same GPIO must not serve two functions. */
    for (int i = 0; i < nu; i++) {
        for (int j = i + 1; j < nu; j++) {
            if (uses[i].gpio != VG_PIN_NONE && uses[i].gpio == uses[j].gpio) {
                add(&a, VG_SEV_ERROR, "pin_conflict", uses[j].name, uses[j].gpio,
                    "GPIO%d assigned to both %s and %s", uses[j].gpio, uses[i].name,
                    uses[j].name);
            }
        }
    }

    if (c->emg.enabled && c->emg.atten == VG_ATTEN_UNSET) {
        add(&a, VG_SEV_ERROR, "atten_unset", "emg", c->emg.gpio,
            "EMG ADC attenuation must be chosen from the confirmed input range");
    }
    if (c->velostat.enabled && c->velostat.atten == VG_ATTEN_UNSET) {
        add(&a, VG_SEV_ERROR, "atten_unset", "velostat", c->velostat.gpio,
            "Velostat ADC attenuation must be chosen from the confirmed divider range");
    }

    if (c->imu.enabled && (c->imu.address < 0x08 || c->imu.address > 0x77)) {
        add(&a, VG_SEV_ERROR, "i2c_address", "imu", VG_PIN_NONE,
            "I2C address 0x%02x outside 7-bit range 0x08-0x77", c->imu.address);
    }
    if (c->oled.enabled) {
        if (!c->imu.enabled) {
            add(&a, VG_SEV_ERROR, "oled_no_bus", "oled", VG_PIN_NONE,
                "OLED shares the IMU I2C bus, which is not enabled");
        } else if (c->oled.address == c->imu.address) {
            add(&a, VG_SEV_ERROR, "i2c_address_conflict", "oled", VG_PIN_NONE,
                "OLED and IMU both at 0x%02x", c->oled.address);
        }
    }

    if (c->role == VG_ROLE_WEARABLE &&
        (c->motor.enabled || c->velostat.enabled || c->oled.enabled || c->dht11.enabled)) {
        add(&a, VG_SEV_ERROR, "role_mismatch", "board", VG_PIN_NONE,
            "wearable role enables a station-only peripheral");
    }
    if (c->role == VG_ROLE_STATION && c->emg.enabled) {
        add(&a, VG_SEV_ERROR, "role_mismatch", "board", VG_PIN_NONE,
            "station role enables EMG");
    }

    if (c->motor.enabled) {
        if (c->motor.actuator == VG_ACTUATOR_LRA) {
            add(&a, VG_SEV_ERROR, "actuator_lra", "motor", c->motor.gpio,
                "LRA needs an LRA driver; brushed PWM output not allowed");
        } else if (c->motor.actuator != VG_ACTUATOR_ERM_BRUSHED) {
            add(&a, VG_SEV_ERROR, "actuator_unknown", "motor", c->motor.gpio,
                "motor type not confirmed");
        }
        if (!c->motor.driver_confirmed) {
            add(&a, VG_SEV_ERROR, "driver_unconfirmed", "motor", c->motor.gpio,
                "no confirmed driver circuit; bare GPIO drive is never allowed");
        }
        if (c->motor.max_pulse_ms == 0 ||
            c->motor.max_pulse_ms > VG_MOTOR_MAX_PULSE_CEILING_MS) {
            add(&a, VG_SEV_ERROR, "pulse_limit", "motor", c->motor.gpio,
                "max_pulse_ms must be 1..%u", (unsigned)VG_MOTOR_MAX_PULSE_CEILING_MS);
        }
    }
    return a.n;
}

int vg_issue_error_count(const vg_issue_t *issues, int n)
{
    int e = 0;
    for (int i = 0; i < n; i++) {
        e += issues[i].severity == VG_SEV_ERROR;
    }
    return e;
}

bool vg_motor_output_permitted(const vg_hw_config_t *c)
{
    if (!c->motor.enabled || c->role != VG_ROLE_STATION) {
        return false;
    }
    vg_issue_t issues[32];
    int n = vg_pin_check(c, issues, 32);
    if (n > 32) {
        return false; /* truncated: cannot prove there are no errors */
    }
    return vg_issue_error_count(issues, n) == 0;
}

static void line(vg_line_sink_t emit, void *ctx, const char *fmt, ...)
{
    char buf[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    emit(ctx, buf);
}

static const char *pin_str(int gpio, char *buf, size_t n)
{
    if (gpio == VG_PIN_NONE) {
        return "none";
    }
    snprintf(buf, n, "GPIO%d", gpio);
    return buf;
}

void vg_config_report(const vg_hw_config_t *c, vg_line_sink_t emit, void *ctx)
{
    char p1[12], p2[12];
    line(emit, ctx, "board: %s (%s)", c->board_name,
         c->role == VG_ROLE_WEARABLE ? "wearable" : "station");
    line(emit, ctx, "  %s", c->description);
    line(emit, ctx, "chip: %s [%s] %s", vg_chip_name(c->chip),
         vg_fact_status_name(c->chip_status), c->chip_evidence ? c->chip_evidence : "");
    line(emit, ctx, "emg: %s pin=%s atten=%s", c->emg.enabled ? "ENABLED" : "disabled",
         pin_str(c->emg.gpio, p1, sizeof p1), vg_atten_name(c->emg.atten));
    line(emit, ctx, "imu: %s sda=%s scl=%s addr=0x%02x clock=%luHz",
         c->imu.enabled ? "ENABLED" : "disabled", pin_str(c->imu.sda_gpio, p1, sizeof p1),
         pin_str(c->imu.scl_gpio, p2, sizeof p2), c->imu.address,
         (unsigned long)c->imu.clock_hz);
    line(emit, ctx, "motor: %s pin=%s active_%s actuator=%s driver=%s max_pulse=%lums",
         c->motor.enabled ? "ENABLED" : "disabled", pin_str(c->motor.gpio, p1, sizeof p1),
         c->motor.active_high ? "high" : "low", vg_actuator_name(c->motor.actuator),
         c->motor.driver_confirmed ? "confirmed" : "unconfirmed",
         (unsigned long)c->motor.max_pulse_ms);
    line(emit, ctx, "motor output permitted: %s", vg_motor_output_permitted(c) ? "YES" : "NO");
    line(emit, ctx, "velostat: %s pin=%s atten=%s", c->velostat.enabled ? "ENABLED" : "disabled",
         pin_str(c->velostat.gpio, p1, sizeof p1), vg_atten_name(c->velostat.atten));
    line(emit, ctx, "oled: %s addr=0x%02x | dht11: %s pin=%s",
         c->oled.enabled ? "ENABLED" : "disabled", c->oled.address,
         c->dht11.enabled ? "ENABLED" : "disabled", pin_str(c->dht11.gpio, p1, sizeof p1));

    vg_issue_t issues[32];
    int n = vg_pin_check(c, issues, 32);
    int shown = n < 32 ? n : 32;
    line(emit, ctx, "pin check: %d errors, %d warnings%s", vg_issue_error_count(issues, shown),
         shown - vg_issue_error_count(issues, shown), n > 32 ? " (truncated)" : "");
    for (int i = 0; i < shown; i++) {
        line(emit, ctx, "  %s %s [%s] %s", issues[i].severity == VG_SEV_ERROR ? "ERROR" : "warn ",
             issues[i].code, issues[i].use, issues[i].message);
    }
}
