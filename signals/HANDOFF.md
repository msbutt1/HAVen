# Salaar handoff — firmware, signals, calibration, experimental scoring

Concise log of decisions and progress. Teammates' planning files are not edited.

## Commands (verified 2026-11-06)

Run these from `signals/`; uv creates the project-local `.venv`:

```sh
uv sync                                    # install package + pytest (dev group)
uv run pytest -q                           # contract tests
uv run vg-validate contract/fixtures/valid/*          # exit 0
uv run vg-validate contract/fixtures/invalid/*.json   # exit 1, one reason per file
uv run vg-validate - < some_stream.ndjson             # validate a stream from stdin
uv run vg-bandwidth                        # provisional serial throughput estimate
uv run vg-sim --list                       # simulator scenarios (see SIMULATOR.md)
uv run vg-sim --scenario happy_path --seed 1 --duration 10 --fast -o run.ndjson
uv run vg-sim --scenario fault_mix --seed 1 --duration 10            # wall-paced to stdout
uv run vg-process contract/fixtures/sim/fault_mix_seed1_5s.ndjson -o /dev/null   # processing smoke test
uv run vg-sim --scenario fault_mix --duration 10 --fast | uv run vg-process -    # sim -> processing
uv run pytest -q tests/test_emg.py         # EMG envelope + calibration algorithm tests
uv run pytest -q tests/test_vibration_score.py   # vibration features + experimental score
uv run vg-demo run --out out/demo --seed 1 --run-id demo-seed1   # offline end-to-end demo (DEMO.md)
uv run vg-demo replay out/demo             # replay as provenance=replay; exit 0 iff output matches
uv run vg-replay out/demo/telemetry.ndjson --speed 10 | uv run vg-process -
uv run python examples/embed_processor.py  # API embedding example for the backend
```

## Decisions

- **D1 Layout.** `signals/src/vibeguard_signals/` holds importable code and is the one
  authoritative implementation. `signals/contract/` holds the spec and fixtures.
  `firmware/` is deferred until Prompt 7 or a confirmed board.
- **D2 Tooling.** uv with a project-local venv. pytest is a dev dependency only. The
  validator uses only the standard library; numpy/scipy are added when processing needs
  them.
- **D3 Ownership.**
  - Salaar: device contract, simulator, processing/calibration library, and eventual
    controller-level stimulus commands.
  - Abduljabbar: APIs, device adapters, persistent recording, production replay.
  - Fahad: station sequencing.
  - Salaar's replay is for offline algorithm verification only.
- **D4 Contract.** The device telemetry contract v1 is **proposed**. It is separate from
  the backend/frontend API. It uses NDJSON for the simulator/host; the physical wire
  format is undecided. See `contract/TELEMETRY_CONTRACT.md`.
- **D5 Boundary.** The device only acquires. All processing runs on the host initially.
- **D6 Validator choice.** A hand-written stdlib validator rather than JSON Schema:
  cross-field rules (timing vs rate, nulls vs flags, conversion vs units) live in one
  place, with no added dependency. There is no second copy of the schema.
- **D7 Simulator.** Pure stdlib (seeded `random.Random` per boot/stream/purpose). It
  emits only contract records, validated before emission. It keeps device time,
  simulation time, and wall pacing separate (see `SIMULATOR.md`).
- **D8 Faults vs defects.** Sensor faults (clipping, dropouts, nulls, disconnect, stale,
  reboot) are schema-valid telemetry from `vg-sim`. Malformed protocol records exist only
  in `contract/fixtures/invalid/`. Disconnected/stale flags represent injected or
  detected conditions; the validator cannot infer them from values.
- **D10 Processing skeleton.** Layers: transport (CLI/caller), orchestration
  (`processor.py`), bounded storage (`window.py`), pure algorithms (`features.py`),
  serialization. One `Processor` per device. Conservative invalidation: any sample gap,
  timing discontinuity, boot change, disconnect/stale, or provenance change resets the
  window and forces warm-up. Reordered records are dropped, not re-sequenced.
  Device-reported disconnected/stale is sticky until the device reports `ok`, so firmware
  must send a status on sensor-state transitions. Details: `PROCESSING.md`.
- **D11 Staleness clock.** Device clock by default (deterministic; detects one stream
  going silent). Host clock with `poll()` is needed live, to detect total silence.
- **D12 Review fixes (post-Prompt 3).**
  1. Rate mismatch is now an availability condition, checked on every batch against the
     declared and timestamp-implied rates, and it clears on agreement.
  2. All rejections happen before any state changes.
  3. A device `clipped` flag degrades the result even when host limit checks pass.
  4. `vg-process --clock host` polls on a timer and flushes its output.
  5. Simulator runs get random identities by default; `--run-id` fixes them for fixtures.
  6. IMU timestamps are offset by the maximum jitter, so low rates stay valid.
- **D13 EMG pipeline (Prompt 4).**
  - Causal stages: EMA offset removal → Butterworth band-pass (SOS, carried state) →
    100 ms moving RMS.
  - Dependencies numpy + scipy were added after checking the SciPy 1.18 docs for
    `butter(output='sos', fs=)` and `sosfilt(zi=)`.
  - Cutoffs are validated against 0.9 × Nyquist of the actual rate, never clamped.
  - Filter state resets through the existing stream-reset hook, and on any null sample
    (no imputation).
- **D14 Calibration (Prompt 4).**
  - Relaxed/squeeze state machine in device time.
  - References are medians. Separation must satisfy ratio ≥ 2 and absolute ≥ 1 unit.
  - Normalised value is unbounded, plus a clamped display value and
    above/below-range flags.
  - Conservative invalidation: reboot, provenance, pipeline signature, device.
  - Labelled `relative_muscle_activation_proxy`, never force/%MVC.
- **D15 Vibration (Prompt 5).**
  - Causal 5 Hz Butterworth high-pass (SOS) → 1 s per-axis RMS → root-sum-of-squares
    combined.
  - `static_mean` (gravity) is reported separately and is never called vibration.
    Unweighted; not ISO 5349.
  - m/s² only with a device-declared or config conversion that cites its source.
- **D16 Experimental score (Prompt 5).**
  - `k·(V/v_ref)^p·(1+w·clamp(a,0,cap))` in au/h, integrated over valid elapsed device
    time.
  - Conservative: any unusable or stale input, or an unusable arriving update, excludes
    time, with a reason.
  - Older updates never replace newer ones. New accumulation on provenance change; boot
    segments never joined.
  - No notch or adaptive cancellation; the EMG interference limitation stands.
- **D17 Demo and replay (Prompt 6).**
  - `vg-demo` wires the existing simulator, processor, calibration and serializer; there
    is no parallel implementation.
  - The scripted 30 s `demo` scenario covers calibration commands, activation, vibration,
    an EMG disconnect, transport loss and recovery.
  - `vg-replay` rewrites only provenance (keeping `original_source` and `recording_id`);
    pacing is declared and content-neutral.
  - `vg-demo replay` checks that re-processing gives output identical apart from
    provenance.
  - The demo's IMU conversion is the simulator's synthetic scale, cited as such.
- **D18 Score interval semantics (revised after the second review).**
  - Each update covers its own input over (its previous update, t], at most the
    freshness limit back.
  - Time is finalised only once both inputs have covered it, or one is provably stale
    (more than its freshness limit behind the newest update).
  - Exclusion keys name every blocking input (sorted ` & `, reason chains `+`).
  - Demonstrated by `tests/test_review2_fixes.py`:
    - identical totals and attribution for both arrival orders, when one or both inputs
      turn invalid, with equal timestamps and with 3/25/49 ms stagger;
    - randomised interleavings with lead within freshness give identical results;
    - a stream leading by more than freshness makes the lagging input `_stale`;
    - pending segments stay bounded over 100 s of one-sided input;
    - the processor is exactly order-invariant on a loss-free stream.
  - Not claimed: invariance across a transport loss, because the processor's
    device-clock staleness rule reacts to arrival order there (both outcomes are
    conservative).
  - Demo score moved 0.048325 → 0.048601 au (valid 19.55 → 19.695 s): intervals closed by
    a usable update are now covered by it.
- **D19 Calibration hardening (second review).**
  - Every EMG pipeline reset, stream-level or a null sample inside a batch, fails an
    active capture directly through a reset listener (`interrupted_<reason>`), even when
    the recovery batch alone completes warm-up. Resets before a capture has started only
    delay it.
  - `load_calibration` validates structure, ids, provenance, channel sets, finite
    non-boolean values, relaxed ≥ 0, separation (current config) and pipeline channels
    before installation. Invalid records emit `calibration_rejected` with
    `problems`, keep the current state, and never crash processing (15 malformed
    variants tested).
- **D9 Transport.** No serial transport yet. NDJSON on stdout/file is the host
  simulation format until the board is confirmed.

## Progress

| Step | State |
|---|---|
| Prompt 0 baseline | Done. Repo had only planning docs; no conflicts. |
| Prompt 1 contract | Done. Spec, 8 valid + 22 invalid fixtures + 1 NDJSON stream, validator, CLI, 46 passing tests, bandwidth estimate. |
| Baseline commit | `5637c0e` (planning docs + Prompt 1). The archive `project-plan.zip` is left untracked. |
| Prompt 2 simulator | Done. `vg-sim` with 11 scenarios, 2 tracked recordings (`contract/fixtures/sim/`), 40 new tests (86 total passing). |
| Prompt 3 processing skeleton | Done. `vibeguard_signals.processing` + `vg-process`, placeholder `window_stats`, 35 new tests (121 total passing). **Astra review checkpoint now.** |
| Checkpoint review fixes | Done. 6 correctness findings fixed, each reproduced first and pinned by `tests/test_review_fixes.py` (47 tests; 168 total passing). Fixtures regenerated with fixed run IDs. |
| Checkpoint commit | `79cc417` (Prompts 2–3 + review fixes). |
| Prompt 4 EMG + calibration | Done. `processing/emg.py`, `processing/calibration.py`, `features.emg_activation`, 51 new tests (219 total passing). Simulator: relaxed envelope ≈23 vs strong ≈290 counts_rms. Calibration reproducible. Invalid calibrations fail visibly. 60 s of data processed in 0.6 s. |
| Prompt 5 vibration + score | Done. `processing/vibration.py`, `processing/score.py`, `features.vibration`, `experimental_score` records, 48 new tests (267 total passing). The default CLI scores 0 with explicit reasons (no conversion, uncalibrated). |
| Scoring regression coverage | 5 dedicated tests for the four scoring cases. Each Prompt 5 fix was mutation-checked: reverting fix 1 fails both case-1 tests; reverting fix 2 fails cases 2 and 4. |
| Prompt 6 demo + replay | Done. `vg-demo`, `vg-replay`, `demo` scenario, `examples/embed_processor.py`, `DEMO.md`, 13 new tests (286 total passing). Demo: calibration done at 7.55 s; activation ≈0 / ≈1.0 / ≈0.59; vibration 3.467 m/s²; score 0.048325 au over 19.55 s valid, paused through disconnect, loss and re-warm-up; replay matches: YES. |
| Second review fixes (A–C) | Done. All three reproduced first (EMG-first order counted an invalid 0.05 s as valid; capture spanned a gap with 250-sample batches; malformed loads crashed with ZeroDivisionError/KeyError/TypeError or installed NaN). 32 regression tests in `tests/test_review2_fixes.py`; 318 total passing. Demo replay still matches. |
| Prompts 7–8 | Not started. **Re-review checkpoint now.** |

## For Abduljabbar (ingestion) and Ali (display)

**Getting a stream now (Abduljabbar)**
- Wall-paced live-like stream: `uv run vg-sim --scenario fault_mix --duration 60 | your_ingest`.
- Files: `contract/fixtures/sim/happy_path_seed1_3s.ndjson` (clean) and
  `fault_mix_seed1_5s.ndjson` (loss, overrun, nulls, disconnect, stale, clipping, reboot).
- The stream contains no wall time; stamp receive time yourself.

**Consuming the examples**
- Start from `contract/fixtures/valid/stream_simulated_boot.ndjson`, which is one boot in
  order. The single-record `.json` files show each record type and each fault case.
- Validate input with `uv run vg-validate`, or in Python:
  `from vibeguard_signals.contract import parse_line`, then
  `record, issues = parse_line(raw_line)`. A record is valid only if `issues == []`.
  Reject invalid records whole.
- Key on (`device_id`, `boot_id`). Track `seq` for lost/duplicate records and
  `first_sample_index` for device-side drops. Treat a new `boot_id` as a reset.
- Timestamps are device-monotonic µs, not wall time. Add your own receive time.
- `null` means unavailable, and must stay distinct from `0` in storage and on screen.
- Show `provenance.source` everywhere data appears. `simulated` and `replay` must be
  visibly labelled, and replay keeps `recording_id` + `original_source`.
- Raw `adc_counts`/`raw_counts` have no physical meaning yet. Don't label them mV or m/s².

**Derived output (Prompt 3, proposed `vibeguard.features` v1):** see `PROCESSING.md`.
Display by `status` (`ok`/`degraded`/`warming_up`/`unavailable`) and `reasons`; values are
`null` whenever not usable. `window_stats` is a placeholder in raw units, not something to
present as activation or vibration.

**Ali specifically:** these records are raw device data, not display values. Normalized
muscle activation, vibration features, and the experimental score come from later host
processing steps, which will publish their own example outputs.

**Decisions needing alignment**
1. Whether the backend accepts this record shape as-is or wraps it (it should not mutate
   the device fields).
2. Host freshness/stale limit for the live view: 0.25 s proposed (`stale_after_s`). The
   backend should run the processor in host-clock mode and call `poll()`.
3. Who stamps wall-clock receive time, and under what field name (backend contract).
4. Physical wire format and baud/USB CDC, once Collins confirms the board.
5. Batch duration (≈50 ms proposed), weighing latency against header overhead.
6. The stimulus command contract with Fahad (separate from this telemetry contract).

## Hardware bring-up requirements (must be confirmed before trusting outputs)

1. **EMG output type: raw EMG vs an already-smoothed envelope.** The processing path
   assumes **raw** EMG (`input_assumption: "raw_emg"` appears in every `emg_activation`
   output and every calibration record). If the confirmed module outputs a rectified or
   smoothed envelope, band-pass + RMS is wrong for that signal. Stop and decide before
   calibrating a wearer. No second pipeline is built speculatively. To check: read the
   module datasheet and which output pin is wired, then inspect a raw recording. Raw EMG
   swings both ways around a mid-scale offset; an envelope is one-sided and slow.
2. **Accelerometer range/sensitivity → `StreamConfig.imu.conversion` + `conversion_source`.**
   Until then there are no m/s² values and no score.
3. **IMU ODR and internal low-pass**, which set the real upper vibration band edge.

## Unresolved hardware assumptions (all explicitly `null` / provisional)

- Controller/board model and revision, and controller count.
- EMG front-end model, output range, ADC resolution/reference → `raw_limits`, mV conversion.
- IMU model, bus/address, range, ODR → `raw_counts` → m/s² conversion, `nominal_rate_hz`.
- Actual sample rates and jitter (measured from timestamps, never assumed).
- Serial link type (UART baud vs native USB) and measured throughput.
- Whether the device can detect EMG disconnection or IMU staleness at all.
- Whether the EMG front end outputs raw EMG (the pipeline assumes so) or an
  already-rectified envelope; also its analog bandwidth, which bounds a sensible
  band-pass.
- The real EMG sample rate, which decides whether the default 20–450 Hz band is even
  valid (it needs ≥ 1000 Hz).
- Mains interference (50/60 Hz) and vibration coupling into EMG on the real sleeve.
  No notch is implemented; the hour-12 fallback applies.
- Calibration durations and thresholds (3 s captures, ratio 2) are untested on a
  wearer.
- How well IMU mounting on the sleeve couples tool vibration (a soft mount attenuates it),
  and the accelerometer's range relative to tool vibration (clipping risk).
- Score coefficients are arbitrary defaults; no physical meaning is claimed.
