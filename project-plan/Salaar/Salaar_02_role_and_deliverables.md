# Salaar: Firmware and signals lead, with limited hardware support

Own production acquisition firmware, sampling, signal processing, calibration, vibration features, and experimental scoring. Help Collins with focused bring-up and debugging windows; avoid becoming a second full-time hardware owner.

## First task
Create acquisition interfaces and test processing on synthetic or recorded samples while Collins wires EMG.

## Owned deliverables
- One owned production firmware path with documented build/flash procedure
- Timestamped, sequenced acquisition data with units and quality flags
- Relaxed/squeeze normalization and vibration features
- Explicitly experimental scoring implementation and documented limitations

## Parallel-working rule
Work against mocks or captured samples until the real dependency is ready. Do not wait idle for wiring, APIs, or screens. Flag interface changes to the affected owner immediately.
