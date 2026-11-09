// HAVen station controller: portable protocol core (REFERENCE, not yet run on hardware).
//
// Offered to Salaar (firmware) and Collins (wiring) as a starting point; adopt or replace it.
// This file has no Arduino dependency: it is compiled and tested on a PC (tests/test_firmware_core.py)
// and used unchanged by station_controller.ino. What it does NOT cover, because it needs the real
// board: pins, PWM, driver electronics, button debouncing, USB serial. Those live in the .ino.
//
// Protocol: see ../../contract/STATION_CONTRACT.md ("Controller link"). One JSON object per line.
//   host -> controller: hello | deliver_stimulus {trial_id, setting, duration_ms} | stop_stimulus
//   controller -> host: hello | ack | stimulus_started | stimulus_ended | response
// A motor "setting" is a command to the driver (0 = off); it is not a measured amplitude.
//
// C++11, no heap, no exceptions, no 64-bit printf (newlib-nano on ESP32 may lack %llu).

#ifndef VG_STATION_CORE_H
#define VG_STATION_CORE_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

namespace vg {

class Hal {
 public:
  virtual ~Hal() {}
  virtual void motor_set(int setting) = 0;        // 0 = motor off
  virtual void write_line(const char* line) = 0;  // send one line (the HAL adds the newline)
  virtual uint32_t micros32() = 0;                // wraps every ~71.6 min; the core extends it
};

class StationCore {
 public:
  static const int kMaxLine = 256;
  static const int kMaxId = 64;
  static const long kMaxDurationMs = 60000;

  StationCore(Hal& hal, int min_setting, int max_setting, const char* fw_name, const char* fw_version)
      : hal_(hal), min_(min_setting), max_(max_setting), fw_name_(fw_name), fw_version_(fw_version),
        len_(0), overflow_(false), active_(false), end_us_(0), have_trial_(false),
        wraps_(0), last_us32_(0), malformed_(0) {
    trial_[0] = 0;
  }

  // Call once at boot: announces the controller. A hello nobody asked for tells the host we rebooted.
  void begin() { send_hello(0); }

  // Feed every received byte.
  void feed_char(char c) {
    if (c == '\r') return;
    if (c == '\n') {
      if (overflow_) {
        ++malformed_;
      } else if (len_ > 0) {
        line_[len_] = 0;
        handle_line(line_);
      }
      len_ = 0;
      overflow_ = false;
      return;
    }
    if (len_ < kMaxLine - 1) line_[len_++] = c; else overflow_ = true;
  }

  // Call often (every loop iteration): ends a finished stimulus.
  void loop() { expire(); }

  // Call when a debounced button press happens.
  void press(bool felt) {
    char b[kBuf];
    int n = snprintf(b, sizeof b, "{\"schema\":\"vibeguard.station_link\",\"version\":1,\"type\":\"response\","
                                   "\"response\":\"%s\",", felt ? "felt" : "not_felt");
    if (have_trial_) n += snprintf(b + n, sizeof b - n, "\"trial_id\":\"%s\",", trial_);
    append_time(b, n);
    hal_.write_line(b);
  }

  bool stimulus_active() const { return active_; }
  int malformed_lines() const { return malformed_; }
  uint64_t stimulus_end_us() const { return end_us_; }

 private:
  static const int kBuf = 320;
  Hal& hal_;
  int min_, max_;
  const char* fw_name_;
  const char* fw_version_;
  char line_[kMaxLine];
  int len_;
  bool overflow_;
  bool active_;
  uint64_t end_us_;
  char trial_[kMaxId + 1];
  bool have_trial_;
  uint32_t wraps_, last_us32_;
  int malformed_;

  // Ends a stimulus whose time is up. Also run before every command, so a command arriving just
  // after the end is never answered "busy" merely because loop() has not run yet.
  void expire() {
    if (active_ && now_us() >= end_us_) {
      hal_.motor_set(0);
      active_ = false;
      send_trial_event("stimulus_ended");
    }
  }

  uint64_t now_us() {
    uint32_t t = hal_.micros32();
    if (t < last_us32_) ++wraps_;  // extend the 32-bit counter to 64 bits
    last_us32_ = t;
    return ((uint64_t)wraps_ << 32) | t;
  }

  static int fmt_u64(char* out, uint64_t v) {  // no %llu
    char tmp[21];
    int n = 0;
    do { tmp[n++] = (char)('0' + (int)(v % 10)); v /= 10; } while (v);
    for (int i = 0; i < n; ++i) out[i] = tmp[n - 1 - i];
    out[n] = 0;
    return n;
  }

  // Appends `"device_t_us":N}` (closing the object).
  void append_time(char* b, int n) {
    n += snprintf(b + n, kBuf - n, "\"device_t_us\":");
    n += fmt_u64(b + n, now_us());
    b[n++] = '}';
    b[n] = 0;
  }

  void send_hello(const char* cid) {
    char b[kBuf];
    int n = snprintf(b, sizeof b, "{\"schema\":\"vibeguard.station_link\",\"version\":1,\"type\":\"hello\",");
    if (cid) n += snprintf(b + n, sizeof b - n, "\"command_id\":\"%s\",", cid);
    snprintf(b + n, sizeof b - n,
             "\"firmware\":{\"name\":\"%s\",\"version\":\"%s\"},\"setting_range\":{\"min\":%d,\"max\":%d}}",
             fw_name_, fw_version_, min_, max_);
    hal_.write_line(b);
  }

  void send_ack(const char* cid, const char* status, const char* reason) {
    char b[kBuf];
    int n = snprintf(b, sizeof b, "{\"schema\":\"vibeguard.station_link\",\"version\":1,\"type\":\"ack\","
                                   "\"command_id\":\"%s\",\"status\":\"%s\"", cid, status);
    if (reason) n += snprintf(b + n, sizeof b - n, ",\"reason\":\"%s\"", reason);
    snprintf(b + n, sizeof b - n, "}");
    hal_.write_line(b);
  }

  void send_trial_event(const char* type) {
    char b[kBuf];
    int n = snprintf(b, sizeof b, "{\"schema\":\"vibeguard.station_link\",\"version\":1,\"type\":\"%s\","
                                   "\"trial_id\":\"%s\",", type, trial_);
    append_time(b, n);
    hal_.write_line(b);
  }

  // -- tiny JSON field readers for our fixed, flat command objects ---------------------------
  static const char* find_value(const char* line, const char* key) {
    char pat[40];
    snprintf(pat, sizeof pat, "\"%s\"", key);
    const char* p = line;
    while ((p = strstr(p, pat)) != 0) {
      const char* q = p + strlen(pat);
      while (*q == ' ') ++q;
      if (*q == ':') {  // a real key, not a value that happens to equal the key name
        ++q;
        while (*q == ' ') ++q;
        return q;
      }
      p += 1;
    }
    return 0;
  }

  static bool id_char(char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '.' ||
           c == '_' || c == ':' || c == '-';
  }

  // String value restricted to the contract's id alphabet (so replies can never be malformed JSON).
  static bool get_id(const char* line, const char* key, char* out, int cap) {
    const char* p = find_value(line, key);
    if (!p || *p != '"') return false;
    ++p;
    int n = 0;
    while (*p && *p != '"') {
      if (!id_char(*p) || n >= cap - 1) return false;
      out[n++] = *p++;
    }
    if (*p != '"' || n == 0) return false;
    out[n] = 0;
    return true;
  }

  static bool get_int(const char* line, const char* key, long* out) {
    const char* p = find_value(line, key);
    if (!p) return false;
    char* end = 0;
    long v = strtol(p, &end, 10);
    if (end == p) return false;
    while (*end == ' ') ++end;
    if (*end != ',' && *end != '}') return false;  // no trailing junk after the number
    *out = v;
    return true;
  }

  // Our commands are one flat object: exactly one '{' first and one '}' last, no nesting, no junk
  // before or after, and no key twice (a parser that takes the first and one that takes the last
  // would disagree about what the line says).
  static bool flat_object(const char* line) {
    size_t n = strlen(line);
    if (n < 2 || line[0] != '{' || line[n - 1] != '}') return false;
    int open = 0, close = 0;
    for (size_t i = 0; i < n; ++i) {
      if (line[i] == '{') ++open;
      if (line[i] == '}') ++close;
    }
    return open == 1 && close == 1;
  }

  static bool unique_key(const char* line, const char* key) {
    const char* first = find_value(line, key);
    return first != 0 && find_value(first, key) == 0;
  }

  void handle_line(const char* line) {
    char schema[40], type[24], cid[kMaxId + 1];
    long version = 0;
    bool flat = flat_object(line);
    // A stop is the safety-critical command: honour it as soon as it is recognisable, even if the
    // rest of the line (id, schema) is damaged. Stopping the motor can never do harm.
    if (flat && unique_key(line, "type") && get_id(line, "type", type, sizeof type) &&
        strcmp(type, "stop_stimulus") == 0) {
      hal_.motor_set(0);
      active_ = false;
      if (get_id(line, "command_id", cid, sizeof cid)) send_ack(cid, "completed", 0);
      else ++malformed_;
      return;
    }
    if (!flat || !unique_key(line, "schema") || !unique_key(line, "version") || !unique_key(line, "type") ||
        !unique_key(line, "command_id") || !get_id(line, "schema", schema, sizeof schema) ||
        strcmp(schema, "vibeguard.station_command") != 0 || !get_int(line, "version", &version) ||
        version != 1 || !get_id(line, "type", type, sizeof type) ||
        !get_id(line, "command_id", cid, sizeof cid)) {
      ++malformed_;  // noise or a boot log line: counted, never acted on, never answered
      return;
    }
    expire();  // a stimulus whose time is up must not make this command "busy"
    if (strcmp(type, "hello") == 0) {
      send_hello(cid);
    } else if (strcmp(type, "deliver_stimulus") == 0) {
      char trial[kMaxId + 1];
      long setting = 0, dur = 0;
      if (!unique_key(line, "trial_id") || !unique_key(line, "setting") || !unique_key(line, "duration_ms") ||
          !get_id(line, "trial_id", trial, sizeof trial) || !get_int(line, "setting", &setting) ||
          !get_int(line, "duration_ms", &dur) || dur < 1 || dur > kMaxDurationMs) {
        send_ack(cid, "rejected", "invalid command");
      } else if (setting != 0 && (setting < min_ || setting > max_)) {
        send_ack(cid, "rejected", "setting out of range");
      } else if (active_) {
        send_ack(cid, "rejected", "busy");
      } else {
        send_ack(cid, "accepted", 0);
        strcpy(trial_, trial);
        have_trial_ = true;
        end_us_ = now_us() + (uint64_t)dur * 1000u;
        hal_.motor_set((int)setting);  // setting 0 (catch trial) leaves the motor off
        active_ = true;
        send_trial_event("stimulus_started");
      }
    } else {
      send_ack(cid, "rejected", "unknown command");
    }
  }
};

}  // namespace vg

#endif  // VG_STATION_CORE_H
