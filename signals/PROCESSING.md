# Host processing skeleton (Prompt 3) — reference implementation

`vibeguard_signals.processing` takes device-contract records in and produces derived
records out. It is a library plus a CLI for integration, **not** a backend service.
Abduljabbar's backend can embed it. The only feature so far is a **placeholder**
(`window_stats`); the EMG envelope/calibration (Prompt 4) and vibration features/score
(Prompt 5) will be added as further `features` keys. All tested inputs are simulated.

## Layers

| Layer | Module | Responsibility |
|---|---|---|
| Transport | `process_cli.py` (and the caller's own code) | Read NDJSON frames, decode with `contract.parse_line`, supply host receive time if used |
| Orchestration | `processing/processor.py` | Sequencing, continuity, invalidation, availability, staleness |
| Storage | `processing/window.py` | Bounded time-based sample window |
| Algorithms | `processing/features.py` | Pure functions over window samples |
| Serialization | `processing/serialize.py` | Compact deterministic NDJSON, non-finite numbers refused |

## Commands (run from `signals/`)

```sh
uv run vg-process contract/fixtures/sim/happy_path_seed1_3s.ndjson -o /dev/null   # smoke, no hardware/backend
uv run vg-process contract/fixtures/sim/fault_mix_seed1_5s.ndjson                 # derived NDJSON to stdout
uv run vg-sim --scenario fault_mix --duration 10 --fast | uv run vg-process -     # pipeline
uv run vg-sim --scenario happy_path --duration 30 | uv run vg-process --clock host -   # live-like pacing
```

The summary (events, feature statuses, peak buffer occupancy versus capacity) goes to
stderr; `--summary-json FILE` saves it as well.

## API and lifecycle

```python
from vibeguard_signals.processing import Processor, ProcessingConfig, to_ndjson_line
proc = Processor(ProcessingConfig())           # one Processor per device
for record in decoded_records:                  # dicts from contract.parse_line
    for out in proc.feed(record):               # host clock: proc.feed(record, rx_time_s=t)
        sink(to_ndjson_line(out))
# host clock only: call proc.poll(now_s) periodically so silence becomes "stale"
for out in proc.finish():                       # end of input: invalidates every stream
    sink(to_ndjson_line(out))
```

- `feed()` validates every record and never raises on bad input. Invalid records become
  `invalid_record` events and are not processed. Rejections (invalid, foreign device,
  retired boot, duplicate, late) happen **before any state changes**: a rejected record
  cannot alter provenance, windows, or sequence tracking. Undecodable frames go through
  `feed_invalid_frame(issues, where)`.
- The processor is deterministic in `device` clock mode: the same input gives the same
  output.
- **Reset behaviour:** a new `boot_id` fully resets every stream (continuity, window, rate,
  device state). Gaps, timing discontinuities, channel/unit changes, rate changes,
  provenance changes, disconnected/stale data, and device-reported sensor faults reset
  only the affected window, which then has to warm up again.
- **Warm-up:** a stream reports `ok` only after `warmup_s` (default: equal to `window_s`)
  of data since its last reset. During warm-up the status is `warming_up`, and the reasons
  include the cause of the reset (for example `["warming_up", "gap"]`).

## Output records (`schema: "vibeguard.features"`, `version: 1`, proposed)

Every output record carries `device_id`, `boot_id`, `provenance` (`source`, plus
`recording_id` / `original_source` when present), `timebase: "device_monotonic_us"`,
`t_us` (device time), and `source_seq`.

### `stream_features` — one per accepted batch, plus one per invalidation

```json
{"type":"stream_features","stream":"emg","t_us":504000,"source_seq":21,
 "status":"degraded","reasons":["clipped"],
 "window":{"length_s":0.1,"samples":100,"start_us":405000,"end_us":504000,
           "nominal_rate_hz":1000.0,"rate_source":"device_hello","measured_rate_hz":1000.0,
           "expected_samples":100.0,"complete_samples":100,"coverage":1.0},
 "quality":{"clipped_samples":3,"clipping_detection":"device_declared_limits","device_flags":["clipped"]},
 "features":{"window_stats":{"status":"degraded","units":{"emg0":"adc_counts"},
                             "mean":{"emg0":2041.11},"ac_rms":{"emg0":385.657}}}}
```

| `status` | Values | Meaning |
|---|---|---|
| `ok` | present | Full window, coverage ≥ `min_coverage`, nothing degraded |
| `degraded` | present | Usable but `reasons` lists `missing_values`, `clipped` (host found values at the limits), or `device_flagged_clipping` (the device flagged clipping that host checks did not find, or could not check) |
| `warming_up` | **null** | Not enough data since the last reset |
| `unavailable` | **null** | `reasons` gives one of: `no_data`, `gap`, `rate_mismatch`, `timing_discontinuity`, `sensor_disconnected`, `sensor_stale`, `device_reports_disconnected`, `device_reports_stale`, `stale`, `insufficient_coverage`, `channels_changed`, `rate_changed`, `provenance_changed`, `end_of_input` |

**Unavailable values are `null`, never 0.** Old values are never repeated: every
invalidation emits a fresh `unavailable` record at once. Display should go by `status`,
not by whether numbers happen to be present.

### `stream_event`

`{"type":"stream_event","event":<kind>,"stream":"emg"|"imu"|null,"t_us":…,"source_seq":…,"detail":{…}}`

| Event | Trigger | Effect |
|---|---|---|
| `boot_change` | New `boot_id` (`detail.hello_received` false if the hello was lost) | Full reset |
| `seq_gap` | `seq` jumped (`missing_records`) | Informational. The affected stream shows its own `sample_gap` |
| `duplicate_record` / `late_record` | `seq` already seen / older than newest | Record dropped |
| `sample_gap` | `first_sample_index` jumped (`missing_samples`; `cause` is `device_overrun`, `transport_or_unreported`, or both) | Window reset |
| `sample_overlap` | Index went backwards with a new `seq` | Record dropped |
| `timing_discontinuity` | Contiguous index but the step deviates > `continuity_tolerance` × interval | Window reset |
| `rate_mismatch` / `rate_mismatch_cleared` | The expected rate (config → hello → batch) disagrees by > 10 % with the batch's declared rate **or** its timestamp-implied rate / agreement restored | **Availability condition:** while mismatched, samples never enter the window and every record is `unavailable` (`rate_mismatch`). Once cleared, the stream warms up again |
| `device_sensor_state` | Status record changed a sensor state | `disconnected`/`stale` is **sticky** until the device reports `ok` |
| `stream_stale` / `stream_recovered` | Silence beyond `stale_after_s` / data resumed | Unavailable while stale |
| `buffer_overflow` | Window hit its hard capacity | Oldest samples discarded |
| `provenance_changed`, `channels_changed`, `rate_changed`, `foreign_device`, `retired_boot_record`, `invalid_record`, `end_of_input` | As named | See code |

## Staleness clocks

- `clock="device"` (default; files, replay, tests): a stream is stale when the newest
  device time seen on *any* stream is more than `stale_after_s` (0.25 s) past this
  stream's last sample. This catches one stream going silent while the other keeps going.
  It **cannot** detect the whole device going silent, because no record arrives to
  advance device time. That case needs the host clock.
- `clock="host"` (live ingestion): staleness is judged from the caller's monotonic
  `rx_time_s` on `feed()` and `now_s` on `poll()`. Wall/host time never enters
  `t_us`. `vg-process --clock host` reads input on a separate thread and polls every
  `--poll-interval` (default 0.05 s) while input is silent. Output is flushed after each
  write, so silence on an open stdin shows up as `stream_stale` before EOF.

## Configuration (`ProcessingConfig` / `StreamConfig`)

| Field | Default | Note |
|---|---|---|
| `emg.window_s` / `imu.window_s` | 0.1 / 0.5 s | Placeholder windows |
| `warmup_s` | = `window_s` | |
| `min_coverage` | 0.9 | Complete rows / expected rows |
| `expected_rate_hz` | `None` = **unknown** | Falls back to the hello's nominal rate, then to the batch's declared rate. `rate_source` reports which |
| `raw_limits` | `None` = **unknown** | Falls back to the hello's `raw_limits` (if `use_device_raw_limits`), else clipping detection reports `unavailable_unknown_limits` and `clipped_samples: null` |
| `conversion` | `None` = **unknown** | Reserved; physical units only arrive in Prompts 4–5, and only with confirmed conversions |
| `stale_after_s` | 0.25 s | |
| `continuity_tolerance` | 0.5 | Fraction of one sample interval |

## Bounds

- **Window capacity:** at most `ceil(window_s × rate × 2) + 256` samples. The 2 is
  `buffer_rate_ceiling_factor`. Overflow discards the oldest samples and emits an event.
- **Typical occupancy:** after eviction, a window holds at most about one window of
  samples (100 EMG / 200 IMU at the simulated rates). The peak, measured just before the
  incoming batch's eviction, is one window plus one batch (150 / 221, as `vg-process`
  reports). Both are independent of run length (tested on 5 s and 60 s runs).
- **Other state:** the seq de-duplication memory (256) and the retired-boot memory (8)
  are also bounded.

## Limitations

- `window_stats` (mean and AC RMS in raw units) is a placeholder. It is **not** muscle
  activation or vibration exposure.
- An embedding backend in host-clock mode must call `poll()` from its own timer, as
  `vg-process` does.
- Reordered records are dropped rather than re-sequenced. USB serial delivery is in
  order; reordering signals a fault worth seeing.
- A mid-window null row still counts toward the measured rate (the device acquired it);
  it lowers coverage.
- Every behaviour here was verified on synthetic data. Real timing jitter, noise, and
  stale/disconnect detection depend on hardware that is not confirmed yet.
