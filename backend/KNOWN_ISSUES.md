# Known issues and limits (backend + signals)

Found by two rounds of adversarial review and soak/serial/clone testing. Fixed items are not
listed. Everything below was reproduced.

## In the signals package (Salaar's) - not changed by the backend

1. **A silent device keeps an "accumulating" score.** When the link stays up but no data arrives,
   the processor marks emg/imu `unavailable/stale`, yet the latest `experimental_score` keeps
   `status: accumulating` with its last rate (vibration input `ok`, `age_s` 0.0), because the stale
   invalidation re-features at the last device time. *Mitigation in the backend:* the runtime
   reports `data_fresh: false`, masks the score in `GET /api/runtimes/{name}` and the WebSocket
   snapshot, and publishes `device_silent`. The raw `derived` stream still carries the old score
   record, so consumers should use the runtime `data_fresh` flag. *Proper fix:* the processor should
   emit a paused score with `*_stale` reasons.
2. **A timing regression inside one boot leaves the score "accumulating".** If device time steps
   backwards by 5 s with the same `boot_id`, the processor emits `timing_discontinuity` and the
   features warm up, but the score engine ignores every update with `t <= last_t` for the next 5 s
   and keeps reporting its old rate. *Proper fix:* treat a backwards step as a score segment
   boundary (flush, reset tracks) or pause the score with a `timing_regression` reason.

## In the backend

- **Host stall false-stale.** With the host clock, a 0.4 s stall of the backend thread with no
  data loss can declare a stream stale and fail an in-progress calibration. It recovers on the
  next frame. Mitigation: none yet (a 250 ms floor is documented).
- **Replay of a mid-stream session** scores about the warm-up time (~1.2 s, ~8%) lower than the
  live session (`score.started_mid_stream` flags it).
- **Examples are regenerated, not hand-edited.** Ids and timestamps are normalised, but counters
  and durations (`seq`, `hub_seq`, `excluded_duration_s`) vary slightly per run, so compare
  shapes, not values.
- **Serial speed.** `--baud 921600` cannot be opened on a pseudo-terminal on macOS (a test
  environment limit); on a real USB serial device it is the intended rate. Unmeasured on hardware.
- **A delivered stimulus is not persisted before it is answered**: a crash loses that one trial (the
  session is marked interrupted; the controller is told to stop the motor on restart).
- **Only the relaxed reference captured before a session** is not saved with it, so a replay of a
  session whose calibration is completed inside it (relaxed before, squeeze during) stays uncalibrated;
  the session is flagged `started_with_relaxed_reference_only`.
- **`verify-backup` is an integrity check, not authentication**: BACKUP.json is not signed, so someone who
  edits a file and recomputes its hash passes.
- **Station controller** is simulated; the stimulus contract is proposed, not agreed.

## Hardware tooling (never run on a real board)

- **Pseudo-terminal limits.** `fake-device` on macOS only accepts some baud rates (115200, 230400), does not
  throttle, and on Linux starts its first boot before any program connects (the first hello may be missed). A
  close-and-reopen under about 50 ms is not noticed. Real USB timing, driver buffering and re-enumeration are
  unmeasured.
- **DTR/RTS cannot be observed on a pty.** `--no-dtr` / `serial.dtr: false` set the lines before opening, but whether
  it stops a given board resetting depends on the OS driver and the board; native-USB boards that wait for DTR need it
  left on. Nothing in the backend writes to the device, so a firmware that waits for a host command stays silent.
- **EMG rawness is a heuristic.** `serial-check` judges it from the spectrum (power above 20 Hz after removing drift)
  and one-sidedness; it separates the obvious cases (smooth envelope, rectified output) from raw EMG in tests on
  synthetic signals, but real EMG modules may surprise it. The profile's `emg.output` is a statement by a human,
  not a measurement.
- **Host-clock cross-check** needs about 3 s of data and only warns on a >3% disagreement.
- **Firmware not run yet.** The ESP32 firmware (Salaar) is described, not in this repository, and has not been connected.
  The backend was built to its README: 921600 baud, CP2102, `device_id` selection, boot banner skipped, hello every 5 s,
  DTR/RTS off. Behaviour that only the real board can show (whether the open still resets it, real throughput and error
  rate at 921600, the exact `hardware`/`counters` keys) is unverified; `serial-check` reports whatever it actually sends.
