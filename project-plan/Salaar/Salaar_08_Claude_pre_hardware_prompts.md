# Salaar: pre-hardware build plan and Claude prompts

## What can start now

Yes. Salaar's seven planning files explicitly allow synthetic/recorded inputs while Collins wires the wearable. Prepare the interfaces, simulator, signal-processing reference implementation, calibration logic, quality flags, and portable acquisition skeleton now. This lets Abduljabbar ingest data and Ali build live views before sensors arrive.

The repository currently contains planning documents and a ZIP archive, with no application implementation. Reinspect it when executing these prompts because teammates may have added code. The existing tattoo sleeve needs no sewing work.

| Ready before hardware | Requires actual hardware |
|---|---|
| Proposed telemetry contract and example messages | Confirmed board, ADC characteristics, pins, and driver libraries |
| Deterministic sensor simulator and malformed-stream fixtures | Actual sampling rate, timing jitter, and sensor bandwidth |
| Host-side EMG envelope and vibration processing | Electrical compatibility, physical noise, mounting, and clipping limits |
| Calibration state machine and invalid-input behavior | Calibration on a wearer and reliability during vibration |
| Experimental score with documented assumptions | Any validation against physical measurements |
| Portable firmware interfaces and fake acquisition | Board compilation, flashing, and sensor acquisition |
| Backend handoff fixtures and offline replay | Full live end-to-end acceptance |

## How to use this file

Open Claude Code at `/home/salaar/nathacks`. Paste Prompt 0, then execute the numbered prompts individually. Let each finish and inspect its reported checks before continuing. These prompts request implementation, not another plan. Stop after the requested step rather than asking Claude to build all future steps at once.

In a fresh Claude conversation, paste Prompt 0 again, then the desired step. Prompt 0 establishes the shared constraints for every step.

A practical pre-hardware budget is about 6–9 focused hours, adjusted to the team's remaining time. Prioritize Prompts 1–3 to unblock others. Prompts 4–6 establish Salaar's processing work; Prompt 7 is optional if firmware choices remain uncertain. Prompt 8 packages whatever actually works. These estimates are planning allowances, not guaranteed runtimes.

## Prompt 0 — Load context and establish ownership

```text
You are implementing Salaar's pre-hardware work for HAVen in /home/salaar/nathacks.

Read the seven original Markdown files in project-plan/Salaar (01 through 07), applicable repository instructions, and any existing source/configuration. Inspect the working tree without disturbing teammates' edits. This file is the execution guide, not an instruction to run every step now.

Salaar owns firmware, signals, calibration algorithms, and experimental scoring, with limited hardware help. Collins owns wiring; Abduljabbar owns backend ingestion/storage/APIs; Ali owns frontend; Fahad owns the station test engine. Keep their implementations outside this task. Share adapters, fixtures, and documented interfaces with them.

Hardware models are unconfirmed. Keep unknown pins, ADC ranges, actual sample rates, IMU models, and motor characteristics explicitly unresolved. Synthetic data must always be marked simulated. This prototype does not establish clinical accuracy, calibrated grip force, or standards-compliant vibration dose. Use normalized muscle activation rather than validated force or %MVC unless the calibration protocol supports that label.

Use existing repository conventions. If there is no application code, favor a small Python host-side reference package for signal processing and simulation, plus a separate portable firmware area only when needed. Avoid introducing a frontend, backend server, database, or cloud stack. Keep one processing implementation authoritative; do not duplicate it on-device prematurely.

For every subsequent prompt: implement only its scope, run relevant checks, report exact runnable commands, changed files, actual test results, and remaining hardware assumptions. Mark checks not run honestly. Verify current official documentation when selecting new dependencies or hardware SDK APIs. Record decisions and progress in a concise Salaar-owned handoff document, not in teammates' planning files.

For this step only, report the repository baseline, proposed owned paths, and any existing interface conflicts. Do not implement later steps yet.
```

**Done when:** Claude has inspected the actual repo and identified where Salaar can work without colliding with teammates.

## Prompt 1 — Define the smallest usable device contract

```text
Implement a proposed versioned telemetry contract for Salaar's device simulator and acquisition path. Reuse an existing team contract if present. Otherwise create a small contract document, example fixtures, and a validator in Salaar-owned paths; mark the contract proposed pending teammate alignment.

Define separate EMG and IMU sample batches so differing sample rates do not require fabricated one-to-one samples. Include protocol version, device ID, boot/stream ID, sequence number, device-monotonic start time, sample interval or explicit timestamps, sample rate, units, quality flags, and provenance. State sequence-number scope and reboot/reset behavior. Raw ADC counts must stay counts until an explicit conversion is available. Use acceleration units only when the conversion is known; otherwise retain raw IMU counts.

Use a simple framed format such as newline-delimited JSON for the host simulator unless the team already selected another format. Bound record/batch sizes. Distinguish simulator transport from an eventual bandwidth-appropriate physical wire format. Estimate bytes/second at provisional sample rates before assuming verbose JSON fits a serial link.

Include example valid EMG and IMU batches plus invalid version, malformed values, timestamp inconsistency, and missing-field cases. Reject nonfinite values. Define how dropouts and stale readings are represented without silently replacing them with zero.

Document the processing boundary: device acquisition first, host reference processing initially. Keep backend APIs and station behavior owned by their teammates. Add a short handoff for Abduljabbar and Ali explaining how to consume the examples and which decisions need alignment.

Completion: fixtures validate, invalid fixtures fail clearly, and the contract has enough detail for another person to consume a stream without asking how timestamps, units, or provenance work.
```

**Handoff now:** Share contract/examples with Abduljabbar and Ali. Keep developing against the proposed adapter while they review; avoid independently changing an existing team contract.

## Prompt 2 — Build a deterministic simulated wearable

```text
Implement a CLI simulated wearable emitting the Prompt 1 contract. Use deterministic seeded generation and a finite run duration. Keep machine-readable telemetry on stdout and diagnostics on stderr. Support an optional output file and a fast non-real-time mode for tests.

Provide named scenarios: relaxed muscle activity, stronger activation, stationary IMU with gravity, controlled synthetic vibration, clipping, missing batches, invalid samples, and reboot/reset. Include a mixed scenario where vibration contaminates EMG, clearly described as synthetic interference rather than a realistic physiological model.

Keep EMG and IMU sample rates independently configurable and explicitly synthetic. Report the seed and configuration in run metadata. Provide at least one clean happy-path recording and one fault recording. Never emit simulated samples labeled live, and never imply that a software fixture proves real sensor performance.

Completion: the same seed/configuration yields the same sample data; output validates against the contract; fault cases produce the declared faults; a short run finishes by itself. Document exact commands Abduljabbar can run immediately to obtain a valid stream or fixture.
```

**Handoff now:** Abduljabbar can integrate ingestion; Ali can consume representative derived/mock values through the agreed backend interface.

## Prompt 3 — Build the host processing skeleton and quality handling

```text
Implement a small host-side streaming processing API consuming the contract and exposing derived feature records. Keep transport, algorithms, and output serialization separate. This is a library/CLI for integration, not Abduljabbar's backend service.

Handle EMG and IMU independently, with bounded buffers and time-based windows. Surface gaps, reordering, duplicate batches, reset/boot changes, clipping when configured ADC limits are known, and stale inputs. Specify reset and warm-up behavior. Do not carry old valid-looking feature values through a disconnect. Preserve provenance, device identity, and timebase in every output.

Create placeholder feature outputs with explicit unavailable status until an algorithm has enough valid data. Keep unavailable distinct from zero. Introduce a small configuration object for sample rates, windows, raw limits, and conversion metadata; unknown hardware values must remain explicit.

Completion: feed the happy-path and fault fixtures through the API and prove bounded buffering, clear invalidation on gaps/reset, and deterministic output. Include a smoke-test command that runs without any hardware or backend.
```

## Prompt 4 — Implement the EMG envelope and calibration

```text
Add causal streaming EMG processing to the host reference package. Use a justified minimal pipeline: configurable offset/baseline handling, filtering appropriate to the configured sample rate, and a roughly 100 ms RMS envelope. Make filter configuration explicit and validate cutoff relationships against Nyquist with practical headroom. Do not hardcode the brief's 20–450 Hz band onto a 500 Hz sampling configuration. Avoid acausal offline filtering in the live path.

Implement relaxed/squeeze calibration as a state machine with configurable timed capture windows, sample-quality checks, and rejection of insufficient data or near-zero relaxed-to-squeeze separation. Define how the reference levels are estimated and how normalized activation is calculated. Record calibration identity/configuration and invalidate incompatible calibration after relevant configuration changes.

Report normalized activation, raw envelope units, calibration state, and quality status. A convenience display may be bounded, but preserve an explicit over-reference flag instead of silently concealing activation above the calibration reference. Label this a relative muscle-activation proxy, not measured grip force.

Test known synthetic amplitudes within tolerance, DC offset handling, filter startup, invalid calibration, clipping/gaps, and repeatable reset behavior. State that these tests verify algorithm behavior only; actual electrode quality and vibration artifacts remain hardware checks.

Completion: the simulator shows relaxed and stronger activation distinctly, successful calibration produces reproducible normalized output, and invalid calibration fails visibly rather than dividing by a near-zero number.
```

## Prompt 5 — Implement vibration features and experimental scoring

```text
Add streaming vibration features using the independently timed IMU stream. Keep raw acceleration, gravity/DC treatment, dynamic per-axis RMS, and combined RMS clearly distinguished. A stationary synthetic gravity vector should not be displayed as tool vibration after the defined warm-up. Describe the passband and limitations; implement no claimed ISO frequency weighting without validated inputs and a justified method.

Compute physical units only when conversion metadata exists. Otherwise emit a clearly labeled raw-unit feature and mark physical-unit metrics unavailable.

Add an explicitly experimental score rate and cumulative score with a short documented formula and configurable coefficients. Separate the measured vibration feature, normalized muscle activation, and combined score in the output. Give the score honest arbitrary units when its scaling is arbitrary. Do not label it A(8), percent daily legal limit, or validated dose.

Align asynchronous feature streams using explicit freshness limits. Integrate by elapsed valid time, not update count. Exclude gaps, stale muscle activation, invalid calibration, and reset intervals; expose excluded duration and current validity. Duplicates and out-of-order events must not double-count or create negative integration time. Keep simulation/replay time distinct from wall time.

Test analytical sinusoid RMS, stationary input, missing units, identical-duration signals at different update rates, invalid intervals, and the declared influence of activation on the score. Explain that the activation-weighting test validates the formula rather than real vibration transmission.

Completion: known-input tests pass and derived records expose rate, accumulation, units, provenance, and quality without silently imputing missing measurements.
```

## Prompt 6 — Deliver an offline end-to-end reference demo

```text
Connect the existing simulated wearable, processing API, calibration flow, and feature serialization into one documented local demo command. Reuse the fixtures and algorithms; avoid a second independent implementation.

Provide a finite scripted run demonstrating calibration, stronger muscle activity, vibration changes, an invalid-data interval, recovery, and saved derived output. Clearly mark all data simulated. Provide a replay command that preserves recording time and source identity, with any accelerated playback declared separately.

Generate a compact inspectable summary (text or CSV is sufficient) showing activation, vibration, experimental score rate/accumulation, and quality changes. Keep UI and database work with Ali and Abduljabbar.

Add an integration example using the processing API directly so Abduljabbar can embed it without spawning the demo CLI. Document input/output examples and lifecycle/reset requirements. Run the existing tests plus one complete offline smoke run.

Completion: another teammate can follow the documented commands from the repository to reproduce the simulation and see gaps handled correctly. Report actual outputs and test results, not just intended behavior.
```

## Prompt 7 — Optional: prepare portable firmware boundaries

```text
Prepare only the firmware work that is useful before board identification. Inspect existing firmware first. Define small acquisition interfaces for clock, EMG input, IMU input, and telemetry output. If a native compiler is available, implement a minimal portable C++ acquisition core with fake sensors and a host smoke test; otherwise provide a narrowly scoped skeleton and honestly mark it uncompiled.

Keep sample capture independent from telemetry formatting/output. Use bounded buffering and a documented overflow/drop policy. Allow independent EMG and IMU scheduling. Record observed capture timing and overrun counts. Do not claim a host timing test demonstrates microcontroller timing.

Leave board-specific adapters explicit and unimplemented until Collins confirms models. Avoid invented pinouts, ADC voltage ranges, assumed sensor libraries, or speculative board build configurations. Keep the working signal-processing reference on the host; do not port it merely to fill the firmware skeleton.

Document the hardware facts needed next: controller/board revision, ADC reference/resolution, EMG output range, IMU model/bus/address/range/ODR, actual connection/power arrangement, controller count, and serial throughput budget. If station command hooks are necessary, agree their boundary with Fahad; do not implement his staircase engine.

Completion: fake acquisition demonstrates sequencing and bounded-buffer overflow behavior, or the skeleton explicitly lists the untested pieces. State that board compilation and flashing are deferred.
```

**Skip this prompt** if it would create throwaway abstractions before an imminent board confirmation. The working simulator and host reference are the higher-value deliverables.

## Prompt 8 — Review and package the hardware handoff

```text
Review only Salaar-owned work completed so far against project-plan/Salaar files 01–07. Resolve correctness problems in timestamps, units, provenance, calibration, invalid-data handling, and cumulative integration. Run relevant checks once fixes are complete; avoid broad refactors or new features.

Write one concise handoff document with:
1. Exact setup, simulator, test, processing, and replay commands that were actually verified.
2. Completed components and explicit unimplemented/deferred components.
3. Contract examples and the minimal integration path for Abduljabbar; display semantics for Ali.
4. Collins's required hardware facts and the first bring-up sequence: identify board → verify wiring/electrical compatibility against manufacturer guidance → compile/flash a minimal acquisition test → confirm units/timing → replace fake adapter → inspect real recordings → calibrate → test simultaneous vibration → integrate the dashboard.
5. Hardware-only acceptance checks and the hour-6/hour-12 fallback gates from Salaar's plan.
6. A short list of remaining choices, clearly distinguished from tested facts.

Verify that simulated data cannot accidentally present itself as live, that the existing sleeve requires no fabrication task, and that no clinical or standards-compliance claims were introduced. Leave teammates' files untouched.

Completion: deliver a concise report of working pre-hardware capabilities, exact tests run, and the next concrete action once Collins provides component details. Do not claim the wearable itself has been validated.
```

## Parallel work while Claude executes

- Salaar: run/review each step, inspect plots or summaries, and coordinate processing boundaries.
- Abduljabbar: consume contract fixtures after Prompt 1 and simulator output after Prompt 2; integrate the processing API after Prompt 6.
- Ali: use shared sample payloads to prepare displays; keep unavailable values distinct from zero.
- Collins: confirm component models, electrical details, available controller count, and mounting on the ready-made sleeve.
- Fahad: independently implement the station test engine against simulated motor commands; align command acknowledgements when firmware details become available.

Pre-hardware completion means the software path is runnable and its behavior tested on synthetic inputs. Real timing, sensor noise, calibration quality, and simultaneous muscle/vibration measurement remain the first hardware milestones.
