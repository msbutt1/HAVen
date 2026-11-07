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
| Prompts 4–8 | Not started. |

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

## Unresolved hardware assumptions (all explicitly `null` / provisional)

- Controller/board model and revision, and controller count.
- EMG front-end model, output range, ADC resolution/reference → `raw_limits`, mV conversion.
- IMU model, bus/address, range, ODR → `raw_counts` → m/s² conversion, `nominal_rate_hz`.
- Actual sample rates and jitter (measured from timestamps, never assumed).
- Serial link type (UART baud vs native USB) and measured throughput.
- Whether the device can detect EMG disconnection or IMU staleness at all.
