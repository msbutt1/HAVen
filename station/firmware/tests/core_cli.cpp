// PC harness around station_core.h, driven over stdin/stdout by tests/test_firmware_core.py.
//
// Every stdin line is either a control line or data for the core:
//   #t <micros>     set the fake clock
//   #loop           call core.loop()
//   #press felt|not_felt
//   #motor          print the current motor setting as "#motor <n>"
//   #sync           print "#sync" (the test uses it as a read barrier)
//   anything else   fed to the core byte by byte (with its newline)
// Core output lines are printed as they are; the motor setting is tracked by the fake HAL.

#include <stdio.h>
#include <string.h>

#include "../station_controller/station_core.h"

class FakeHal : public vg::Hal {
 public:
  int motor = 0;
  uint32_t now = 0;
  void motor_set(int s) override { motor = s; }
  void write_line(const char* line) override { printf("%s\n", line); }
  uint32_t micros32() override { return now; }
};

int main() {
  FakeHal hal;
  vg::StationCore core(hal, 1, 100, "vibeguard-station-ref", "0.1");
  char buf[1024];
  while (fgets(buf, sizeof buf, stdin)) {
    if (buf[0] == '#') {
      unsigned long t = 0;
      if (sscanf(buf, "#t %lu", &t) == 1) hal.now = (uint32_t)t;
      else if (!strncmp(buf, "#loop", 5)) core.loop();
      else if (!strncmp(buf, "#press felt", 11)) core.press(true);
      else if (!strncmp(buf, "#press not_felt", 15)) core.press(false);
      else if (!strncmp(buf, "#boot", 5)) core.begin();
      else if (!strncmp(buf, "#motor", 6)) printf("#motor %d\n", hal.motor);
      else if (!strncmp(buf, "#malformed", 10)) printf("#malformed %d\n", core.malformed_lines());
      else if (!strncmp(buf, "#sync", 5)) printf("#sync\n");
      fflush(stdout);
      continue;
    }
    for (char* p = buf; *p; ++p) core.feed_char(*p);
    fflush(stdout);
  }
  return 0;
}
