/* VibeGuard bring-up DIAGNOSTIC (Prompt 3). Not the wearable or station app.
 *
 * Console on UART0 at 115200. Every result line starts with "VGD" and says
 * whether the value is MEASURED (read from hardware), CONFIGURED (written by
 * this firmware) or DERIVED (computed from datasheet tables).
 *
 * Safety: this build contains no GPIO output code. The proposed motor pin is
 * never configured. It drives only the I2C lines (open-drain) of the proposed
 * IMU bus and reads two input-only ADC pins.
 *
 * Sequence:
 *  1. identity: chip info, MAC, board config, pin check, IDF table cross-check
 *  2. IMU on the proposed I2C pins: bus scan, WHO_AM_I at 0x68/0x69, identity
 *  3. if identity is supported: reset, configure accel, read back, measure
 *     data-ready rate and an accel average at rest
 *  4. forever: ADC on GPIO36 (VP, proposed EMG) and GPIO34 (proposed Velostat)
 *     every second; IMU accel summary every 5 s
 */
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "driver/i2c_master.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "vg/imu_mpu.h"
#include "vg_target.h"

#define I2C_HZ 100000      /* conservative; the MPU-9250 PS allows 400 kHz */
#define I2C_TIMEOUT_MS 50

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_imu;
static uint8_t s_imu_addr;
static bool s_imu_ready;
static int s_lsb_per_g;

/* ------------------------------------------------------------------ I2C -- */

static esp_err_t rd(uint8_t reg, uint8_t *buf, size_t n)
{
    return i2c_master_transmit_receive(s_imu, &reg, 1, buf, n, I2C_TIMEOUT_MS);
}

static esp_err_t wr(uint8_t reg, uint8_t v)
{
    uint8_t b[2] = {reg, v};
    return i2c_master_transmit(s_imu, b, 2, I2C_TIMEOUT_MS);
}

static bool bus_init(int sda, int scl)
{
    i2c_master_bus_config_t bc = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = sda,
        .scl_io_num = scl,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        /* weak internal pull-ups as a fallback only; module pull-ups unknown */
        .flags.enable_internal_pullup = true,
    };
    esp_err_t e = i2c_new_master_bus(&bc, &s_bus);
    printf("VGD i2c bus sda=GPIO%d scl=GPIO%d %dHz internal_pullup=on init=%s\n", sda, scl,
           I2C_HZ, esp_err_to_name(e));
    return e == ESP_OK;
}

static int bus_scan(uint8_t *found, int max)
{
    int n = 0;
    for (uint8_t a = 0x08; a <= 0x77; a++) {
        if (i2c_master_probe(s_bus, a, I2C_TIMEOUT_MS) == ESP_OK && n < max) {
            found[n++] = a;
        }
    }
    printf("VGD MEASURED i2c_scan acks=%d:", n);
    for (int i = 0; i < n; i++) printf(" 0x%02x", found[i]);
    printf("%s\n", n ? "" : " none (check VCC/GND/SDA/SCL wiring and module power)");
    return n;
}

/* --------------------------------------------------------------- IMU ----- */

static bool imu_identify(void)
{
    uint8_t found[16];
    int n = bus_scan(found, 16);
    for (int i = 0; i < n; i++) {
        uint8_t a = found[i];
        if (a != VG_MPU_ADDR_AD0_LOW && a != VG_MPU_ADDR_AD0_HIGH) {
            continue;
        }
        i2c_device_config_t dc = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = a,
                                  .scl_speed_hz = I2C_HZ};
        if (i2c_master_bus_add_device(s_bus, &dc, &s_imu) != ESP_OK) {
            continue;
        }
        uint8_t who = 0;
        esp_err_t e = rd(VG_MPU_WHO_AM_I, &who, 1);
        if (e != ESP_OK) {
            printf("VGD FAIL imu addr=0x%02x WHO_AM_I read error=%s\n", a, esp_err_to_name(e));
            i2c_master_bus_rm_device(s_imu);
            continue;
        }
        vg_imu_identity_t id = vg_imu_identify(who);
        printf("VGD MEASURED imu addr=0x%02x (AD0=%s) WHO_AM_I=0x%02x identity=%s status=%s\n", a,
               a == VG_MPU_ADDR_AD0_LOW ? "low" : "high", who, id.name,
               id.status == VG_IMU_ID_OFFICIAL     ? "SUPPORTED(official doc)"
               : id.status == VG_IMU_ID_SECONDARY ? "RECOGNISED(secondary sources only)"
                                                  : "UNSUPPORTED");
        printf("VGD   identity_source: %s\n", id.source);
        printf("VGD   magnetometer: not probed (not needed; never inferred from the module label)\n");
        if (id.status == VG_IMU_ID_UNSUPPORTED) {
            printf("VGD FAIL imu unsupported identity 0x%02x: no registers written\n", who);
            i2c_master_bus_rm_device(s_imu);
            continue;
        }
        s_imu_addr = a;
        return true;
    }
    printf("VGD FAIL imu not found at 0x68/0x69\n");
    return false;
}

static bool imu_configure(void)
{
    if (wr(VG_MPU_PWR_MGMT_1, 0x80) != ESP_OK) {      /* H_RESET */
        printf("VGD FAIL imu reset write failed\n");
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(100));                   /* PS: register start-up max 100 ms */
    bool ok = true;
    for (int i = 0; i < VG_DIAG_ACCEL_SETUP_N; i++) {
        const vg_reg_write_t *w = &VG_DIAG_ACCEL_SETUP[i];
        esp_err_t e = wr(w->reg, w->value);
        uint8_t back = 0xFF;
        esp_err_t r = rd(w->reg, &back, 1);
        bool match = e == ESP_OK && r == ESP_OK && back == w->value;
        ok &= match;
        printf("VGD CONFIGURED reg 0x%02x <- 0x%02x | MEASURED readback 0x%02x %s | %s\n", w->reg,
               w->value, back, match ? "match" : "MISMATCH", w->meaning);
    }
    vTaskDelay(pdMS_TO_TICKS(50));
    uint8_t r[5];
    if (rd(VG_MPU_SMPLRT_DIV, r, 5) != ESP_OK) {     /* 0x19..0x1D */
        printf("VGD FAIL imu config readback\n");
        return false;
    }
    vg_accel_derived_t d = vg_accel_derive(r[3], r[4], r[0], r[1], r[2]);
    s_lsb_per_g = d.lsb_per_g;
    printf("VGD DERIVED accel (from read-back registers + RM-MPU-9250A-00 r1.6 tables): range=+-%dg "
           "sensitivity=%d LSB/g dlpf=%s bw_3db=%.1fHz internal=%.0fHz odr=%.1fHz\n",
           d.full_scale_g, d.lsb_per_g, d.dlpf_bypassed ? "bypassed" : "on", d.bandwidth_3db_hz,
           d.internal_rate_hz, d.output_rate_hz);
    return ok;
}

static bool wait_ready(int64_t deadline_us)
{
    while (esp_timer_get_time() < deadline_us) {
        uint8_t st = 0;
        if (rd(VG_MPU_INT_STATUS, &st, 1) == ESP_OK && (st & 0x01)) {
            return true;
        }
    }
    return false;
}

static void imu_measure_rate(void)
{
    /* RAW_DATA_RDY_INT clears when INT_STATUS is read (INT_ANYRD_2CLEAR=0 after reset). */
    int64_t t0 = esp_timer_get_time(), end = t0 + 1000000;
    int ready = 0, errors = 0;
    while (esp_timer_get_time() < end) {
        uint8_t st = 0;
        if (rd(VG_MPU_INT_STATUS, &st, 1) != ESP_OK) {
            errors++;
        } else if (st & 0x01) {
            ready++;
        }
    }
    printf("VGD MEASURED imu data_ready events in 1.000 s = %d (i2c errors %d); polling at %d Hz "
           "I2C can undercount if polling is slower than the output rate\n",
           ready, errors, I2C_HZ);
}

static void imu_sample_summary(int n)
{
    double sum[3] = {0}, sq[3] = {0};
    int got = 0, frozen = 0;
    int16_t prev[3] = {0};
    for (int k = 0; k < n; k++) {
        if (!wait_ready(esp_timer_get_time() + 20000)) {
            continue;
        }
        uint8_t b[6];
        if (rd(VG_MPU_ACCEL_XOUT_H, b, 6) != ESP_OK) {
            continue;
        }
        int16_t v[3];
        for (int a = 0; a < 3; a++) {
            v[a] = (int16_t)((b[2 * a] << 8) | b[2 * a + 1]);
            sum[a] += v[a];
            sq[a] += (double)v[a] * v[a];
        }
        if (got && memcmp(v, prev, sizeof v) == 0) {
            frozen++;
        }
        memcpy(prev, v, sizeof v);
        got++;
    }
    if (got == 0) {
        printf("VGD FAIL imu no accel samples read (data ready never set or read errors)\n");
        return;
    }
    double m[3], sd[3];
    for (int a = 0; a < 3; a++) {
        m[a] = sum[a] / got;
        double var = sq[a] / got - m[a] * m[a];
        sd[a] = var > 0 ? sqrt(var) : 0;
    }
    double mag = sqrt(m[0] * m[0] + m[1] * m[1] + m[2] * m[2]);
    printf("VGD MEASURED accel n=%d mean_counts x=%.1f y=%.1f z=%.1f sd_counts x=%.1f y=%.1f z=%.1f "
           "identical_consecutive=%d\n",
           got, m[0], m[1], m[2], sd[0], sd[1], sd[2], frozen);
    printf("VGD DERIVED accel |mean| = %.0f counts = %.3f g at %d LSB/g (expect ~1 g when still)\n",
           mag, mag / s_lsb_per_g, s_lsb_per_g);
}

/* ---------------------------------------------------------- DRV2605L ----- */
/* READ-ONLY probe of the station's haptic driver. Nothing is written to it,
 * so it stays in its power-up state. Per TI SLOS854D: address 0x5A;
 * STATUS 0x00 bits 7-5 DEVICE_ID (7 = DRV2605L); MODE 0x01 bit 6 STANDBY
 * (default 1 = software standby: no output). */
#define DRV_ADDR 0x5A

static void drv_probe(void)
{
    if (i2c_master_probe(s_bus, DRV_ADDR, I2C_TIMEOUT_MS) != ESP_OK) {
        printf("VGD MEASURED drv2605l: no ACK at 0x5a (not wired to this bus, or unpowered)\n");
        return;
    }
    i2c_device_config_t dc = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = DRV_ADDR,
                              .scl_speed_hz = I2C_HZ};
    i2c_master_dev_handle_t drv;
    if (i2c_master_bus_add_device(s_bus, &dc, &drv) != ESP_OK) {
        printf("VGD FAIL drv2605l: could not add device\n");
        return;
    }
    uint8_t reg = 0x00, st = 0, mode = 0;
    esp_err_t e1 = i2c_master_transmit_receive(drv, &reg, 1, &st, 1, I2C_TIMEOUT_MS);
    reg = 0x01;
    esp_err_t e2 = i2c_master_transmit_receive(drv, &reg, 1, &mode, 1, I2C_TIMEOUT_MS);
    i2c_master_bus_rm_device(drv);
    if (e1 != ESP_OK || e2 != ESP_OK) {
        printf("VGD FAIL drv2605l: register read error (%s / %s)\n", esp_err_to_name(e1),
               esp_err_to_name(e2));
        return;
    }
    int id = st >> 5;
    const char *name = id == 7 ? "DRV2605L" : id == 3 ? "DRV2605" : id == 4 ? "DRV2604"
                     : id == 6 ? "DRV2604L" : "unknown";
    printf("VGD MEASURED drv2605l addr=0x5a STATUS=0x%02x DEVICE_ID=%d (%s, %s) DIAG_RESULT=%d "
           "OVER_TEMP=%d OC_DETECT=%d\n",
           st, id, name, id == 7 ? "SUPPORTED: TI SLOS854D Table 4" : "UNSUPPORTED", (st >> 3) & 1,
           (st >> 1) & 1, st & 1);
    printf("VGD MEASURED drv2605l MODE=0x%02x STANDBY=%d (%s) mode_bits=%d | nothing written: "
           "motor not driven by this build\n",
           mode, (mode >> 6) & 1, (mode >> 6) & 1 ? "standby, output off" : "NOT IN STANDBY",
           mode & 7);
}

/* --------------------------------------------------------------- ADC ----- */

typedef struct {
    int gpio;
    const char *label;
    adc_channel_t ch;
    adc_cali_handle_t cali;
} adc_pin_t;

static adc_oneshot_unit_handle_t s_adc1;

static bool adc_pin_init(adc_pin_t *p)
{
    adc_unit_t unit;
    if (adc_oneshot_io_to_channel(p->gpio, &unit, &p->ch) != ESP_OK || unit != ADC_UNIT_1) {
        printf("VGD FAIL adc GPIO%d is not an ADC1 pin\n", p->gpio);
        return false;
    }
    adc_oneshot_chan_cfg_t cc = {.atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_12};
    if (adc_oneshot_config_channel(s_adc1, p->ch, &cc) != ESP_OK) {
        return false;
    }
    adc_cali_line_fitting_config_t lc = {.unit_id = ADC_UNIT_1, .atten = ADC_ATTEN_DB_12,
                                         .bitwidth = ADC_BITWIDTH_12};
    adc_cali_line_fitting_efuse_val_t ev;
    adc_cali_scheme_line_fitting_check_efuse(&ev);
    if (adc_cali_create_scheme_line_fitting(&lc, &p->cali) != ESP_OK) {
        p->cali = NULL;
    }
    printf("VGD CONFIGURED adc GPIO%d (%s) ADC1_CH%d atten=12dB bitwidth=12 | calibration=%s "
           "efuse=%s\n",
           p->gpio, p->label, (int)p->ch, p->cali ? "line_fitting" : "NONE",
           ev == ADC_CALI_LINE_FITTING_EFUSE_VAL_EFUSE_TP     ? "two_point"
           : ev == ADC_CALI_LINE_FITTING_EFUSE_VAL_EFUSE_VREF ? "vref"
                                                               : "default_vref(estimate)");
    return true;
}

static void adc_report(adc_pin_t *p)
{
    int mn = 4096, mx = -1;
    long sum = 0;
    const int n = 64;
    for (int i = 0; i < n; i++) {
        int raw = 0;
        adc_oneshot_read(s_adc1, p->ch, &raw);
        mn = raw < mn ? raw : mn;
        mx = raw > mx ? raw : mx;
        sum += raw;
    }
    int mean = (int)(sum / n), mv = -1;
    if (p->cali) adc_cali_raw_to_voltage(p->cali, mean, &mv);
    /* Measured on board S: an unconnected input-only pin reads 0 too, so a low
     * reading cannot distinguish "jumper to GND" from "nothing connected". */
    const char *cls = mean <= 40     ? "reads ~0 (GND jumper OR unconnected: indistinguishable)"
                      : mean >= 4050 ? "SATURATED (>= ~2.45 V, e.g. jumper to 3V3)"
                      : (mx - mn) > 400 ? "noisy/floating (nothing connected?)"
                                        : "mid-range";
    printf("VGD MEASURED adc GPIO%d raw mean=%d min=%d max=%d | calibrated=%d mV%s | %s\n",
           p->gpio, mean, mn, mx, mv,
           (p->cali ? (mv < 150 || mv > 2450) : (mean < 186 || mean > 3000))
               ? " (outside datasheet effective range 150-2450 mV)"
               : "",
           cls);
}

static void adc_rate(adc_pin_t *p)
{
    int raw;
    int64_t t0 = esp_timer_get_time();
    for (int i = 0; i < 2000; i++) adc_oneshot_read(s_adc1, p->ch, &raw);
    int64_t dt = esp_timer_get_time() - t0;
    printf("VGD MEASURED adc oneshot throughput GPIO%d: 2000 reads in %" PRId64 " us = %.0f reads/s\n",
           p->gpio, dt, 2000.0 * 1e6 / (double)dt);
}

/* --------------------------------------------------------------- main ---- */

void app_main(void)
{
    vTaskDelay(pdMS_TO_TICKS(300));
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_BASE);
    printf("\nVGD ===== VibeGuard diagnostic %s, board config %s =====\n", VG_FIRMWARE_VERSION,
           VG_BOARD_NAME);
    printf("VGD MEASURED mac=%02x:%02x:%02x:%02x:%02x:%02x (W=28:05:a5:07:55:54 S=28:05:a5:07:fb:6c)\n",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    printf("VGD motor_output=OFF (no GPIO output code; haptic driver only read, never written)\n");
    /* Role from the MEASURED MAC: board S is the station; anything else uses
     * the wearable configuration. */
    static const uint8_t MAC_S[6] = {0x28, 0x05, 0xa5, 0x07, 0xfb, 0x6c};
    bool station = memcmp(mac, MAC_S, 6) == 0;
    const vg_hw_config_t *cfg = station ? &vg_board_station : &vg_board_wearable;
    printf("VGD role=%s (selected from measured MAC)\n", station ? "station" : "wearable");
    int problems = vg_target_startup(cfg);   /* logs report + IDF table check */
    printf("VGD config_problems=%d\n", problems);

    const vg_i2c_imu_cfg_t *imu = &cfg->imu;
    printf("VGD I2C bus pins from board config: SDA GPIO%d, SCL GPIO%d\n", imu->sda_gpio,
           imu->scl_gpio);
    bool bus = imu->sda_gpio >= 0 && imu->scl_gpio >= 0 && bus_init(imu->sda_gpio, imu->scl_gpio);
    if (bus && station) {
        drv_probe();
    }
    if (bus && imu_identify()) {
        s_imu_ready = imu_configure();
        if (s_imu_ready) {
            imu_measure_rate();
            imu_sample_summary(200);
        } else {
            printf("VGD FAIL imu configuration did not read back as written\n");
        }
    }

    adc_oneshot_unit_init_cfg_t uc = {.unit_id = ADC_UNIT_1};
    /* EMG pin from the board config (wired), plus VP (GPIO36) as a spare
     * ADC1 input for the BioAmp-free check if it is a different pin. */
    int apin = station ? cfg->velostat.gpio : cfg->emg.gpio;
    adc_pin_t pins[2] = {{apin, station ? "Velostat input (proposed)" : "EMG input (BioAmp OUT)", 0,
                          NULL},
                         {36, "spare ADC1 input VP", 0, NULL}};
    int ncand = apin == 36 ? 1 : 2;
    int npins = 0;
    if (adc_oneshot_new_unit(&uc, &s_adc1) == ESP_OK) {
        for (int i = 0; i < ncand; i++) {
            if (pins[i].gpio >= 0 && adc_pin_init(&pins[i])) {
                pins[npins++] = pins[i];
            }
        }
        if (npins) adc_rate(&pins[0]);
    }
    printf("VGD ADC inputs: only this board's GND/3V3 jumpers, or the BioAmp powered from 3V3 "
           "(see WIRING.md)\n");

    for (int k = 0;; k++) {
        for (int i = 0; i < npins; i++) adc_report(&pins[i]);
        if (s_imu_ready && k % 5 == 4) imu_sample_summary(100);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
