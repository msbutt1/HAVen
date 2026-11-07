/* Deterministic FAKE acquisition sources. Every sample they produce is
 * SIMULATED: there is no API to relabel it, and real-hardware adapters are
 * separate types, so fake data cannot be substituted into a live stream.
 * Values are arbitrary synthetic counts and imply nothing about the hardware.
 */
#ifndef VG_FAKE_SOURCE_H
#define VG_FAKE_SOURCE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VG_FAKE_PROVENANCE "simulated"

typedef struct {
    uint32_t state;
    uint64_t index;      /* samples produced since init */
} vg_fake_emg_t;

typedef struct {
    uint32_t state;
    uint64_t index;
} vg_fake_imu_t;

void vg_fake_emg_init(vg_fake_emg_t *f, uint32_t seed);
/* One synthetic EMG sample, 0..4095 synthetic counts. */
int32_t vg_fake_emg_next(vg_fake_emg_t *f);

void vg_fake_imu_init(vg_fake_imu_t *f, uint32_t seed);
/* One synthetic accelerometer sample (x, y, z) in synthetic signed counts. */
void vg_fake_imu_next(vg_fake_imu_t *f, int32_t out[3]);

#ifdef __cplusplus
}
#endif
#endif
