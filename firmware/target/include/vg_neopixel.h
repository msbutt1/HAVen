/* Status light: WS2812 / NeoPixel LEDs on one GPIO (board W: GPIO 23), driven by RMT.
 *
 * The phone app sends the colour of the Today ring over the telemetry TCP connection, one line:
 *   "VGLED 1 solid <rrggbb>"                    steady colour
 *   "VGLED 1 breathe <rrggbb> <rrggbb> <ms>"    smooth fade between two colours (over the daily limit)
 *   "VGLED 1 off"
 * With no phone connected the light breathes dim blue ("looking for the phone"). Brightness is scaled down
 * (VG_LED_BRIGHTNESS) to keep the current low and the light comfortable.
 */
#ifndef VG_NEOPIXEL_H
#define VG_NEOPIXEL_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

bool vg_neopixel_start(int gpio, int count);
/* One command line from the phone (without the newline). Returns true if it was a VGLED line. */
bool vg_neopixel_command(const char *line);
/* Link state, so the light can show "looking for the phone" when nobody is connected. */
void vg_neopixel_link(bool connected);

#ifdef __cplusplus
}
#endif
#endif
