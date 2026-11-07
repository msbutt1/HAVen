# Salaar: 50-hour schedule

Hour 0–1: agree scope, inventory, contracts, ownership, and example data with the team. Then start independently.

## Hours 0–4
Build acquisition skeleton and synthetic-signal processing.

## Hours 4–10
Integrate real EMG/IMU acquisition, envelope, and vibration features.

## Hours 10–18
Implement calibration and experimental scoring; support station firmware commands.

## Hours 18–28
Add quality flags and tune processing using captured data.

## Hours 28–36
Fix measurement failures and document supported sampling/bandwidth.

## Hours 36–43
Verify restart/calibration behavior and freeze firmware.

## Hours 43–50
Rehearsal, rest, final fixes, packing, and submission buffer. No new features.

## Shared checkpoints
- Hour 4: every subsystem runs independently with mocks.
- Hour 10: real wearable reading reaches the dashboard.
- Hour 18: wearable session saves and station commands operate the motor.
- Hour 28: both journeys work end to end.
- Hour 36: three successful full runs and feature freeze.

Stagger rest around handoffs; these are elapsed milestones, not uninterrupted work shifts.
