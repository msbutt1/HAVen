#include "vg/hw_config.h"

const char *vg_fact_status_name(vg_fact_status_t s)
{
    switch (s) {
    case VG_FACT_PROVISIONAL: return "provisional";
    case VG_FACT_CONFIRMED: return "confirmed";
    default: return "unknown";
    }
}

const char *vg_chip_name(vg_chip_t c)
{
    return c == VG_CHIP_ESP32 ? "ESP32" : "unknown-chip";
}

const char *vg_atten_name(vg_adc_atten_t a)
{
    switch (a) {
    case VG_ATTEN_DB_0: return "0dB";
    case VG_ATTEN_DB_2_5: return "2.5dB";
    case VG_ATTEN_DB_6: return "6dB";
    case VG_ATTEN_DB_12: return "12dB";
    default: return "unset";
    }
}

const char *vg_actuator_name(vg_actuator_t a)
{
    switch (a) {
    case VG_ACTUATOR_ERM_BRUSHED: return "ERM-brushed";
    case VG_ACTUATOR_LRA: return "LRA";
    default: return "unknown";
    }
}
