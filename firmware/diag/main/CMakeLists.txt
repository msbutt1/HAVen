idf_component_register(SRCS "main.c"
                       PRIV_REQUIRES shared boards target esp_driver_i2c esp_adc esp_timer
                                     esp_hw_support freertos log)
