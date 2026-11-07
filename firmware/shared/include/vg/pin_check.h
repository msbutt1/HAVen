/* Validates a hardware configuration against the chip's GPIO rules and the
 * project's safety rules (motor off unless fully confirmed). Pure logic. */
#ifndef VG_PIN_CHECK_H
#define VG_PIN_CHECK_H

#include <stddef.h>

#include "vg/hw_config.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { VG_SEV_WARNING = 1, VG_SEV_ERROR = 2 } vg_severity_t;

typedef struct {
    vg_severity_t severity;
    const char *code;     /* stable identifier, e.g. "pin_input_only" */
    const char *use;      /* "emg", "imu.sda", "motor", ... or "board" */
    int gpio;             /* VG_PIN_NONE when not pin-specific */
    char message[120];
} vg_issue_t;

/* Writes up to max_issues issues; returns the total number found. */
int vg_pin_check(const vg_hw_config_t *cfg, vg_issue_t *issues, int max_issues);

/* Number of VG_SEV_ERROR entries among the first n issues. */
int vg_issue_error_count(const vg_issue_t *issues, int n);

/* Real motor output is permitted only when the motor is enabled, fully
 * confirmed, on the station role, and the configuration has zero errors. */
bool vg_motor_output_permitted(const vg_hw_config_t *cfg);

/* Human-readable configuration report, one line per call to emit(). */
typedef void (*vg_line_sink_t)(void *ctx, const char *line);
void vg_config_report(const vg_hw_config_t *cfg, vg_line_sink_t emit, void *ctx);

#ifdef __cplusplus
}
#endif
#endif
