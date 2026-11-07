// VibeGuard station controller: ESP32 reference sketch (UNCOMPILED AND UNTESTED ON HARDWARE).
//
// Written without an ESP32 toolchain, so only station_core.h (the protocol) is tested, on a PC.
// This file is the thin board layer: PWM motor, two buttons, USB serial. Offered to Salaar and
// Collins as a starting point. Fill in station_config.h from the real wiring first.
//
// Target: ESP32 with the Arduino-ESP32 core (2.x or 3.x; the LEDC calls differ, handled below).

#include "station_config.h"
#include "station_core.h"

#ifndef PIN_MOTOR
#error "Set PIN_MOTOR in station_config.h (see HARDWARE.md)"
#endif
#ifndef PIN_BTN_FELT
#error "Set PIN_BTN_FELT in station_config.h (see HARDWARE.md)"
#endif
#ifndef PIN_BTN_NOT_FELT
#error "Set PIN_BTN_NOT_FELT in station_config.h (see HARDWARE.md)"
#endif

static_assert(VG_SETTING_MAX <= 100, "settings are PWM duty percent: VG_SETTING_MAX must be <= 100");
static const int kPwmFreqHz = 200;  // placeholder; check the motor/driver datasheet
static const int kPwmBits = 8;

class BoardHal : public vg::Hal {
 public:
  void motor_set(int setting) override {
    // Settings are percent duty. 0 = off.
    uint32_t duty = (uint32_t)setting * ((1u << kPwmBits) - 1) / 100u;
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(PIN_MOTOR, duty);
#else
    ledcWrite(0, duty);
#endif
  }
  void write_line(const char* line) override { Serial.println(line); }
  uint32_t micros32() override { return micros(); }
};

static BoardHal hal;
static vg::StationCore core(hal, VG_SETTING_MIN, VG_SETTING_MAX, VG_FW_NAME, VG_FW_VERSION);

// A constructor (not default member initializers): Arduino-ESP32 core 2.x builds as C++11, where
// a struct with default member initializers cannot be brace-initialised.
struct Button {
  int pin;
  bool felt;
  bool stable;       // true = released (INPUT_PULLUP)
  bool last_raw;
  uint32_t changed_ms;
  Button(int p, bool f) : pin(p), felt(f), stable(true), last_raw(true), changed_ms(0) {}
};
static Button buttons[2] = {Button(PIN_BTN_FELT, true), Button(PIN_BTN_NOT_FELT, false)};

void setup() {
  Serial.begin(VG_BAUD);
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PIN_MOTOR, kPwmFreqHz, kPwmBits);
#else
  ledcSetup(0, kPwmFreqHz, kPwmBits);
  ledcAttachPin(PIN_MOTOR, 0);
#endif
  hal.motor_set(0);  // motor off before anything else
  for (auto& b : buttons) pinMode(b.pin, INPUT_PULLUP);
  core.begin();      // announce ourselves; a hello nobody asked for tells the host we rebooted
}

void loop() {
  while (Serial.available() > 0) core.feed_char((char)Serial.read());
  core.loop();
  uint32_t now = millis();
  for (auto& b : buttons) {
    bool raw = digitalRead(b.pin) != 0;
    if (raw != b.last_raw) { b.last_raw = raw; b.changed_ms = now; }
    if (raw != b.stable && now - b.changed_ms >= VG_DEBOUNCE_MS) {
      b.stable = raw;
      if (!raw) core.press(b.felt);  // pressed (pulled to GND)
    }
  }
}
