# Team mock integration

Status: software checked on synthetic inputs. Hardware models and recordings are still required for sensor agreement. This fixture is a provisional integration profile, not a validated model of a worker or tool.

## Start here

From `signals/`:

```sh
uv sync --locked
uv run python examples/mock_feed.py
```

This generates the existing 30-second calibrated demo, verifies its replay, and serves only `out/team-mock/` on localhost port 8766. Leave it running while the frontend develops. Stop with Ctrl+C. For files only, add `--build-only`. Generated data is git-ignored; the script reproduces it.

- `/derived.json`: JSON envelope with `source`, `hardware_verified`, `recording_id`, and `records`. Use this for UI development.
- `/telemetry.json`: same envelope containing device contract records, for backend ingestion.
- `/telemetry.ndjson`: raw input for streaming adapters.
- `/derived.ndjson`: original processor output.
- `/summary.csv` and `/summary.txt`: inspect expected phases and values.
- `/manifest.json`: processing configuration, calibration schedule, original artifact hashes and source identity. JSON wrapper files are convenience exports outside the original manifest hash list.

## Ali: frontend

Use `examples/frontend_mock.js` as a browser module; call `playMock(record => updateYourStore(record))`. It delivers existing processor records paced over 30 seconds and returns a cleanup function. Display a persistent **SIMULATED** badge. Faster playback changes presentation timing only; label the selected speed.

Route `stream_features` by `stream` (`emg`/`imu`), `experimental_score` to the score view, and `stream_event` to connection/calibration notices. Use the exact fields documented in PROCESSING.md. Display unavailable values as a dash, keep quality/reasons visible, and keep the score's `au` units. Clear displayed values on unavailable/end-of-input. Do not render missing samples as zero. The fixed demo has one boot; backend live routing must also handle boot changes.

The HTTP server is a development fixture endpoint. Align final frontend API envelopes with Abduljabbar; the underlying derived records are the shared signal interface.

## Abduljabbar: backend

Start with `examples/embed_processor.py`: it demonstrates raw frame ingestion, scripted calibration and the same synthetic IMU conversion used to generate the expected results. Merely piping raw telemetry into an unconfigured processor will leave activation uncalibrated and physical vibration unavailable; that is correct behavior.

For paced raw transport input:

```sh
uv run vg-sim --scenario demo --duration 30 --seed 1
```

In the backend, use one Processor per device. Provide the demo processing configuration for simulated inputs only; issue relaxed/squeeze capture commands as shown in the example (or from UI controls). For wall-paced input use host clock with receive timestamps and poll every approximately 50 ms. Persist original provenance and call finish() on stream closure. Production hardware conversion must come from the actual component configuration.

For faults beyond the fixed demo, use `vg-sim --list` and select `fault_mix`, `reboot`, `clipping`, or `invalid_samples`. These are raw test feeds; they do not automatically produce calibrated activation. Reboot resets device time and requires identity-aware routing.

## What the readings represent

The provisional EMG device uses 0–4095 ADC counts, offset 2048, synthetic amplitude-modulated noise, 1000 Hz sampling by default. ADC voltage reference, analog gain, electrode placement and actual biological amplitudes are unknown; counts cannot yet be converted into a defensible human EMG voltage or grip force.

The provisional accelerometer uses signed counts with 4096 counts/g and gravity plus sinusoidal vibration/noise. Its default sampling, tone, scale, and jitter are model choices. The 0.5 g sinusoid gives an ideal vector RMS near 3.47 m/s²; the processor result is consistent with that constructed input. This does not prove a particular tool produces that signal. Real vibration can be broadband, impulsive, orientation-dependent and subject to sensor filtering/aliasing; the current single-tone model does not reproduce all of those effects.

Disconnect/stale flags are injected deliberately. Actual firmware may not be able to detect every injected condition. The experimental score is arbitrary and its activation weighting is assumed. Station motor perception data and longitudinal worker history need their own explicitly simulated fixtures; this package covers Salaar's wearable pipeline only.

## Required hardware agreement

Confirm controller/ADC resolution and reference; EMG raw-versus-envelope output and analog bandwidth; IMU model, range, scale, filter and output rate; actual acquisition timing; and transport throughput. The proposed telemetry contract must be implemented by firmware or a backend adapter. Capture relaxed/squeeze and stationary/vibrating recordings, compare distributions/spectra and timing against these fixtures, then adjust the profile. Until that comparison is done, exact sensor matching remains unverified.

Known conservative variation: transport-loss arrival order can change full-processor invalidation duration by about 0.05 s in the demo. See HANDOFF D18.

## Confirmed inventory update (2026-11-07)

See [HARDWARE_INVENTORY.md](HARDWARE_INVENTORY.md). BioAmp EXG Pill ×1, ESP32 V2 ×2 and two IMUs marked MPU-9250/6500 are confirmed. Use one ESP32/IMU pair per wearable and station. Arduino hardware is optional. Motor driver boards remain missing. Exact ESP32 variant, populated IMU chip identity and configured acquisition settings still need confirmation; mock scaling remains provisional.
