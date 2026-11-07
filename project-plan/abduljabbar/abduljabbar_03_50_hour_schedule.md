# abduljabbar: 50-hour schedule

Hour 0–1: agree scope, inventory, contracts, ownership, and example data with the team. Then start independently.

## Hours 0–4
Build simulated device and ingestion skeleton.

## Hours 4–10
Implement serial adapter, live events, and session storage.

## Hours 10–18
Add recording/replay and calibration/session APIs.

## Hours 18–28
Add history, reconnect handling, and data-provenance enforcement.

## Hours 28–36
Fix integration failures and verify persistent records.

## Hours 36–43
Stabilize release and verify offline restart/replay.

## Hours 43–50
Rehearsal, rest, final fixes, packing, and submission buffer. No new features.

## Shared checkpoints
- Hour 4: every subsystem runs independently with mocks.
- Hour 10: real wearable reading reaches the dashboard.
- Hour 18: wearable session saves and station commands operate the motor.
- Hour 28: both journeys work end to end.
- Hour 36: three successful full runs and feature freeze.

Stagger rest around handoffs; these are elapsed milestones, not uninterrupted work shifts.
