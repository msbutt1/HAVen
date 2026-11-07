/* Haptic driver adapter interface. The station controller drives the motor
 * ONLY through one of these; it never touches a GPIO itself.
 *
 * Contract for implementations:
 *  - init(): verify the device identity, force output off, apply the
 *    configuration, read it back. Return false on any mismatch (unusable).
 *  - on(level): level 1..127 = fraction of the configured full-scale drive.
 *  - off(): must be callable at any time, idempotent, and leave output off.
 *  - check(): poll for driver faults; false = fault (controller turns off).
 */
#ifndef VG_HAPTIC_H
#define VG_HAPTIC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VG_HAPTIC_LEVEL_MAX 127

typedef struct {
    bool (*init)(void *ctx, char *detail, size_t n);
    bool (*on)(void *ctx, uint8_t level);
    bool (*off)(void *ctx);
    bool (*check)(void *ctx, char *detail, size_t n);
    void *ctx;
    const char *model;        /* e.g. "DRV2605L" or "fake" */
    const char *actuator;     /* e.g. "ERM" */
    const char *drive;        /* e.g. "open_loop_rtp" */
    double full_scale_v;      /* derived from datasheet + configuration; 0 if n/a */
    bool simulated;           /* fake driver: events are labelled simulated */
} vg_haptic_t;

/* Fake (logic-only) driver for tests and the host simulator. Records state;
 * can be told to fail. Never connected to hardware. */
typedef struct {
    bool output_on;
    uint8_t level;
    uint32_t on_calls, off_calls, init_calls, check_calls;
    bool fail_init, fail_on, fail_off, fail_check;
} vg_fake_haptic_t;

vg_haptic_t vg_fake_haptic(vg_fake_haptic_t *state);

#ifdef __cplusplus
}
#endif
#endif
