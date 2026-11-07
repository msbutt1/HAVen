# Salaar handoff — firmware, signals, calibration, experimental scoring

**Status (2026-11-06):** the pre-hardware software path is complete and runnable. Every
result below was verified on **synthetic (simulated) inputs only**. Nothing has run on
the wearable, so nothing here validates sensors, electrodes, mounting, timing on a
microcontroller, clinical relevance, grip force, or vibration dose. No firmware exists
yet: Prompt 7 is deferred until Collins confirms the board and sensors.

**Owned paths:** `signals/` only.
- Code: `src/vibeguard_signals/`
- Device contract and fixtures: `contract/`
- Docs: `SIMULATOR.md`, `PROCESSING.md`, `DEMO.md`
- This file

Teammates' planning files have been untouched since baseline `5637c0e`. Commits:
`5637c0e` (contract), `79cc417` (simulator + processing), `2bcccc5` (EMG, calibration,
vibration, score, demo/replay).

The existing tattoo-style sleeve needs **no fabrication**: no sewing, nylon,
conductive-fabric or custom-electrode task exists anywhere in this software or plan. Use
gel electrodes on skin; the sleeve only secures components and routes cables.

---

## 1. Verified commands

Run from `signals/`. All were re-run for this handoff; the first block was also run from
a fresh `git clone` of `2bcccc5`, which reproduced the demo byte-for-byte.

| Command | Verified result |
|---|---|
| `uv sync` | exit 0 (Python ≥ 3.11, numpy, scipy, pytest dev) |
| `uv run pytest -q` | **318 passed**: contract 46, simulator 41, processing 35, review fixes 47, EMG 51, vibration/score 53, demo 13, second review 32 |
| `uv run vg-demo run --out out/demo --seed 1 --run-id demo-seed1` | exit 0; 1216 telemetry / 2390 derived records; final score 0.048601 au over 19.695 s valid |
| `uv run vg-demo replay out/demo` | exit 0; "matches the original apart from provenance: YES" |
| `uv run vg-validate -q contract/fixtures/valid/* contract/fixtures/sim/*` | 325/325 valid, exit 0 |
| `uv run vg-validate -q contract/fixtures/invalid/*.json` | 0/22 valid, exit 1 (intended; one reason each) |
| `uv run vg-sim --scenario fault_mix --seed 1 --duration 10 --fast \| uv run vg-validate -` | 379/379 valid |
| `uv run vg-sim --scenario fault_mix --duration 10 --fast \| uv run vg-process -` | 379/379 accepted, 749 outputs |
| `uv run vg-sim --scenario happy_path --duration 3 \| uv run vg-process --clock host -` | 124/124 accepted (wall-paced, host clock) |
| `uv run vg-replay out/demo/telemetry.ndjson --speed 10 \| uv run vg-process -` | 1216/1216 accepted; 30 s replayed in about 3 s |
| `uv run python examples/embed_processor.py` | final score 0.048601 au (same implementation as the demo); calibration persisted and reloaded |
| `uv run vg-bandwidth` | exit 0; table in `contract/TELEMETRY_CONTRACT.md` |
| `uv run vg-sim --list` | 12 scenarios |

---

## 2. Components: completed versus deferred

| Completed (software, synthetic-verified) | Where |
|---|---|
| Device telemetry contract v1 (**proposed**): separate EMG/IMU batches, per-boot `seq`, sample indices, device-µs timing, units, quality flags, provenance; strict validator + CLI | `contract/TELEMETRY_CONTRACT.md`, `contract.py`, `vg-validate` |
| Deterministic simulated wearable: 12 scenarios including faults, never `live` | `simulator.py`, `vg-sim`, `SIMULATOR.md` |
| Streaming processor: continuity, gaps, duplicates, reboots, staleness (device or host clock), rate mismatch, bounded windows, provenance | `processing/processor.py`, `vg-process`, `PROCESSING.md` |
| Causal EMG envelope (offset → Nyquist-checked band-pass → 100 ms RMS); **assumes raw EMG** | `processing/emg.py` |
| Relaxed/squeeze calibration state machine, normalized activation proxy, validated load/export, conservative invalidation | `processing/calibration.py` |
| Vibration features: gravity separated, per-axis and combined dynamic RMS, m/s² only with a cited conversion, unweighted | `processing/vibration.py` |
| Experimental score (au/h, au), order-independent interval finalisation, excluded time by reason | `processing/score.py` |
| Offline demo, replay, CSV/text summaries, embedding example | `demo.py`, `vg-demo`, `vg-replay`, `examples/`, `DEMO.md` |

| Deferred / not implemented | Why / owner |
|---|---|
| **All firmware** (Prompt 7 portable skeleton, board adapters, build/flash) | Board and sensors unconfirmed; no speculative adapters (Salaar, after Collins) |
| Physical wire format / serial transport | Depends on board and link throughput (see §6) |
| Controller-level stimulus commands | Contract to be agreed with Fahad; the station engine is his |
| Backend ingestion, storage, APIs, production replay | Abduljabbar |
| Dashboard and station screens | Ali |
| Notch / adaptive cancellation of interference in EMG | Out of scope; hour-12 gate (§5) |
| Alternative pipeline for a pre-smoothed EMG envelope | Not built speculatively; bring-up check #1 (§4) |
| ISO frequency weighting, physical-unit EMG (mV) | Not justified with the available inputs |
| Stretch items (temperature flag, OLED, second IMU, pressure gating) | Not started |

---

## 3. Integration

### Abduljabbar (backend): minimal path

1. **Input now.** Use `uv run vg-sim --scenario fault_mix --duration 60` (wall-paced
   NDJSON on stdout), or the files `contract/fixtures/sim/*.ndjson` and
   `out/demo/telemetry.ndjson`. Every line is one contract-v1 record; examples are in
   `contract/fixtures/valid/`.
2. **Decode.** `record, issues = contract.parse_line(raw_line)`; a record is valid only if
   `issues == []`. Never mutate device fields. Key on (`device_id`, `boot_id`); a new
   `boot_id` is a reset. Add your own wall-clock receive time; device timestamps are µs
   since boot.
3. **Process in-process:** see `examples/embed_processor.py` and `DEMO.md`
   (*Embedding*). Steps:
   - one `Processor(ProcessingConfig(clock="host", ...))` per device;
   - `proc.feed(record, rx_time_s=time.monotonic())`, plus `proc.poll(time.monotonic())`
     from a ~50 ms timer;
   - calibration API → `proc.calibration_start("relaxed" | "squeeze")` /
     `calibration_cancel()`;
   - persist `proc.calibration_record()`, reload with `load_calibration`;
   - `proc.finish()` at the end of the stream.
4. **Outputs** are `stream_features`, `stream_event` and `experimental_score` records
   (`schema: vibeguard.features`, proposed); their format is in `PROCESSING.md`. Each one
   carries `device_id`, `boot_id`, `provenance` and `timebase`.
5. **Recording / replay.** Production recording is yours. `vg-replay` shows the required
   semantics: provenance becomes `replay` with `original_source` and `recording_id`
   preserved, everything else is unchanged, and pacing is declared separately.

### Ali (display semantics)

- **Show the status, not just numbers.** Every feature has a `status`: `ok`, `degraded`
  (usable, with `reasons`), `warming_up`, or `unavailable`. **Values are `null` when not
  usable; never show them as 0,** and never keep showing an old value after an
  `unavailable` record.
- **Keep the three quantities visibly separate:**
  1. Measured vibration: `features.vibration.physical.combined_rms`, m/s², labelled
     unweighted.
  2. Normalized muscle activation: `features.emg_activation.normalized`, labelled
     "relative activation proxy". Use `display` (0–1) for bars, and show the
     `above_reference` / `below_relaxed` flags.
  3. Experimental score: the `experimental_score` records, rate in au/h and cumulative in
     au, labelled experimental. Show `status`, `reasons` and `excluded_by_reason`.
- **Label provenance everywhere.** `simulated` and `replay` must be visible on every view.
  Simulated history must be labelled as such.
- **Calibration state** is in `features.emg_activation.calibration`, and changes arrive as
  `calibration_state` events with reasons. Failures (e.g. `insufficient_separation`,
  `interrupted_gap`) are messages for the user.

### Fahad (station)

- Nothing station-related is implemented here.
- The stimulus command contract (deliver stimulus, abort, acknowledgement) is still to be
  agreed. Its controller side is Salaar's firmware, which waits for the board.
- Motor settings must be reported as settings, not measured amplitudes.

### Decisions still needing alignment

1. Whether the backend accepts the device record shape as is, or wraps it without mutating
   it.
2. The receive-time field name and owner.
3. The live stale limit (0.25 s proposed) and score freshness limits (0.25 s).
4. Batch duration (~50 ms proposed).
5. The physical wire format and link, after the board is confirmed.
6. The stimulus command contract with Fahad.

---

## 4. For Collins: hardware facts needed, and first bring-up

### Facts needed

All of these are currently `null` / unconfirmed in the code:

1. **Controller:** board model and revision, controller count (one or two), USB-serial
   type (native USB CDC or UART bridge with its maximum baud).
2. **EMG front end:**
   - module model;
   - **output type: raw EMG or an already rectified/smoothed envelope** (pin used);
   - output voltage range;
   - analog bandwidth;
   - supply.
3. **ADC:** resolution, reference voltage and input range. These give `raw_limits`
   (clipping detection) and any mV conversion.
4. **IMU:**
   - model;
   - bus (I²C/SPI) and address;
   - configured range (±g);
   - output data rate;
   - internal low-pass setting;
   - FIFO/data-ready availability.
   These give `conversion` + `conversion_source`, the vibration upper band edge, and the
   staleness detection mode.
5. **Actual connection and power arrangement:** wiring and pin map, grounding, electrode
   placement and reference electrode, where the IMU is mounted on the sleeve and how
   rigidly.
6. **Serial throughput budget:** can the link carry about 32 kB/s (EMG 1 kHz + IMU
   400 Hz, compact JSON; `vg-bandwidth`)? That rules out 115200-baud UART for JSON.
7. **Local mains frequency** (50/60 Hz) and the controlled vibration source to be used
   (frequency, amplitude).

### First bring-up sequence

Each step names the check that already exists in software:

1. **Identify the board** and record facts 1–4 above in the hardware map.
2. **Verify wiring and electrical compatibility against the manufacturers' guidance**
   (logic levels, EMG module supply and output range versus the ADC input range, IMU
   voltage) **before** connecting the wearer. That check is Collins's.
3. **Compile and flash a minimal acquisition test** on the confirmed board (Salaar,
   Prompt 7 then begins): emit contract-v1 records from fake data first, then from real
   sensors.
4. **Confirm units and timing.** Record ≥ 60 s, then run `uv run vg-validate -` and
   `uv run vg-process -` on the recording. Check:
   - `measured_rate_hz` against the nominal rate;
   - no `rate_mismatch` or `timing_discontinuity`;
   - `sample_gap` / `seq_gap` counts, which must be ~0 for a stable link.
5. **Replace the fake adapter.** The real serial stream feeds the backend's adapter in
   place of `vg-sim`; the simulator stays for tests.
6. **Inspect real recordings.**
   - EMG at rest and while squeezing: **raw EMG swings both ways around mid-scale; an
     envelope is one-sided and slow.** If it's an envelope, **stop**: the current
     pipeline does not apply.
   - Check for mains hum, clipping at hard squeeze, a stationary IMU giving gravity only,
     and a deliberately unplugged electrode lead giving a visible fault (not silent zeros).
7. **Calibrate a wearer** (relaxed, then squeeze). Check normalized ≈0 relaxed and ≈1
   squeeze, and that a deliberately weak squeeze fails with `insufficient_separation`.
8. **Test simultaneous vibration** with the controlled source. Check: vibration rises in
   m/s²; the accelerometer does not clip; and **whether the relaxed EMG envelope rises
   from vibration alone** (interference, §5).
9. **Integrate the dashboard** through the backend, with live, replay and simulated
   sources labelled.

---

## 5. Hardware-only acceptance checks and fallback gates

### Acceptance checks

None of these can pass in software. Each needs the real wearable:

| Check | Pass condition |
|---|---|
| Sample rate measured | `measured_rate_hz` within ~1 % of nominal over 10 min; jitter recorded |
| Dropped samples measured | `sample_gap` / `overrun` / `seq_gap` counts recorded; ~0 at the chosen link settings |
| Filters fit the rate and bandwidth | No `filter_config_invalid`; EMG band within the front end's analog bandwidth; vibration band within the IMU's ODR and low-pass |
| EMG output type | Raw EMG confirmed (else stop; see §4, step 6) |
| Calibration on a wearer | Succeeds; weak or near-zero squeeze rejected; normalized activation tracks squeeze |
| Clipping surfaced | Hard squeeze / strong tap produces `clipped` / `degraded`, once real `raw_limits` are configured |
| Disconnect visible | Unplugged electrode → unavailable or stale status, never stale-looking values; USB unplug → host-clock `stream_stale` within ~0.3 s |
| Vibration within sensor capability | Controlled source changes m/s² as expected; stationary reads near the noise floor; no accelerometer clipping |
| EMG during vibration | Relaxed envelope with vibration on ≈ without (else the hour-12 gate) |
| Restart | Device reboot → `boot_change`, calibration invalidated, recalibration works |
| Backup recording | Real session recorded; replays as `replay` with its `recording_id` |
| Score by design | Same vibration with stronger grip raises the score; presented as the formula's behaviour, **not** validated transmission |

### Fallback gates (from Salaar's plan)

- **Hour 6: EMG acquisition unstable.** Collins and Salaar pair on it: gel electrodes,
  secured cables, drop cosmetic assembly.
- **Hour 12: EMG uninterpretable during vibration.** Demonstrate muscle activation and
  vibration **separately**, and remove the combined-measurement claims. The score is then
  shown only as an algorithm demonstration on recorded or simulated data, or not at all.
  No adaptive cancellation, no custom electrodes.
- **Hour 36:** freeze features and keep a known-good build and recording.

### Known limitations (verified on synthetic data)

- **EMG vibration interference.** An in-band 60 Hz synthetic tone (75 counts) raises the
  relaxed envelope from ~23 to ~59 counts. Nothing removes it.
- **Transport-loss ordering.** The processor's device-clock staleness rule reacts to which
  stream's first post-loss batch arrives first. Around a transport loss, two arrival
  orders can give slightly different invalidations (both conservative) and so slightly
  different scores: about 0.05 s of valid time in the demo. The score never counts time
  its inputs report invalid. Exact order invariance is tested only for loss-free streams
  (`PROCESSING.md`, D18).
- **Missing samples are never imputed.** Frequent nulls keep the EMG and vibration
  filters warming up.
- **No physical score without a conversion.** Without a confirmed accelerometer
  conversion there are no m/s² values and no score; the demo uses the simulator's own
  scale, cited as such.
- **No stale-sensor inference.** A frozen sensor without a device `stale` flag is
  schema-valid; detection depends on firmware.

---

## 6. Remaining choices (distinct from tested facts)

**Tested facts (synthetic inputs only):**
- The contract validator rejects all 22 defect fixtures and accepts all valid and
  simulated streams.
- The simulator is deterministic (byte-identical with a fixed `--run-id`).
- Gaps, reboots, duplicates, stale streams, disconnects, rate mismatch and
  provenance changes invalidate visibly.
- EMG envelope of an in-band sinusoid = A/√2 within 4 %; vibration RMS = A/√2 within 3 %;
  stationary gravity gives < 0.5 counts of dynamic RMS.
- Calibration is reproducible; captures fail across any reset; malformed loaded records
  are rejected (15 variants).
- Score totals and attribution are order-independent within the freshness limits;
  invalid or stale time never accumulates.
- Demo replay matches the original apart from provenance.

**Open choices and assumptions** (defaults in place; change via config, not code):

| Choice | Current default | Decide when / with |
|---|---|---|
| Wire format and link | NDJSON; ≥ 460800 baud or native USB, else binary framing | Board + measured throughput (Collins/Salaar) |
| Batch duration | 50 ms | Link test |
| EMG band | 20–450 Hz (needs ≥ 1000 Hz sampling) | Real rate + analog bandwidth |
| EMG input type | raw EMG assumed | Bring-up step 6 |
| EMG envelope window / settle | 100 ms / 100 ms | First real recordings |
| Vibration high-pass / window | 5 Hz / 1 s; upper edge sensor-limited | IMU ODR and low-pass |
| Accelerometer conversion | none (no m/s², no score) | IMU range from datasheet + config |
| Calibration captures / thresholds | 3 s, discard 0.5 s, ratio ≥ 2, separation ≥ 1 unit | First wearer trials |
| Calibration across reboot | invalidated (`survives_reboot=False`) | Team preference |
| Stale / freshness limits | 0.25 s | Backend live view |
| Score coefficients | k = 1 au/h, v_ref = 1 m/s², exponent 2, weight 1, cap 2 | Demo narrative only; no physical meaning |
| Stimulus command contract | none | With Fahad |
| Prompt 7 firmware skeleton | deferred | When Collins confirms board and sensors |

---

## Appendix: decision log (condensed)

- **D1** Layout: `signals/` only. One authoritative implementation in
  `src/vibeguard_signals/`; `firmware/` deferred.
- **D2** Tooling: uv, project venv; pytest dev-only; stdlib validator; numpy/scipy from
  Prompt 4 (the SciPy 1.18 `butter`/`sosfilt` docs were checked).
- **D3** Ownership:
  - Salaar: contract, simulator, processing, calibration, future controller firmware and
    stimulus commands.
  - Abduljabbar: APIs, adapters, storage, production replay.
  - Fahad: station sequencing.
  - Ali: screens.
- **D4** Device contract v1 is proposed and distinct from the backend API. NDJSON for
  host/simulator; physical wire format open.
- **D5** Device acquires only; host processes.
- **D6** Hand-written validator: cross-field rules in one place.
- **D7** Simulator: stdlib, seeded per boot/stream/purpose; device / simulation / wall time
  kept separate; random identity per run.
- **D8** Sensor faults are valid telemetry; only protocol defects are invalid; flags report
  conditions and are never inferred.
- **D9** No serial transport until the board is confirmed.
- **D10** Processor layers. Conservative resets. Reordered records dropped. Device-reported
  disconnect/stale is sticky until `ok`, so firmware must send status on transitions.
- **D11** Device clock for files and replay; host clock + `poll()` live.
- **D12** First review:
  - rate mismatch is an availability condition;
  - rejections happen before any state mutation;
  - the device clipping flag is honoured;
  - timed host polling;
  - distinct boot IDs;
  - valid low-rate timestamps.
- **D13** EMG pipeline: causal, Nyquist-validated, no imputation; `input_assumption: raw_emg`
  on every output.
- **D14** Calibration:
  - median references; ratio and absolute separation rules;
  - unbounded value plus clamped display and range flags;
  - conservative invalidation;
  - labelled a relative activation proxy.
- **D15** Vibration: gravity separate, unweighted, m/s² only with a cited conversion.
- **D16** Score formula in arbitrary units; invalid or stale time excluded with reasons; a
  provenance change starts a new accumulation; boots never joined.
- **D17** Demo and replay reuse the single implementation; replay rewrites only provenance;
  pacing is content-neutral.
- **D18** Score intervals:
  - an update covers (its previous update, t], at most the freshness limit back;
  - finalisation waits for every required input, or provable staleness;
  - keys name all blocking inputs;
  - order-independent within freshness (tested); not claimed across transport loss.
- **D19** Every EMG reset fails an active capture; loaded calibrations are validated before
  installation.
- **D20** (Prompt 8) The bandwidth estimator's sizing record is relabelled `simulated`, so
  no software path emits `source: "live"`. Estimates rose 200 B/s (now 21,740 / 32,140 /
  45,960 B/s). Prompt 7 is deferred: a firmware skeleton would not unblock integration,
  which already runs through the contract, simulator and API.

## Appendix: progress history

| Step | Result |
|---|---|
| Prompt 0 | Baseline: planning docs only; owned paths agreed |
| Prompt 1 | Contract, 8 valid + 22 invalid fixtures, validator (46 tests) → commit `5637c0e` |
| Prompt 2 | Simulator, 11 scenarios, 2 tracked recordings (86 tests) |
| Prompt 3 | Processing skeleton (121 tests); first review: 6 findings fixed (168) → `79cc417` |
| Prompt 4 | EMG envelope + calibration (219) |
| Prompt 5 | Vibration + experimental score (267); scoring cases mutation-checked (272) |
| Prompt 6 | Demo, replay, embedding example (286); second review: 3 findings fixed (318) → `2bcccc5` |
| Prompt 7 | Deferred until hardware is confirmed |
| Prompt 8 | This handoff; all commands re-verified, including from a fresh clone |
