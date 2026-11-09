# Host processing — reference implementation (Prompts 3–5)

`haven_signals.processing` takes device-contract records in and produces derived
records out. It is a library plus a CLI for integration, **not** a backend service.
Abduljabbar's backend can embed it. Outputs are kept separate:
- `window_stats`: placeholder statistics, both streams.
- `emg_activation`: EMG envelope and normalized activation; see
  [EMG envelope and calibration](#emg-envelope-and-calibration-prompt-4).
- `vibration`: measured accelerometer vibration.
- `experimental_score` records: the combined score; see
  [Vibration and experimental score](#vibration-and-experimental-score-prompt-5).

All tested inputs are simulated.

## Layers

| Layer | Module | Responsibility |
|---|---|---|
| Transport | `process_cli.py` (and the caller's own code) | Read NDJSON frames, decode with `contract.parse_line`, supply host receive time if used |
| Orchestration | `processing/processor.py` | Sequencing, continuity, invalidation, availability, staleness |
| Storage | `processing/window.py` | Bounded time-based sample window |
| Algorithms | `processing/features.py`, `emg.py`, `calibration.py`, `vibration.py`, `score.py` | Window statistics; stateful causal EMG envelope and vibration filters (reset through stream-reset hooks); calibration state machine; experimental score integration |
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
from haven_signals.processing import Processor, ProcessingConfig, to_ndjson_line
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

## EMG envelope and calibration (Prompt 4)

### Pipeline (`processing/emg.py`; all causal; state carried across batches)

1. **Offset removal.** `offset_mode="ema"` (default): subtract a running mean with
   `offset_tau_s` = 1 s, initialised to the first sample after each reset, so the ADC
   mid-scale offset doesn't excite the band-pass. `"fixed"` subtracts `fixed_offset`;
   `"none"` relies on the band-pass.
2. **Band-pass.** Butterworth, `order` 2 per edge (4th-order band-pass). Designed with
   `scipy.signal.butter(..., btype="bandpass", fs=rate, output="sos")` and run with
   `sosfilt` with carried `zi`. Defaults are 20–450 Hz, valid only if
   `high_hz ≤ nyquist_headroom (0.9) × fs/2`. At 1000 Hz, 450 just fits. At 500 Hz the
   default is **rejected** (`filter_config_invalid`, with a detail message), not clamped;
   set e.g. `EmgFilterConfig(20, 200)`.
3. **Envelope.** Moving RMS over `envelope_window_s` = 100 ms, a common surface-EMG
   envelope length that balances latency against ripple. The first `settle_s` = 0.1 s of
   filter output after a reset is excluded. The status is `warming_up` until
   settle + window of contiguous valid samples (200 ms) have arrived.

Units are `<input unit>_rms`, e.g. `adc_counts_rms`. Measured on synthetic input: an
in-band sinusoid of amplitude A gives A/√2 within 4 %; a 4 Hz tone is attenuated to
below 10 %; pure DC gives under 0.5 counts.

**Filter state resets** on every stream reset (gap, timing break, rate change or mismatch,
reboot, disconnect, stale, provenance change, end of input) and on **any null sample**.
Missing samples are never imputed, so a stream with frequent nulls stays in warm-up.
After a reset, output is identical to a fresh pipeline fed only the post-reset data
(tested). Clipped samples (host-detected or device-flagged) in the envelope window make
the envelope `degraded` (`clipped`).

### Calibration state machine (`processing/calibration.py`)

`uncalibrated → capturing_relaxed → relaxed_captured → capturing_squeeze → calibrated`,
plus `failed` and `invalidated`. Commands (called by the backend's calibration API):

```python
proc.calibration_start("relaxed")   # then later:
proc.calibration_start("squeeze")
proc.calibration_cancel()
rec = proc.calibration_record()     # export the active calibration for storage
proc.load_calibration(rec)          # reinstall; used only while compatible
```

All commands return events (`calibration_state`, `calibration_rejected`,
`calibration_cleared`).

- **Capture.** Starts at the first usable envelope update after the command; if the
  envelope is still warming up, the capture waits. Once started, **every EMG filter
  reset** (gap, null sample, timing break, rate mismatch, stale, disconnect,
  provenance change) fails the capture directly (`interrupted_<reason>`), even if the next
  batch alone is long enough to finish warm-up. It lasts `relaxed_s` / `squeeze_s`
  (default 3 s) of device time, and the first `discard_s` (0.5 s) is excluded.
- **Rejected with `failed` and a reason when:**
  - any unusable update arrives after the capture began (`interrupted_<cause>`, e.g.
    `interrupted_gap`);
  - the clipped-sample fraction exceeds `max_clipped_fraction` (default 0, `clipped`);
  - fewer than `min_updates` (10) updates are retained (`insufficient_data`);
  - the separation is too small (`insufficient_separation`).
  Any failure restarts calibration from the relaxed capture. Squeeze without a valid
  relaxed capture is rejected (`relaxed_reference_missing`).
- **References.** The per-channel **median** envelope over the retained updates; P25/P75
  are recorded too.
- **Acceptance.** Per channel, squeeze ≥ `min_ratio` (2) × relaxed **and**
  squeeze − relaxed ≥ `min_separation` (1 envelope unit). The denominator below is
  therefore never near zero.
- **Normalised activation.** `(envelope − relaxed) / (squeeze − relaxed)`, reported
  **unbounded** as `value`. `display` is clamped to [0, 1], and `above_reference` /
  `below_relaxed` flag values outside the calibrated range, so nothing is concealed.
- **Identity.** `calibration_id` is a hash of the record, which holds: device, boot,
  provenance, pipeline signature and effective config, calibration config, both capture
  summaries, and the references.
- **Invalidation (visible, `invalidated` + reason).** Triggers:
  - a reboot (unless `survives_reboot=True`);
  - a provenance change;
  - a change in EMG rate, channels, units or pipeline config (`processing_config_mismatch`);
  - another device.
  A loaded calibration stays `calibration_unverified` until device, provenance, rate and
  config are known from data.
- **Loading is validated before installation.** `load_calibration` checks:
  - schema, version and label;
  - the ids and signature;
  - provenance;
  - that relaxed and squeeze name the same non-empty `emg0..7` channels;
  - finite, non-boolean numbers, relaxed ≥ 0, and the separation rule under the current
    config;
  - that the pipeline channels match.
  Any problem emits `calibration_rejected` (`invalid_calibration_record`, `problems: [...]`)
  and leaves the current state, including any active calibration, unchanged.
  Processing continues. Starting a new relaxed capture discards the active
  calibration.

### Output: `features.emg_activation` (EMG `stream_features` only)

```json
{"label":"relative_muscle_activation_proxy",
 "label_note":"normalised to this wearer's relaxed and squeeze calibration for the current electrode placement; not force, not %MVC, not clinically validated",
 "envelope":{"status":"ok","reasons":[],"value":{"emg0":289.18},"units":{"emg0":"adc_counts_rms"},"window_s":0.1},
 "normalized":{"status":"ok","reasons":[],"value":{"emg0":0.9937},"display":{"emg0":0.9937},
               "above_reference":{"emg0":false},"below_relaxed":{"emg0":false},"calibration_id":"cal-34448d401a1b"},
 "calibration":{"state":"calibrated","reason":null,"calibration_id":"cal-34448d401a1b","capturing":null}}
```

`envelope.status` and `normalized.status` are independent. For example, the envelope can
be `ok` while `normalized` is `unavailable` with reason `uncalibrated`.

### Known limitation found on synthetic data

Vibration that couples into the EMG band is not removed. In `vibration_emg_mixed`, a
synthetic 60 Hz tone of 75 counts raises the relaxed envelope from about 23 to about
59 counts. No notch or adaptive cancellation is implemented; adaptive cancellation is out
of scope. Whether real EMG is interpretable during vibration is the **hour-12 fallback
gate**: if not, demonstrate muscle activation and vibration separately.

## Vibration and experimental score (Prompt 5)

### `features.vibration` (IMU `stream_features`)

These quantities are deliberately kept apart:

| Field | Meaning |
|---|---|
| `static_mean` | Mean **raw** acceleration per axis over the window. Includes gravity and orientation. **Not vibration.** |
| `dynamic_rms` | Per-axis RMS after a causal Butterworth high-pass (`highpass_hz` 5 Hz, order 2, SOS with carried state) that removes gravity, orientation and slow arm motion |
| `combined_rms` | √(Σ per-axis dynamic RMS²), the RMS magnitude of the dynamic vector |
| `physical` | The same dynamic values in **m/s²**, only when the batch carries m/s² (`device_declared_conversion`) or `StreamConfig.conversion` is given **with a `conversion_source`**. Otherwise `unavailable` / `no_conversion`, with raw values still reported |

- **Passband and window.** The passband runs from `highpass_hz` to `lowpass_hz` if set,
  otherwise up to the **sensor's own bandwidth**: its internal low-pass and ODR, unknown
  until the IMU is confirmed, and at most Nyquist. The window is 1 s, and warm-up is
  0.25 s settle + 1 s window.
- **No frequency weighting.** The output is **unweighted** (`weighting` says so); no
  ISO 5349 weighting is applied or implied.
- **Measured on synthetic input:** a sinusoid of amplitude A gives A/√2 within 3 %; a
  two-axis signal gives the root-sum-of-squares; a stationary tilted gravity vector gives
  dynamic RMS < 0.5 counts while `static_mean` keeps |g|; 1 Hz arm motion is attenuated
  below 5 %.
- **Resets and quality.** The same reset rules as EMG apply: every stream reset and any
  null row reset the filter. Clipping (a saturated accelerometer underestimates
  vibration) makes the result `degraded`. A gyro-only stream gives `unavailable`.

### `experimental_score` records (one per EMG or IMU feature update)

```text
rate [au/h] = k_per_hour * (V / v_ref)^exponent * (1 + activation_weight * clamp(a, 0, activation_cap))
defaults:     k_per_hour = 1, v_ref = 1 m/s², exponent = 2, activation_weight = 1, activation_cap = 2
```

**Inputs:**
- **V** is `vibration.physical.combined_rms` in m/s². Raw counts are never scored.
- **a** is the normalized activation, maximum over EMG channels.
- `activation_mode="ignored"` gives a vibration-only score (a = 0), and the output
  declares it.

**What the units mean.** Units are **arbitrary (au, au/h)**. The record says explicitly
that this is not A(8), not a dose, not a percentage of any limit, and that the
activation weighting is an algorithm choice, not validated transmission. Raising the
activation raises the score by construction; that demonstrates the formula, nothing
physical.

**Interval semantics.** Integration uses **elapsed device time**, not update count:
1. **Coverage.** An update of an input at device time t covers that input over
   (its previous update, t], reaching back at most its freshness limit
   (`vibration_fresh_s` / `activation_fresh_s`, 0.25 s). After a longer silence, the older
   part is uncovered and counts as `<input>_stale`. The first update of an input per boot
   only marks its start.
2. **Finalisation waits for both inputs.** Time is added to valid or excluded totals only
   once every required input has covered it, or is provably stale: its last update is
   more than its freshness limit behind the newest update of any input. A valid update
   from one stream therefore **cannot** finalise an interval that the other stream later
   reports as invalid. Results are identical for either arrival order, with equal or
   staggered timestamps, as long as neither stream leads the other by more than the
   freshness limit. A stream that leads by more makes the lagging one stale; that is
   explicit freshness handling, not a reordering artefact.
3. **Validity and rate.** A finalised sub-interval is valid iff the covering update of
   every required input is usable (`ok`/`degraded`). Its rate uses those covering
   updates' values.
4. **Pending state** spans at most the largest freshness limit of device time. It is
   reported as `pending_s` and is bounded (tested over 100 s of one-sided input).
   `processor.finish()` flushes it, with unreported time excluded as
   `<input>_end_of_input`.

**Excluded time.** Each finalised excluded sub-interval is attributed in
`excluded_by_reason` to a key that names **every** blocking input (sorted, joined with
` & `). Each input appears with its full reason chain (joined with `+`), or as
`<input>_stale` / `<input>_no_data` when uncovered. Examples:
`activation_uncalibrated & vibration_warming_up`,
`activation_warming_up+gap & vibration_warming_up+gap`, `activation_stale & vibration_stale`,
`vibration_no_conversion`. Attribution follows from the same coverage, so it is also
independent of arrival order (tested). Degraded-but-counted time is reported as
`degraded_duration_s`.

**Ordering and boundaries:**
- Updates at or before an input's own last update are ignored, so duplicates and
  reordering never double-count, replace newer information, or produce negative time.
- Until anything is finalised, the start is the earliest first update of any input,
  whichever stream is delivered first.
- A reboot starts a new `boot_segment`: nothing spans boots, and the reboot gap is not
  measurable in device time.
- A provenance change starts a new `accumulation` (`score_accumulation_reset` event), so
  live, simulated and replay data are never summed.

**Time domain.** All time is device time, i.e. recording time for replay; wall-clock
pacing has no effect.

Each score record contains: `status` (`accumulating`/`paused`) and `reasons`, `rate`,
`cumulative`, `valid_duration_s`, `degraded_duration_s`, `excluded_duration_s`,
`excluded_by_reason`, `integrated_until_us` (finalised up to), `pending_s`, the latest `inputs` (value, units, status,
age, freshness limit), the `formula` with every coefficient, `accumulation`,
`boot_segments`, and the usual identity, provenance and timebase.

## Limitations

- `window_stats` (mean and AC RMS in raw units) is a placeholder. It is **not** muscle
  activation or vibration exposure.
- The EMG pipeline assumes the front end outputs **raw** EMG. If the confirmed module
  outputs an already rectified/smoothed envelope (some hobby EMG boards do), band-pass +
  RMS is wrong for that output, and the pipeline needs a different input mode.
- An embedding backend in host-clock mode must call `poll()` from its own timer, as
  `vg-process` does.
- Reordered records are dropped rather than re-sequenced. USB serial delivery is in
  order; reordering signals a fault worth seeing.
- A mid-window null row still counts toward the measured rate (the device acquired it);
  it lowers coverage.
- Every behaviour here was verified on synthetic data. Real timing jitter, noise, and
  stale/disconnect detection depend on hardware that is not confirmed yet.

## Tool use vs normal arm use (`tool_use` records)

One `tool_use` record follows every IMU `stream_features` record (about 20 per second
from the live wearable), plus one at the end of input. Implementation:
`processing/tool_use.py`. Configuration: `ToolUseConfig` in `processing/config.py`.

**How it decides.** The accelerometer (in m/s², so a cited conversion is required) is
split into two bands:
- the **tool band**, a 4th-order high-pass at 20 Hz: power tools vibrate continuously
  at tens to hundreds of Hz;
- the **arm-motion band**, 0.5–10 Hz: reaching, swinging, lifting, shaking.

| Step | Rule (defaults) |
|---|---|
| Candidate | Tool-band RMS ≥ 1.0 m/s² **and** ≥ 0.5 × arm-band RMS (0.5 s window) |
| → `tool` | Candidates sustained for **2 s**. Knocks and taps never qualify |
| → `no_tool` | Under 30 % of the last **3 s** had tool vibration (≥ 0.6 m/s², same ratio). Short pauses inside a job don't split it |
| `unknown` | No valid data (warming up, gap, no conversion). This time is counted as neither tool nor no-tool |

**Time and warnings.** The session survives gaps and device reboots;
`Processor.tool_use_reset_session()` starts a new one (bridge command `newshift`).
- `session_tool_s` counts only time with tool vibration, plus the 2 s that confirmed
  each bout.
- `bout_s` is the length of the current job, including short pauses.
- `bouts` and `longest_bout_s` summarise the session.
- `warnings`:
  - `continuous_tool_use_long`: the current bout passed 20 min;
  - `session_tool_time_approaching`: the session total reached 80 % of 2 h;
  - `session_tool_time_high`: the session total passed 2 h.

**These thresholds are provisional team settings, not regulatory exposure limits, and
not a vibration dose.** Tune them on the real tools and mounting.

Example (from `vg-sim --scenario tool_vs_arm`, simulated):

```json
{"type":"tool_use","state":"tool","status":"ok","candidate":true,
 "tool_band_rms_ms2":6.937,"motion_band_rms_ms2":2.217,"dominant_hz":120.0,
 "grip_activation":null,"gripping":null,"bout_s":2.05,"bouts":1,"longest_bout_s":2.05,
 "session_tool_s":2.05,"session_observed_s":20.05,"warnings":[],"thresholds":{...}}
```

`grip_activation` and `gripping` report the latest normalised EMG activation (when it
is calibrated) as supporting evidence. They are not required for detection.

**Verified on simulated data** (`tests/test_tool_use.py`, 11 tests):
- **The `tool_vs_arm` scenario** (60 s: arm swinging and knocks, then 120 Hz and 90 Hz
  tools): 2 bouts, 27.7 s of tool time against 27 s true, dominant 120 and 90 Hz found,
  and no false "tool" during arm use.
- **Rejected as arm use:** knocks at rest, and vigorous 8 Hz and 10 Hz shaking.
- **Bouts:** pauses shorter than 3 s keep one bout; longer pauses split it.
- **Robustness:** without a unit conversion the detector reports `unavailable`; gaps and
  reboots don't add tool time.

**Hardware validation with real tools is pending.**
