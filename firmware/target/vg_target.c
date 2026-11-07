#include "vg_target.h"

#include "esp_adc/adc_oneshot.h"
#include "esp_chip_info.h"
#include "esp_log.h"
#include "hal/gpio_types.h"
#include "sdkconfig.h"
#include "vg/chip_caps.h"
#include "vg/pin_check.h"

static const char *TAG = "vg";

static void log_line(void *ctx, const char *line)
{
    (void)ctx;
    ESP_LOGI(TAG, "%s", line);
}

/* Compare the portable table (chip_caps.c, from the docs) with the IDF
 * driver's own knowledge. The portable table may be STRICTER (GPIO20 is
 * treated as absent) but never more permissive. */
static int cross_check_gpio_table(vg_chip_t chip)
{
    int mismatches = 0;
    for (int g = 0; g < SOC_GPIO_PIN_COUNT; g++) {
        vg_gpio_caps_t k;
        if (!vg_chip_gpio_caps(chip, g, &k)) {
            continue;
        }
        bool ours_exists = k.flags & VG_GPIO_EXISTS;
        if (ours_exists && !GPIO_IS_VALID_GPIO(g)) {
            ESP_LOGE(TAG, "table: GPIO%d marked present but IDF says invalid", g);
            mismatches++;
        }
        if (ours_exists && ((k.flags & VG_GPIO_INPUT_ONLY) != 0) == GPIO_IS_VALID_OUTPUT_GPIO(g)) {
            ESP_LOGE(TAG, "table: GPIO%d input-only flag disagrees with IDF", g);
            mismatches++;
        }
        if (!ours_exists) {
            continue;
        }
        adc_unit_t unit;
        adc_channel_t ch;
        bool idf_adc = adc_oneshot_io_to_channel(g, &unit, &ch) == ESP_OK;
        bool ours_adc = k.adc_unit != 0;
        if (idf_adc != ours_adc ||
            (idf_adc && ((int)unit + 1 != k.adc_unit || (int)ch != k.adc_channel))) {
            ESP_LOGE(TAG, "table: GPIO%d ADC mapping disagrees with IDF", g);
            mismatches++;
        }
    }
    return mismatches;
}

int vg_target_startup(const vg_hw_config_t *cfg)
{
    ESP_LOGI(TAG, "%s %s role=%s board=%s (build-time selection)", VG_FIRMWARE_NAME,
             VG_FIRMWARE_VERSION, cfg->role == VG_ROLE_WEARABLE ? "wearable" : "station",
             VG_BOARD_NAME);

    esp_chip_info_t info;
    esp_chip_info(&info);
    /* Measured on the running chip (only meaningful once flashed). */
    ESP_LOGI(TAG, "measured chip: model=%d revision=%u cores=%u (build target %s)",
             (int)info.model, (unsigned)info.revision, (unsigned)info.cores,
             CONFIG_IDF_TARGET);
    if (cfg->chip == VG_CHIP_ESP32 && info.model != CHIP_ESP32) {
        ESP_LOGE(TAG, "board config says ESP32 but running chip model is %d", (int)info.model);
    }

    vg_config_report(cfg, log_line, NULL);

    vg_issue_t issues[32];
    int n = vg_pin_check(cfg, issues, 32);
    int errors = vg_issue_error_count(issues, n < 32 ? n : 32) + (n > 32 ? 1 : 0);

    int mismatches = cfg->chip == VG_CHIP_UNKNOWN ? 0 : cross_check_gpio_table(cfg->chip);
    ESP_LOGI(TAG, "GPIO/ADC table vs IDF driver: %d mismatches%s", mismatches,
             cfg->chip == VG_CHIP_UNKNOWN ? " (skipped: chip unknown)" : "");
    return errors + mismatches;
}
