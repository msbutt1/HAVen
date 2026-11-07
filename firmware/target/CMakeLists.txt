# ESP-IDF-only helpers shared by the wearable, station and diag apps.
idf_component_register(SRCS "vg_target.c" "vg_uart_tx.c" "vg_drv2605l.c"
                       INCLUDE_DIRS "include"
                       REQUIRES shared boards esp_driver_i2c
                       PRIV_REQUIRES esp_adc esp_hw_support log esp_driver_uart freertos esp_rom)
