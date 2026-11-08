# VibeGuard signals — team handoff

**Owner:** Salaar (firmware, signals, calibration, experimental scoring).
**Status (2026-11-07):** the whole host-side signal path is built, tested and runnable
**without hardware**. Everything below was verified on **simulated data only**. Nothing
here has run on the real wearable, so it validates no sensors, electrodes, mounting or
microcontroller timing. It also does not establish clinical relevance, grip force, %MVC
or a standards-compliant vibration dose. No firmware exists yet; it waits for Collins
to confirm the board and sensors.

The existing tattoo-style sleeve needs **no fabrication**: nothing in this plan involves
sewing, conductive fabric or custom electrodes. Gel electrodes go on the skin; the sleeve
only secures parts and routes cables.

**Contents**
1. [Quick start](#1-quick-start-5-minutes)
2. [What we built, in plain terms](#2-what-we-built-in-plain-terms)
3. [Repository map](#3-repository-map)
4. [How each teammate uses this while hardware is being made](#4-how-each-teammate-uses-this-while-hardware-is-being-made)
5. [Output records: quick reference](#5-output-records-quick-reference)
6. [Verified commands](#6-verified-commands)
7. [What only hardware can answer](#7-what-only-hardware-can-answer)
8. [Known limitations](#8-known-limitations)
9. [Open choices and defaults](#9-open-choices-and-defaults)
10. [History and decisions](#10-history-and-decisions)

---

## 1. Quick start (5 minutes)

You need [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11. Run everything from
`signals/`.

```sh
cd signals
uv sync                                   # installs numpy, scipy, pytest into .venv
uv run pytest -q                          # 318 passed
uv run vg-demo run --out out/demo --seed 1 --run-id demo-seed1
uv run vg-demo replay out/demo            # "matches ... apart from provenance: YES"
```

The demo writes a full simulated 30 s session to `out/demo/` (git-ignored):

| File | Use it for |
|---|---|
| `telemetry.ndjson` | What the wearable would send: one device record per line |
| `derived.ndjson` | Everything the processor produced: features, events, score |
| `summary.csv` | One row per 0.5 s; quickest way to see the session |
| `summary.txt` | Human-readable phase table, event timeline, final score |
| `manifest.json` | Seed, config, calibration, SHA-256 of every file |

`summary.txt` shows what happens: calibration, rising muscle activation, vibration on and
off, an injected EMG disconnect, a transport loss, then recovery. The final score is
0.048601 au over 19.695 s of valid time.

---

## 2. What we built, in plain terms

The real system will be: **wearable → serial link → backend → dashboard / station**.
Everything Salaar owns sits on the **wearable → derived numbers** part. Until the
hardware exists, a simulator stands in for the wearable, so downstream integration can proceed against the proposed contract. Real hardware behavior and configuration still require validation.

```
 [vg-sim simulator]  ──or──  [real firmware, later]
          │  device telemetry contract v1 (NDJSON, one record per line)
          ▼
 [contract validator]  rejects malformed records
          ▼
 [Processor]  quality checks → EMG envelope → calibration → vibration → score
          │  derived records: stream_features / stream_event / experimental_score
          ▼
 backend (Abduljabbar) → dashboard (Ali) / station (Fahad)
```

### 2.1 Device telemetry contract v1 (proposed)
**What:** the proposed format for everything the wearable sends. Each line is one JSON
record: `hello` (device description), `status` (device and sensor state), `emg_batch` or
`imu_batch` (samples).

**Why it matters:** the simulator and the future firmware emit the *same* records. Hardware integration requires firmware or an adapter implementing this contract, plus verified acquisition settings. Every record
carries:
- a `seq` number, so lost records are detectable;
- a `boot_id`, so reboots are detectable;
- device timestamps in µs;
- units: raw counts unless a conversion is given;
- quality flags;
- provenance: `live`, `simulated` or `replay`.

Missing values are `null`, never 0.

**Where:** `contract/TELEMETRY_CONTRACT.md` (spec), `src/vibeguard_signals/contract.py`
(the validator), `uv run vg-validate FILE`. Example records:
`contract/fixtures/valid/`. One broken example per error type: `contract/fixtures/invalid/`.

### 2.2 Simulated wearable (`vg-sim`)
**What:** a deterministic fake wearable. It produces synthetic EMG and accelerometer data
in contract format, in real time or as fast as possible.

**Why it matters:** it is the stand-in for the hardware. Its 12 scenarios cover clean data
and every fault type:

| Scenario | Shows |
|---|---|
| `demo` | The scripted 30 s session used by `vg-demo` |
| `happy_path` | Clean run: relaxed, activation, vibration, both |
| `relaxed`, `strong_activation` | EMG levels |
| `stationary_gravity`, `vibration` | Accelerometer at rest versus vibrating |
| `vibration_emg_mixed` | Synthetic vibration leaking into EMG (interference) |
| `clipping` | EMG overdriven into ADC limits |
| `missing_batches` | Records lost in transport |
| `invalid_samples` | Null samples, EMG disconnected, IMU stale |
| `reboot` | Device reboot mid-run |
| `fault_mix` | All of the above in one run |

Output is always labelled `source: simulated`; the simulator can never claim to be live.
The same `--seed` always gives the same samples, and `--run-id` makes files
byte-identical. **Where:** `simulator.py`, `SIMULATOR.md`.

### 2.3 Streaming processor (`Processor`, `vg-process`)
**What:** the host-side brain. It takes contract records in arrival order and produces
derived records. It is a Python library the backend can import, plus a CLI.

**What it handles:**
- record loss, duplicates and late records;
- reboots and timing breaks;
- sample rates that don't match what the device claims;
- disconnected or stale sensors, judged by device clock (files) or host clock (live);
- changes of provenance.

Every problem produces a visible event and makes the affected values `unavailable` until
the stream warms up again. Nothing is silently repaired. **Where:** `processing/`,
`PROCESSING.md`.

### 2.4 EMG envelope
**What:** turns raw EMG into a smooth "how active is the muscle" level, in three causal
steps:
1. remove the DC offset;
2. band-pass 20–450 Hz, checked against the sample rate;
3. take a 100 ms RMS envelope.

Units stay `adc_counts_rms`.

**Assumption:** the EMG module outputs **raw** EMG. If it turns out to output an
already-smoothed envelope, this stage does not apply (see §7). **Where:** `processing/emg.py`.

### 2.5 Calibration and normalized activation
**What:** a per-wearer, two-step calibration.
1. Relaxed capture: 3 s.
2. Squeeze capture: 3 s.
3. Activation is then reported on a scale where relaxed ≈ 0 and squeeze ≈ 1.

A calibration is rejected if squeeze and relaxed are not clearly different
(`insufficient_separation`). It is invalidated automatically by a reboot, a device or
channel change, or a processing-config change. Calibrations can be saved and reloaded.

**Label:** "relative muscle activation proxy". This is **not force and not %MVC.**
**Where:** `processing/calibration.py`.

### 2.6 Vibration features
**What:** the accelerometer signal with gravity separated out, high-passed at 5 Hz. The
output is per-axis and combined RMS: in raw counts always, and in m/s² **only** when a
cited conversion is configured. Unweighted (not ISO 5349). **Where:** `processing/vibration.py`.

### 2.7 Experimental score
**What:** a demo metric combining vibration and activation:

`rate = k · (V / v_ref)^p · (1 + w · clamp(activation, 0, cap))`

Rate is in au/h, and the cumulative total in au (arbitrary units). Time only counts
when both inputs are valid and fresh. Excluded time is reported **with its reason**,
e.g. `activation_uncalibrated & vibration_warming_up`. **It is not a dose, not A(8), and
not a percentage of any limit.** **Where:** `processing/score.py`.

### 2.8 Demo, replay, embedding example
- `vg-demo run` / `vg-demo replay`: the offline end-to-end session from §1. Replay
  re-processes the recording as `provenance: replay` and proves the output is identical.
- `vg-replay FILE --speed X`: re-emits any recording with replay provenance, optionally
  paced in real time. Pacing never changes content.
- `examples/embed_processor.py`: how a backend uses the processor in-process.

**Where:** `demo.py`, `replay.py`, `DEMO.md`.

### 2.9 Bandwidth estimate (`vg-bandwidth`)
**What:** estimates how many bytes per second the contract needs at provisional rates.

**Result:** compact JSON needs about 21.7–46 kB/s, which is **too much for a 115200-baud
UART**. It works at ≥ 460800 baud, over native USB, or with a binary encoding. The
choice waits for the board.

---

## 3. Repository map

```
signals/
├── HANDOFF.md                  ← this file
├── README.md                   short index
├── SIMULATOR.md                simulator scenarios, fault semantics
├── PROCESSING.md               processor, output formats, EMG/calibration/vibration/score detail
├── DEMO.md                     demo, replay, embedding lifecycle
├── pyproject.toml / uv.lock    package + CLI entry points
├── contract/
│   ├── TELEMETRY_CONTRACT.md   device → host contract v1 (proposed)
│   └── fixtures/
│       ├── valid/              one example per record kind + a short stream
│       ├── invalid/            22 broken records, one defect each
│       ├── invalid_manifest.json  expected error code per invalid fixture
│       └── sim/                happy_path_seed1_3s.ndjson, fault_mix_seed1_5s.ndjson
├── examples/embed_processor.py backend embedding example
├── src/vibeguard_signals/
│   ├── contract.py             validator (parse_line, validate_record, serialize_record)
│   ├── simulator.py, sim_cli.py            vg-sim
│   ├── validate_cli.py                     vg-validate
│   ├── process_cli.py                      vg-process
│   ├── replay.py, replay_cli.py            vg-replay
│   ├── demo.py, demo_cli.py                vg-demo
│   ├── bandwidth.py                        vg-bandwidth
│   └── processing/
│       ├── processor.py        Processor: feed / poll / finish / calibration API
│       ├── config.py           ProcessingConfig and per-stage configs
│       ├── window.py, features.py   bounded windows, window statistics
│       ├── emg.py              EMG envelope
│       ├── calibration.py      calibration state machine
│       ├── vibration.py        vibration features
│       ├── score.py            experimental score
│       └── serialize.py        deterministic NDJSON output
└── tests/                      318 tests (pytest)
```

Nothing outside `signals/` is touched by this work. Teammates' planning files are
unchanged.

---

## 4. How each teammate uses this while hardware is being made

The rule for everyone: **build against the contract and the derived records, using
`vg-sim` or the demo files as the device.** When the hardware arrives, only the input
source changes: a serial reader replaces `vg-sim`.

### 4.1 Abduljabbar (backend)

**Get a live-like device stream right now:**

```sh
# wall-clock paced, like a real device, to stdout
uv run vg-sim --scenario fault_mix --duration 60
# or write a file
uv run vg-sim --scenario demo --duration 30 --fast -o sim.ndjson
```

Pipe it into your ingestion, or read it from a subprocess. Static files are also
available: `contract/fixtures/sim/*.ndjson`, `out/demo/telemetry.ndjson`.

**Decode and validate each line:**

```python
from vibeguard_signals import contract
record, issues = contract.parse_line(raw_line)   # bytes or str
if issues:            # invalid → reject the whole record, never use part of it
    ...
```

**Rules for device records:**
- Key on (`device_id`, `boot_id`); a new `boot_id` is a reset.
- Never modify device fields.
- Add your own wall-clock receive time; device `t_us` is µs since boot, not wall time.

**Embed the processor (live mode).** This snippet was run against the happy-path fixture
(247 outputs):

```python
import dataclasses, time
from vibeguard_signals import contract
from vibeguard_signals.demo import demo_processing_config
from vibeguard_signals.processing import Processor, to_ndjson_line

# demo config cites the SIMULATOR's accelerometer scale; swap in the real one later
config = dataclasses.replace(demo_processing_config(), clock="host")
proc = Processor(config)                    # one Processor per device

def on_line(line):                          # call for each received line
    record, issues = contract.parse_line(line)
    outs = (proc.feed_invalid_frame(issues, "adapter") if record is None
            else proc.feed(record, rx_time_s=time.monotonic()))
    publish(outs)

def every_50ms():                           # timer: makes silence become "stale"
    publish(proc.poll(time.monotonic()))

# calibration endpoints → proc.calibration_start("relaxed" | "squeeze") / calibration_cancel()
# persist proc.calibration_record() (JSON); after restart: proc.load_calibration(saved)
# end of session → publish(proc.finish())
```

Full lifecycle notes are in `DEMO.md` (*Embedding the processor*). A complete runnable
version is `uv run python examples/embed_processor.py`.

**Route outputs by `type`:** `stream_features`, `stream_event`, `experimental_score`
(see §5). Each one carries `device_id`, `boot_id`, `provenance`, `timebase` and `t_us`.

**Recording and replay** are yours in production. `vg-replay` shows the required
semantics: only `provenance` changes, to `{source: replay, original_source,
recording_id}`, and accelerated playback never alters timestamps.

### 4.2 Ali (dashboard / screens)

**Data to build against now:**
- `out/demo/derived.ndjson`: real processor output for the whole demo session, including
  calibration, faults and recovery. Replay it into your UI to see every state.
- `out/demo/summary.csv`: the same session flattened to 0.5 s rows, handy for
  prototyping charts.
- For other UI states, process another scenario:
  `uv run vg-sim --scenario reboot --duration 20 --fast | uv run vg-process - -o reboot.ndjson`.
  Swap in `clipping`, `invalid_samples` or `missing_batches` for the other states.

**Where each displayed value lives:**

| Show | Field | Label it |
|---|---|---|
| Vibration | `stream_features` (stream `imu`) → `features.vibration.physical.combined_rms` (m/s²) | "measured vibration, unweighted" |
| Muscle activation | `stream_features` (stream `emg`) → `features.emg_activation.normalized.display.emg0` (0–1 for bars); raw value in `.value` | "relative activation proxy" (not force) |
| Calibration state | `features.emg_activation.calibration.state` and `stream_event` with `event: calibration_state` | `uncalibrated` / `capturing_relaxed` / `relaxed_captured` / `capturing_squeeze` / `calibrated` / `failed` / `invalidated` (+ `reason`) |
| Experimental score | `experimental_score` → `rate.value` (au/h), `cumulative.value` (au), `status`, `reasons`, `excluded_by_reason` | "experimental score, arbitrary units" |
| Data source | `provenance.source` on every record | `simulated` / `replay` / `live` |

**Display rules:**
1. **Status before numbers.** Every feature has `status`: `ok`, `degraded` (usable, with
   `reasons`), `warming_up` or `unavailable`. The score has `accumulating` or `paused`.
2. **`null` means unavailable.** Never render it as 0. Never keep showing an old value
   after an `unavailable` record.
3. **Keep the three quantities separate.** Vibration, activation and score are different
   things; don't merge them into one gauge.
4. **Always show provenance.** Simulated and replay data must be visibly labelled on every
   view.
5. **Show events as messages:** `boot_change`, `seq_gap`, `sample_gap`, `stream_stale`,
   `stream_recovered`, `device_sensor_state`, `end_of_input`. Show calibration failures
   too, e.g. `insufficient_separation`, which means "squeeze harder and retry".

### 4.3 Fahad (station test engine)

- Nothing station-specific is implemented here, and the station engine is yours.
- **Mock now:** the stimulus command contract (deliver stimulus, abort, acknowledgement)
  is not agreed yet. Define the interface you need and send it to Salaar; the
  controller-side implementation is firmware, which waits for the board.
- Report motor settings as **settings**, not as measured amplitudes. Measured vibration
  comes from the accelerometer path (§2.6).
- For simulated "response" data during a trial, use `vg-sim --scenario vibration` or
  `strong_activation`, or process those scenarios as in §4.2.

### 4.4 Collins (wiring / hardware)

These facts are needed from Collins. Every one of them is currently `null` / unknown in
the code.

1. **Controller:** board model and revision, how many controllers, USB type (native USB
   CDC, or a UART bridge plus its maximum baud).
2. **EMG module:**
   - model;
   - **does it output raw EMG or a rectified/smoothed envelope?** (the most important one);
   - output pin;
   - voltage range;
   - analog bandwidth;
   - supply.
3. **ADC:** resolution, reference voltage, input range.
4. **IMU:**
   - model;
   - bus and address;
   - configured ±g range;
   - output data rate;
   - internal low-pass setting;
   - FIFO / data-ready support.
5. **Wiring and power:**
   - pin map;
   - grounding;
   - electrode and reference placement;
   - where the IMU sits on the sleeve and how rigidly it is mounted.
6. **Link throughput:** can it sustain about 32 kB/s (`uv run vg-bandwidth`)?
7. **Local mains frequency** (50/60 Hz) and the controlled vibration source
   (frequency, amplitude).

**First bring-up sequence.** Each step uses a check that already exists:

1. Identify the board and record facts 1–4.
2. **Collins checks wiring and electrical compatibility against the manufacturers'
   guidance before anything touches the wearer.** This covers logic levels, and EMG
   output range versus ADC input range.
3. Salaar compiles a minimal acquisition firmware for that board. It emits contract
   records from fake data first, then from real sensors.
4. Record ≥ 60 s, then run `uv run vg-validate rec.ndjson` and `uv run vg-process rec.ndjson`.
   Check:
   - the measured rate matches the nominal rate;
   - there is no `rate_mismatch` and no `timing_discontinuity`;
   - gap counts are ≈ 0.
5. The backend swaps `vg-sim` for the serial reader.
6. Inspect real EMG.
   - **Raw EMG swings both ways around mid-scale; an envelope is one-sided and slow.** If
     it is an envelope, stop and adapt the pipeline.
   - Check for mains hum, clipping at a hard squeeze, a stationary IMU reading gravity
     only, and an unplugged lead giving a visible fault rather than silent zeros.
7. Calibrate a wearer. A weak squeeze must fail with `insufficient_separation`.
8. Switch on the vibration source.
   - Vibration should rise in m/s² with no accelerometer clipping.
   - **Check whether the relaxed EMG rises from vibration alone** (interference, §8).
9. End-to-end dashboard test with `live`, `replay` and `simulated` labelled.

---

## 5. Output records: quick reference

All derived records have `schema: "vibeguard.features"`, `version: 1` (proposed), plus
`device_id`, `boot_id`, `provenance`, `timebase: "device_monotonic_us"`, `t_us` and
`source_seq`. The full format is in `PROCESSING.md`. The examples below are trimmed from
the real `out/demo/derived.ndjson`.

**`stream_event`**: something happened.
```json
{"type":"stream_event","event":"calibration_state","stream":"emg","t_us":504000,
 "detail":{"state":"capturing_relaxed","reason":null,"phase":"relaxed"}, ...}
```

**`stream_features` (emg)** → `features.emg_activation`
```json
{"label":"relative_muscle_activation_proxy","input_assumption":"raw_emg",
 "envelope":{"status":"ok","value":{"emg0":321.3498},"units":{"emg0":"adc_counts_rms"}},
 "normalized":{"status":"ok","value":{"emg0":1.1004},"display":{"emg0":1.0},
               "above_reference":{"emg0":true},"below_relaxed":{"emg0":false}},
 "calibration":{"state":"calibrated","calibration_id":"cal-3341827795b8"}}
```

**`stream_features` (imu)** → `features.vibration`
```json
{"label":"measured_vibration_within_sensor_bandwidth",
 "weighting":"none (unweighted; not ISO 5349 frequency-weighted)",
 "status":"ok","static_mean":{"ax":-0.05,"ay":0.1,"az":4096.22},
 "combined_rms":1448.35,
 "physical":{"status":"ok","units":"m/s^2","combined_rms":3.4676,
             "conversion_source":"simulator synthetic scale 4096 counts per g (SIMULATED; ...)"}}
```

**`experimental_score`**
```json
{"type":"experimental_score","status":"accumulating","reasons":[],
 "rate":{"value":12.021476,"units":"au/h"},"cumulative":{"value":0.004945,"units":"au"},
 "valid_duration_s":5.4485,"excluded_duration_s":7.4515,
 "excluded_by_reason":{"activation_capturing_squeeze":3.0,
                       "activation_uncalibrated & vibration_warming_up":0.45, ...},
 "inputs":{"vibration":{"status":"ok","value":3.4672,"units":"m/s^2","age_s":0.051},
           "activation":{"status":"ok","value":-0.0017,"units":"normalized","age_s":0.0}}}
```

---

## 6. Verified commands

All of these were re-run for this handoff, from `signals/`. The first block was also run
from a fresh `git clone`, which reproduced the demo byte-for-byte.

| Command | Verified result |
|---|---|
| `uv sync` | exit 0 |
| `uv run pytest -q` | **318 passed**: contract 46, simulator 41, processing 35, review fixes 47, EMG 51, vibration/score 53, demo 13, second review 32 |
| `uv run vg-demo run --out out/demo --seed 1 --run-id demo-seed1` | 1216 telemetry / 2390 derived records; 0.048601 au over 19.695 s valid |
| `uv run vg-demo replay out/demo` | matches apart from provenance: YES |
| `uv run vg-validate -q contract/fixtures/valid/* contract/fixtures/sim/*` | 325/325 valid |
| `uv run vg-validate -q contract/fixtures/invalid/*.json` | 0/22 valid, exit 1 (intended) |
| `uv run vg-sim --scenario fault_mix --seed 1 --duration 10 --fast \| uv run vg-validate -` | 379/379 valid |
| `uv run vg-sim --scenario fault_mix --duration 10 --fast \| uv run vg-process -` | 379/379 accepted |
| `uv run vg-sim --scenario happy_path --duration 3 \| uv run vg-process --clock host -` | 124/124 accepted |
| `uv run vg-replay out/demo/telemetry.ndjson --speed 10 \| uv run vg-process -` | 1216/1216 accepted, about 3 s |
| `uv run python examples/embed_processor.py` | 0.048601 au; calibration persisted and reloaded |
| host-clock embedding snippet (§4.1) on `happy_path_seed1_3s.ndjson` | 247 outputs |
| `uv run vg-bandwidth` / `uv run vg-sim --list` | exit 0 / 12 scenarios |

---

## 7. What only hardware can answer

| Check | Pass condition |
|---|---|
| Sample rate | Measured rate within ~1 % of nominal over 10 min; jitter recorded |
| Dropped samples | `sample_gap` / `overrun` / `seq_gap` ≈ 0 at the chosen link settings |
| Filters fit | No `filter_config_invalid`; EMG band within the module's bandwidth; vibration band within the IMU's ODR and low-pass |
| EMG output type | Raw EMG confirmed; otherwise stop and adapt |
| Calibration on a wearer | Succeeds; a weak squeeze is rejected; activation tracks the squeeze |
| Clipping visible | A hard squeeze or tap shows `clipped` / `degraded` once real `raw_limits` are set |
| Disconnect visible | An unplugged lead gives an unavailable or stale status; a USB unplug gives `stream_stale` within ~0.3 s (host clock) |
| Vibration | The controlled source changes m/s² as expected; at rest it reads near the noise floor; no clipping |
| EMG during vibration | Relaxed envelope with vibration on ≈ the same as without (else the hour-12 gate) |
| Reboot | `boot_change`, calibration invalidated, recalibration works |
| Backup recording | A real session recorded, and replays as `replay` |
| Score behaviour | Same vibration with a stronger grip gives a higher score. That is the formula's behaviour by design, **not** validated transmission |

**Fallback gates:**
- **Hour 6** (EMG unstable): Collins and Salaar pair on it with gel electrodes, secured
  cables, and the cosmetic assembly dropped.
- **Hour 12** (EMG uninterpretable during vibration): show activation and vibration
  **separately**, and drop the combined claims. The score becomes an algorithm demo on
  recorded or simulated data only.
- **Hour 36:** freeze features; keep a known-good build and recording.

---

## 8. Known limitations

These are verified on synthetic data.

- **EMG vibration interference.** An in-band synthetic 60 Hz tone raises the relaxed
  envelope from ~23 to ~59 counts, and nothing removes it. There is no notch or adaptive
  cancellation, by design. See `vg-sim --scenario vibration_emg_mixed`.
- **Transport-loss ordering.** Around a transport loss, the processor's device-clock
  staleness rule reacts to which stream's first post-loss batch arrives first. Two arrival
  orders can therefore give slightly different, both conservative, invalidations and
  scores: about 0.05 s of valid time in the demo. The score never counts time its inputs
  report as invalid. Exact order independence is tested for loss-free streams only.
- **No imputation.** Missing samples stay missing; frequent nulls keep filters warming up.
- **No conversion, no physical score.** Without a confirmed accelerometer conversion there
  are no m/s² values and no score. The demo cites the simulator's own scale.
- **No stale inference.** A frozen sensor without a device `stale` flag looks valid;
  detecting it depends on the firmware.
- **The pipeline assumes raw EMG.** A pre-smoothed envelope pipeline was not built
  speculatively.

---

## 9. Open choices and defaults

All of these are config values, not code changes.

| Choice | Current default | Decide when / with |
|---|---|---|
| Wire format and link | NDJSON; ≥ 460800 baud or native USB, else binary | Board + throughput test (Collins/Salaar) |
| Batch duration | 50 ms | Link test |
| EMG band | 20–450 Hz (needs ≥ 1000 Hz sampling) | Real rate and module bandwidth |
| EMG envelope window | 100 ms | First real recordings |
| Vibration high-pass / window | 5 Hz / 1 s | IMU ODR and low-pass |
| Accelerometer conversion | none → no m/s², no score | IMU range from the datasheet |
| Calibration capture / thresholds | 3 s capture, ratio ≥ 2, separation ≥ 1 | First wearer trials |
| Calibration across reboot | invalidated | Team preference |
| Stale / freshness limits | 0.25 s | Backend live view (Abduljabbar) |
| Score coefficients | k = 1, v_ref = 1 m/s², p = 2, w = 1, cap = 2 | Demo narrative only; no physical meaning |
| Backend record wrapping, receive-time field | not decided | Abduljabbar |
| Stimulus command contract | none | Fahad |
| Firmware (Prompt 7) | deferred | When Collins confirms board and sensors |

---

## 10. History and decisions

| Step | Result | Commit |
|---|---|---|
| Prompt 1 | Contract v1, fixtures, validator | `5637c0e` |
| Prompts 2–3 | Simulator (scenarios, faults); processor skeleton; first review (6 fixes) | `79cc417` |
| Prompts 4–6 | EMG envelope, calibration, vibration, score, demo/replay; second review (3 fixes) | `2bcccc5` |
| Prompt 7 | **Deferred**: a firmware skeleton would not unblock anyone before the board is known | — |
| Prompt 8 | Handoff; all commands re-verified incl. fresh clone; bandwidth sizing record relabelled `simulated` | `174f0ba` + this file |

**Key decisions:**
- **Scope.** Only `signals/` is touched. There is one implementation, in
  `src/vibeguard_signals/`.
- **Processing location.** The device only acquires; all processing runs on the host.
- **Contract.** The device contract is separate from the backend API. Its physical wire
  format stays open until the board is known.
- **Faults versus defects.** Sensor faults are valid telemetry; only protocol defects make
  a record invalid. Flags report conditions and are never inferred.
- **Resets.** They are conservative and visible, never silently repaired. Duplicate and
  late records are rejected before any state changes.
- **Clocks.** Device clock for files and replay (deterministic); host clock with `poll()`
  for live use.
- **Labelling.** No software path emits `source: live`. Simulated data is always labelled.
- **Score accounting.** Time counts only when both inputs cover it. Excluded time names
  every blocking input.
- **Replay.** It changes only provenance, and pacing never changes content.

## Ready-to-use team mock

See [MOCK_INTEGRATION.md](MOCK_INTEGRATION.md) for generated JSON endpoints, browser playback, backend integration and provisional sensor assumptions.

## Confirmed inventory update (2026-11-07)

See [HARDWARE_INVENTORY.md](HARDWARE_INVENTORY.md). BioAmp EXG Pill ×1, ESP32 V2 ×2 and two IMUs marked MPU-9250/6500 are confirmed. Use one ESP32/IMU pair per wearable and station. Arduino hardware is optional. Motor driver boards remain missing. Exact ESP32 variant, populated IMU chip identity and configured acquisition settings still need confirmation; mock scaling remains provisional.
