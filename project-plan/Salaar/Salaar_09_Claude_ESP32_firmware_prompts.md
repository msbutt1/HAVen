# Salaar: ESP32 firmware plan and Claude prompts

## Target and sequence

ESP32 #1: sleeve, BioAmp EXG Pill and IMU #1. ESP32 #2: station, IMU #2, vibration motor through a suitable driver, Velostat, optional OLED and DHT11. Both IMUs are modules marked MPU-9250/6500; populated chip identity remains unverified. Arduino hardware is optional. Use the existing tattoo sleeve and gel electrodes.

This guide supersedes the optional portable-only Prompt 7 in Salaar_08. The existing Python processing implementation stays authoritative for EMG filtering/calibration, vibration features and experimental scoring. Fahad owns the station staircase/test engine; firmware owns acquisition and bounded actuator commands. Abduljabbar owns ingestion and API delivery.

Paste Prompt 0 into Claude Code at the repository root, then execute the other prompts separately. Each step ends with runnable evidence and remaining hardware checks. A fresh chat should start with Prompt 0. Hardware-dependent steps stay pending until their listed prerequisites are met; independent firmware modules can continue.

Prioritize wearable acquisition and basic station stimulus control. OLED and temperature are stretch work. Prompts 1–3 can largely proceed before assembly. After contracts are fixed, station work can proceed alongside wearable work using separate owned source files; one person owns shared code. Reserve approximately 2–4 hours for initial wearable firmware, 2–4 for station control, and 2–4 for bench integration, adjusting to actual remaining time and failures. These are planning allowances, not delivery guarantees.

## What Salaar/Collins must provide

### Needed now for exact board builds and pin assignments

- Clear markings on each ESP32's metal module and development board (for example, the actual WROOM/S3/C3 marking; “V2” alone is insufficient). Front/back photos or exact text are useful. Confirm whether both boards are identical.
- Photos or exact markings of both IMU modules and their pin labels. Firmware will read chip identity; a seller's combined MPU-9250/6500 label is not conclusive.
- BioAmp board revision and its current filter/solder-jumper configuration. Do not alter it until the matching manufacturer instructions are checked.
- Available USB data cables, breadboard, jumpers, multimeter and resistors. Confirm which computer will connect to both boards.

### Needed before BioAmp/body connection

- A proposed wiring/power diagram showing BioAmp supply, output conditioning and ESP32 ADC input.
- Confirmed supply/output limits and manufacturer-approved 3.3 V interfacing for this BioAmp revision.
- Manufacturer guidance for body-connected acquisition and power isolation reviewed by Collins. Complete initial controller/ADC tests without body electrodes attached.

### Needed before motor operation

- Motor label, part number or supplier page; rated voltage/current and whether it is a brushed eccentric rotating mass motor or a linear resonant actuator.
- Driver-board part number, or markings of available transistor/MOSFET and diode, plus resistor values. Bare GPIO motor drive is not an acceptable fallback.
- Suitable motor supply and circuit diagram, including protection components and shared reference where appropriate.
- How the user reports felt/not felt: dashboard buttons are sufficient initially; report if a physical button is available.

### Optional later

- OLED pin labels, controller model and interface (128×64 describes resolution only).
- DHT11 module versus bare sensor, and Velostat divider resistor/mounting details.
- Battery and charger model, permitted charge current, protection and power-path details before portable power. Battery capacity alone does not establish compatibility.

You do not need to determine IMU register values or ADC calibration constants manually; Claude should derive them from official documentation after identification and verify them on-device.

## Prompt 0 — Inspect and load boundaries

```text
Implement the ESP32 firmware work described in this guide, one requested step at a time.
Read signals/HARDWARE_INVENTORY.md, signals/HANDOFF.md, signals/contract/TELEMETRY_CONTRACT.md,
signals/PROCESSING.md, and applicable repository instructions. Inspect existing firmware,
backend and station code before selecting paths. Preserve teammates' work.

We have two ESP32 V2 development boards, two MPU-9250/6500-marked IMU modules,
one BioAmp EXG Pill, Velostat, two vibration motors, one OLED 128x64, and DHT11.
Motor driver boards are missing. Exact ESP32 variant, IMU chip identities, BioAmp
revision/interface and motor ratings need confirmation. Do not invent these facts.

Propose firmware/ with separate wearable and station targets and a small shared
contract/acquisition layer. Host algorithms remain in signals/. Fahad owns test
sequencing; Abduljabbar owns backend APIs. Keep optional peripherals disabled until
configured. Log unresolved hardware facts and completed checks in firmware/HANDOFF.md.

For each implementation step, consult current official chip/framework documentation,
pin dependency versions, implement only that step, and report commands actually run.
Distinguish host tests, successful board compilation, successful flashing and measured
hardware behavior. A compiled fake-device build is not a validated wearable.

For now, inspect the repo and available build tools, identify owned paths and missing
hardware facts, and report the next executable step. Do not run later prompts yet.
```

## Prompt 1 — Reproducible builds and board configuration

```text
Create the two firmware targets and documented build commands. Use the team's existing
framework/toolchain if present; otherwise select one familiar ESP32-supported toolchain
and justify it briefly. Verify APIs against official docs. Pin framework/library versions.

Separate board/pin/power facts from logic. Define configurable IMU bus pins/address,
EMG ADC pin/settings, station motor-control pin/polarity, Velostat input, and optional
peripherals. Check selected pins against the exact confirmed ESP32 variant, including
boot-strapping, flash/USB use, analog capability and input-only restrictions.

If exact board identification is still missing, build portable logic with fake adapters
and document the target-specific build as pending. Do not quietly select a generic board
and call it confirmed. Real motor output must remain disabled in an unconfigured target.

Provide reproducible host tests and build instructions. If a real target is confirmed,
compile both role binaries, but leave flashing for the bring-up step.

Done: owned source layout, dependency lock/version record, board configuration template,
working host build, and explicit status of both board builds.
```

## Prompt 2 — Wearable contract and transport

```text
Implement wearable telemetry compatible with the existing Python validator. Preserve
hello/status/emg_batch/imu_batch, independent sensor rates/sample indices, one shared
sequence counter per device/boot, a fresh boot ID per boot/stream start, and extended
monotonic timestamps. Document precisely when sample indices advance for dropped samples.

Compute actual wire load including framing at target sample rates. Prefer simple batched
NDJSON if the confirmed serial path supports it with headroom. Do not assume 115200 baud
or native USB. If a binary format is necessary, keep it minimal and provide a tested decoder
into the existing logical contract; coordinate its adapter boundary with Abduljabbar.

Keep acquisition separate from serialization and transmission. Bound all buffers and
frames, count losses, and prevent a slow host from blocking acquisition indefinitely.
Define reconnect behavior without repeating sequence numbers in the same boot.
Send diagnostics on a separate channel or disable them on the telemetry port.

Use a clearly simulated fake-acquisition target to emit deterministic test frames.
Hardware acquisition alone may emit live provenance; fallback/fake samples must never
be silently substituted into a live stream. Unknown sensor state remains unknown.

Done: generated firmware frames pass vg-validate, sequences/timing survive wrap/restart
checks, and a stalled output sink causes visible bounded loss rather than fabricated data.
```

## Prompt 3 — Sensor discovery and minimal bench bring-up

```text
Implement a diagnostic mode for the confirmed board. First boot with motor output off.
Probe each IMU on its own controller and report bus address, WHO_AM_I and supported
identity using official register documentation. Unsupported identity or failed reads must
fail visibly. Do not infer a magnetometer from a combined module label. Acceleration is
the required signal; gyro/magnetometer are not needed for the current score.

Read back configured acceleration range, sensor filtering and output rate. Report which
fields are measured versus configured. Provide a BioAmp-free ADC diagnostic using only
known safe input levels within the confirmed board specification.

Prepare a wiring table checked against actual board/module pinouts and supply requirements.
Ask for the missing physical facts only where they prevent correct wiring or flashing.
With user-confirmed USB port/board mapping, flash the diagnostic binary and record actual
results; otherwise mark flashing and sensor checks pending.

Done: either real identity/register/ADC observations recorded, or a compiled diagnostic
with exact pending physical steps. Do not present fake observations as hardware evidence.
```

## Review checkpoint A

Independent review after Prompt 3: build reproducibility, pin configuration, transport
budget, validator compatibility, synthetic/live separation and motor-off boot behavior.
Review pending physical checks explicitly. Station work can continue while wearable
physical measurements are pending.

## Prompt 4 — Wearable acquisition and host integration

```text
Implement acquisition for BioAmp analog EMG and IMU acceleration on ESP32 #1.
Use the verified ADC interface/settings and IMU identity. Start with 1000 Hz EMG as a
target if the confirmed hardware supports it; choose IMU rate/range/filter based on its
actual supported modes and intended signal band. Do not copy the simulator's rate or
4096 counts/g without configuring and verifying the corresponding sensor settings.

Acquire distinct new IMU samples using a justified data-ready/FIFO/polling strategy.
Timestamp actual acquisition; if FIFO samples use reconstructed timestamps, document
that method and its limitations. Track overruns, missed samples and timing variation.
Never repeat a stale register value while claiming it is a newly acquired sample.

Keep filter/envelope/calibration/scoring on the host. Report raw ADC units accurately;
separate ADC voltage conversion from input-referred muscle voltage and biological force.
Surface actual detectability limits: a normal analog value cannot prove electrodes are
connected. Report confirmed I/O failures, clipping and stale samples honestly.

Integrate recorded or live firmware output with the existing Processor. Establish real
conversion metadata and acquisition configuration. Run sustained simultaneous EMG/IMU
streaming, compare measured rates and transport load, and record lost samples.

Done: real recording validates, host features update, unplug/restart behavior is visible,
and remaining hardware/signal limitations are written down. If hardware is unavailable,
finish compilation and fake-adapter integration but leave live acceptance pending.
```

## Prompt 5 — Station command contract and safe actuator control

```text
Implement the station controller boundary with Fahad's existing engine/API if available.
Otherwise draft a small separate versioned command/response contract in firmware/ and
mark it proposed. Wearable telemetry v1 does not define station commands; do not squeeze
motor controls into EMG/IMU fields or silently broaden the existing validator.

Commands need request ID, bounded stimulus setting/duration, abort, and status. Define
accepted, started, completed and rejected acknowledgements with device timing. Duplicate
requests must not unexpectedly restart or extend a stimulus. Define busy/concurrent
command handling, malformed/oversized input handling and reboot identity.

Implement motor control through a verified driver adapter only. Default off at boot,
reset, disconnect timeout and fault; enforce a local maximum pulse duration independent
of host behavior. Abort must stop output promptly even during other peripheral activity.
Keep driver enable default-off in hardware as well as software where the circuit permits.

Until the motor type, driver, ratings and wiring are confirmed, use a fake driver or
logic-only output test. Do not energize a motor using a guessed pin or supply.
Use appropriate control for the identified actuator; a brushed motor PWM adapter is not
an LRA driver. The station staircase stays with Fahad on the host.

Done: tests cover abort, duplicate request, busy state, malformed commands, disconnect
and duration expiry; confirmed hardware tests demonstrate motor-off and bounded pulses,
or these are explicitly pending. Share exact command examples with Fahad/Abduljabbar.
```

## Prompt 6 — Station measurements and optional peripherals

```text
Add IMU #2 and Velostat acquisition to the station target using confirmed circuits.
Keep Velostat raw readings or a calibrated relative contact indicator; do not call them
newtons without physical force calibration. Define how the host checks a consistent
contact range and how missing/invalid contact data affects the test.

Associate acceleration samples and contact readings with stimulus/request IDs and
controller timestamps. Account for motor startup/settling and the actual IMU bandwidth.
PWM duty is a motor setting, not acceleration; actual measured amplitude and measurement
quality must be distinct. Do not claim a controlled-frequency sensory threshold when
motor frequency changes with drive or the IMU cannot resolve it.

Coordinate station events with Fahad. Reuse common acquisition code where justified,
but preserve a separate station event schema/adapter. Do not feed station IMU readings
into wearable exposure scoring by accident.

Only after motor/measurement basics work, add the confirmed OLED driver for prompts and
DHT11 as ambient context. Bound read/display work so it cannot delay abort or acquisition.
Use dashboard felt/not-felt buttons initially if no physical button is available.
Leave sound sensor, conductive textiles, RTC and NeoPixel outside the critical path.

Done: fake and real station events have clear provenance/units, host can associate trials
and measurements, and optional peripheral failures do not stop core acquisition/control.
```

## Prompt 7 — Complete system verification and realistic mock alignment

```text
Verify both ESP32 roles with the existing backend and station engine if available; if
not, use the documented adapters and identify exactly which integration remains pending.

Capture real relaxed/squeeze EMG, stationary/moving/vibrating IMU, sensor interruptions,
reboots, station motor pulses and contact-pressure variation. Record board/firmware/config,
units, timestamps and acquisition quality. Follow the BioAmp manufacturer's instructions
for body-connected use and the confirmed motor/power circuit.

Compare real recordings with the mock's ranges, offset, noise, spectrum, sample cadence,
quantization, saturation and failure behavior. Update a versioned hardware-specific mock
profile from measured facts without rewriting the historical synthetic fixtures. Retain
real recording provenance and cite the recordings used for profile tuning.

Run the existing signal regression suite plus firmware/adapter checks. Verify that
calibration fails visibly when interrupted and the score excludes invalid intervals.
Demonstrate host disconnect/reconnect, fresh boot IDs, no stale good-looking values,
and station abort while display/sensor tasks are active.

If EMG is unusable during vibration, document and use the previously agreed separate
muscle/vibration demonstration fallback. Do not spend remaining time on adaptive filtering.

Done: a recorded integrated demo, exact reproducible commands, measured limitations,
and a checklist distinguishing compiled, bench-tested and wearer-tested components.
```

## Review checkpoint B and final handoff

Request an independent review after Prompt 7, or earlier once wearable and station core
paths are working. Review actual source and hardware evidence; passing host tests alone
does not clear physical acquisition or motor-control acceptance.

## Prompt 8 — Freeze and document

```text
After review blockers are resolved, prepare a local checkpoint commit containing only
the completed firmware and intentional integration changes. Inspect staged files; keep
large recordings, build products, credentials and unrelated teammate work out. Do not push.

Finish firmware/HANDOFF.md with exact build/flash/monitor commands for each identified
board, confirmed pin/wiring tables, versions, startup/calibration procedure, station
command examples, recovery steps, power requirements, test evidence, and unresolved work.
Link the relevant source documents and real sample recordings. Keep a known-good binary
for each role with its build/config identity and a backup demo recording.

Report completion separately for wearable acquisition, station actuation/measurement,
optional peripherals and integrated demonstration. Stop after the handoff.
```

## Immediate next move

Start Prompt 0 now. Collect the ESP32 markings, IMU module labels and motor/driver details
in parallel. Start Prompt 1 with confirmed facts; use fake adapters for missing hardware.
The motor-driver gap must not hold up wearable firmware or the station protocol.
