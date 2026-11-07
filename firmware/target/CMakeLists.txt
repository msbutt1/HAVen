# ESP-IDF-only helpers shared by the wearable and station apps.
idf_component_register(SRCS "vg_target.c" "vg_uart_tx.c"
                       INCLUDE_DIRS "include"
                       REQUIRES shared boards
                       PRIV_REQUIRES esp_adc esp_hw_support log esp_driver_uart freertos)
