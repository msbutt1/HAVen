# vg-sim — deterministic simulated wearable (SIMULATED DATA ONLY)

Emits device telemetry contract v1 NDJSON (`contract/TELEMETRY_CONTRACT.md`). Every
record has `provenance.source = "simulated"` and the run's `recording_id`. The simulator
cannot emit `live`.

The signals are **synthetic surrogates** for exercising the pipeline:
- EMG is Gaussian noise around a synthetic ADC midpoint, with its spread set by a
  simulator "activation" knob.
- The IMU signal is a gravity vector, plus an optional sinusoidal vibration and noise.

Neither is a physiological, electrode, or sensor model. Fixture output proves nothing
about real sensor performance.

## Commands (run from `signals/`)

```sh
uv run vg-sim --list                                   # scenarios
uv run vg-sim --scenario happy_path --seed 1 --duration 10            # wall-paced, stdout
uv run vg-sim --scenario fault_mix --seed 1 --duration 10 --fast -o run.ndjson
uv run vg-sim --scenario happy_path --duration 60 --speed 4           # 4x wall pacing
uv run vg-sim --scenario fault_mix --fast | uv run vg-validate -      # pipe into validator
uv run vg-sim ... --summary-json summary.json          # counts of injected faults
```

Telemetry goes to stdout (or `-o FILE`); diagnostics go to stderr. Runs are finite
(`--duration`, max 3600 s), and the process exits by itself.

## Configuration

`--seed`, `--duration`, `--emg-rate` (default 1000 Hz), `--imu-rate` (default 400 Hz),
`--batch-ms` (default 50), `--device-id`, `--vibration-hz`. The rates are **synthetic
choices, not hardware facts**; each stream is configured independently. Synthetic tones
must stay below 45 % of the relevant sample rate, or the config is rejected to prevent
aliasing. Seed, scenario, full config, and declared fault windows are recorded in each
`hello` record's `provenance.detail`.

Simulator-only constants (synthetic, not assumptions about real parts):
- EMG ADC range 0–4095, midpoint 2048;
- IMU range ±32768 counts, 4096 counts per g;
- IMU timestamp jitter ±0.5 %. The first IMU sample is offset by the maximum jitter, so
  timestamps stay ≥ 5 ms after boot at every supported rate (tested down to 0.1 Hz).
  Unsupported configs (rate ≤ 0 or > 20 kHz, aliasing tones, bad duration or run ID) are
  rejected before any output, with exit code 2.

Values stay in raw counts with no `conversion`, matching what real firmware must do until
conversions are known.

## Identity versus reproducibility

By default every run gets a **fresh random identity**, `recording_id = sim-<random>` and
`boot_id = sim-<random>-b<n>`, just as a real device gets a new `boot_id` per boot. A
restarted simulator is therefore never mistaken for duplicates of an earlier run. The
`hello` reports `identity: "random_per_run"` and a `config_digest` (a hash of config and
generator version), so runs with equal sample content can be matched.

`--run-id ID` (or `Simulation(config, run_id=ID)`) fixes the identity, for reproducible
fixtures only. Two runs with the same `--run-id` deliberately reuse boot IDs, so don't
feed both to the same persistent consumer.

## Determinism

The same seed and config produce identical sample values and device timestamps. With a
fixed `--run-id`, the output is byte-identical. Each (boot, stream, purpose) has
its own seeded RNG, and every sample draws the same random numbers whether or not a
fault affects it. As a result:
- changing the IMU rate does not change EMG values;
- fault windows do not shift unaffected samples.

Changing the seed changes values and IMU jitter, so the interleaving of EMG and IMU
records can change too. Per-stream batch structure does not.

## Three separate clocks

| Clock | Where | Behaviour |
|---|---|---|
| Device time | `t0_us` / `t_us` in records | µs since the current simulated boot; restarts at reboot |
| Simulation time | phase/fault windows in `declared_faults` (`start_s`/`end_s`) | seconds since run start, continuous across reboots (a reboot spends 0.3 s with no output) |
| Wall time | CLI pacing only | `--speed`/`--fast` never change record contents |

## Scenarios

| Name | What it produces |
|---|---|
| `happy_path` | Relaxed → stronger activation → vibration → both → relaxed. No fault flags, no loss. |
| `relaxed` | Relaxed EMG, stationary IMU. |
| `strong_activation` | Relaxed / stronger / relaxed EMG. |
| `stationary_gravity` | Tilted gravity vector, no vibration. |
| `vibration` | 0.5 g, 60 Hz synthetic sinusoid in the middle half. |
| `vibration_emg_mixed` | The vibration also leaks into EMG as an additive tone (150 counts/g): **synthetic interference**, not a physiological model. |
| `clipping` | EMG overdriven into the synthetic ADC rails → `clipped`. |
| `missing_batches` | Records lost in simulated transport → gaps in `seq` (never `hello`). |
| `invalid_samples` | Sporadic `null` samples (`missing_values`); EMG disconnected (all-null batches, `sensor_disconnected`, status `disconnected`); IMU stale (repeated values, `stale`). |
| `reboot` | New `boot_id`; `seq`, sample index, and device time restart; a new `hello` is sent. |
| `fault_mix` | All of the above, plus device-side `overrun` (`first_sample_index` jump = `dropped_samples_before`). |

Sensor faults are **schema-valid telemetry**: every scenario passes `vg-validate`.
Deliberately malformed protocol records live separately in `contract/fixtures/invalid/`.
`sensor_disconnected` and `stale` mark conditions the scenario **injects**, standing in for
firmware detection. The validator cannot infer them from values.

Emission order: one `seq` counter per boot covers all record types, in the order records
are emitted. A batch is emitted when its last sample is complete. Status records go out
every 1 s and at each disconnect/stale transition, with the device's dropped-sample
counters.

## Tracked recordings

`contract/fixtures/sim/`; tests check that both regenerate byte for byte:

```sh
uv run vg-sim --scenario happy_path --seed 1 --duration 3 --fast --run-id fixture-happy-seed1 -o contract/fixtures/sim/happy_path_seed1_3s.ndjson
uv run vg-sim --scenario fault_mix  --seed 1 --duration 5 --fast --run-id fixture-fault-seed1 -o contract/fixtures/sim/fault_mix_seed1_5s.ndjson
```

A behaviour change in the simulator that alters output requires regenerating these
recordings and noting it in `HANDOFF.md`.

## Not implemented

There is no serial or physical transport: NDJSON over stdout/file is the host
simulation format. Hardware transport waits for board confirmation.
