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

## Progress

| Step | State |
|---|---|
| Prompt 0 baseline | Done. Repo had only planning docs; no conflicts. |
| Prompt 1 contract | Done. Spec, 8 valid + 22 invalid fixtures + 1 NDJSON stream, validator, CLI, 46 passing tests, bandwidth estimate. |
| Prompts 2–8 | Not started. |

## For Abduljabbar (ingestion) and Ali (display)

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

**Ali specifically:** these records are raw device data, not display values. Normalized
muscle activation, vibration features, and the experimental score come from later host
processing steps, which will publish their own example outputs.

**Decisions needing alignment**
1. Whether the backend accepts this record shape as-is or wraps it (it should not mutate
   the device fields).
2. Host freshness/stale limits for the live view (proposed in the Prompt 3 processing step).
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
