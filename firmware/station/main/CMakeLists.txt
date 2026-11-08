idf_component_register(SRCS "main.c"
                       PRIV_REQUIRES shared boards target esp_timer freertos log esp_driver_uart
                                     esp_driver_i2c esp_hw_support bootloader_support)
