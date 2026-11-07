/* Station role (ESP32 #2): bounded actuator commands, IMU #2, Velostat.
 *
 * Prompt 1 skeleton: startup report only. This file contains NO GPIO output
 * code: the motor pin is never configured, whatever the board config says.
 * Motor control arrives in Prompt 5 behind vg_motor_output_permitted().
 */
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "vg/pin_check.h"
#include "vg_target.h"

static const char *TAG = "station";

void app_main(void)
{
    int problems = vg_target_startup(&vg_board_station);
    ESP_LOGW(TAG, "motor output: OFF (config permits: %s; control not implemented in this build)",
             vg_motor_output_permitted(&vg_board_station) ? "yes" : "no");
    if (problems) {
        ESP_LOGE(TAG, "%d configuration problems", problems);
    }
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        ESP_LOGI(TAG, "idle; motor OFF");
    }
}
