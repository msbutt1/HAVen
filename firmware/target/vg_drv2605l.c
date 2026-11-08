#include "vg_drv2605l.h"

#include <stdio.h>

#include "esp_rom_sys.h"

/* TI SLOS854D register map */
#define R_STATUS 0x00     /* [7:5] DEVICE_ID (7 = DRV2605L), [1] OVER_TEMP, [0] OC_DETECT */
#define R_MODE 0x01       /* [6] STANDBY (default 1), [2:0] MODE (5 = RTP) */
#define R_RTP 0x02        /* RTP_INPUT */
#define R_OD_CLAMP 0x17   /* open-loop full scale: V = 21.59 mV x OD_CLAMP (Eq. 6) */
#define R_FEEDBACK 0x1A   /* [7] N_ERM_LRA (0 = ERM), rest default 0x36 */
#define R_CONTROL3 0x1D   /* [5] ERM_OPEN_LOOP, [3] DATA_FORMAT_RTP; default 0xA0 */

#define MODE_STANDBY 0x40
#define MODE_RTP 0x05
#define FEEDBACK_ERM 0x36   /* default value: N_ERM_LRA = 0 */
#define CONTROL3_ERM_OL 0xA0 /* ERM_OPEN_LOOP = 1, DATA_FORMAT_RTP = 0 (signed: 0x00 off .. 0x7F full) */
#define TIMEOUT_MS 20

static esp_err_t wr(vg_drv2605l_t *d, uint8_t reg, uint8_t v)
{
    uint8_t b[2] = {reg, v};
    return i2c_master_transmit(d->dev, b, 2, TIMEOUT_MS);
}

static esp_err_t rd(vg_drv2605l_t *d, uint8_t reg, uint8_t *v)
{
    return i2c_master_transmit_receive(d->dev, &reg, 1, v, 1, TIMEOUT_MS);
}

static bool wr_verify(vg_drv2605l_t *d, uint8_t reg, uint8_t v, char *detail, size_t n)
{
    uint8_t back = 0;
    if (wr(d, reg, v) != ESP_OK || rd(d, reg, &back) != ESP_OK || back != v) {
        snprintf(detail, n, "reg 0x%02x write 0x%02x readback 0x%02x", reg, v, back);
        return false;
    }
    return true;
}

static bool d_off(void *ctx)
{
    vg_drv2605l_t *d = ctx;
    if (d->dev == NULL) return false;
    /* STANDBY first: stops the output with one write. Then clear RTP_INPUT. */
    bool ok = wr(d, R_MODE, MODE_STANDBY) == ESP_OK;
    wr(d, R_RTP, 0x00);
    return ok;
}

static bool d_init(void *ctx, char *detail, size_t n)
{
    vg_drv2605l_t *d = ctx;
    i2c_device_config_t dc = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = d->addr,
                              .scl_speed_hz = 100000};
    if (d->dev == NULL && i2c_master_bus_add_device(d->bus, &dc, &d->dev) != ESP_OK) {
        snprintf(detail, n, "cannot add I2C device 0x%02x", d->addr);
        return false;
    }
    esp_rom_delay_us(300);                     /* >= 250 us after power-up (datasheet) */
    uint8_t st = 0;
    if (rd(d, R_STATUS, &st) != ESP_OK) {
        snprintf(detail, n, "no response at 0x%02x", d->addr);
        return false;
    }
    if ((st >> 5) != 7) {
        snprintf(detail, n, "DEVICE_ID %d is not DRV2605L (7)", st >> 5);
        return false;
    }
    if (!d_off(d)) {
        snprintf(detail, n, "could not enter standby");
        return false;
    }
    if (!wr_verify(d, R_MODE, MODE_STANDBY, detail, n) ||
        !wr_verify(d, R_FEEDBACK, FEEDBACK_ERM, detail, n) ||
        !wr_verify(d, R_CONTROL3, CONTROL3_ERM_OL, detail, n) ||
        !wr_verify(d, R_OD_CLAMP, d->od_clamp, detail, n) ||
        !wr_verify(d, R_RTP, 0x00, detail, n)) {
        d_off(d);
        return false;
    }
    snprintf(detail, n, "DRV2605L ok: ERM open-loop RTP, OD_CLAMP %u (%.2f V), standby",
             d->od_clamp, d->od_clamp * 0.02159);
    return true;
}

static bool d_on(void *ctx, uint8_t level)
{
    vg_drv2605l_t *d = ctx;
    if (level > VG_HAPTIC_LEVEL_MAX) return false;
    /* Amplitude first, then leave standby into RTP mode. */
    if (wr(d, R_RTP, level) != ESP_OK || wr(d, R_MODE, MODE_RTP) != ESP_OK) {
        d_off(d);
        return false;
    }
    uint8_t m = 0;
    if (rd(d, R_MODE, &m) != ESP_OK || m != MODE_RTP) {
        d_off(d);
        return false;
    }
    return true;
}

static bool d_check(void *ctx, char *detail, size_t n)
{
    vg_drv2605l_t *d = ctx;
    uint8_t st = 0;
    if (rd(d, R_STATUS, &st) != ESP_OK) {
        snprintf(detail, n, "STATUS read failed");
        return false;
    }
    d->last_status = st;
    if (st & 0x03) {
        snprintf(detail, n, "DRV2605L STATUS 0x%02x: %s%s", st, (st & 2) ? "OVER_TEMP " : "",
                 (st & 1) ? "OC_DETECT" : "");
        return false;
    }
    return true;
}

vg_haptic_t vg_drv2605l_haptic(vg_drv2605l_t *d, i2c_master_bus_handle_t bus, uint8_t addr,
                               uint8_t od_clamp)
{
    d->bus = bus;
    d->dev = NULL;
    d->addr = addr;
    d->od_clamp = od_clamp;
    d->last_status = 0;
    return (vg_haptic_t){d_init, d_on,  d_off, d_check, d, "DRV2605L", "ERM", "open_loop_rtp",
                         od_clamp * 0.02159, false};
}
