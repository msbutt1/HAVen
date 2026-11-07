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
- **Station controller** is simulated; the stimulus contract is proposed, not agreed.
