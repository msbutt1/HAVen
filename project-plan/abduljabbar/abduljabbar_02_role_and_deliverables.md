# abduljabbar: Backend lead

Own device adapters, serial ingestion, session management, storage, live delivery, recording/replay, history, and reconnect handling. Keep acquisition independent of UI refreshes.

## First task
Publish example events and run a simulated device through the ingestion and live-delivery path.

## Owned deliverables
- Mock and serial adapters behind the same interface
- Session and calibration APIs/events
- Persistent wearable and station records
- Live delivery, recording/replay, and recovery behavior

## Parallel-working rule
Work against mocks or captured samples until the real dependency is ready. Do not wait idle for wiring, APIs, or screens. Flag interface changes to the affected owner immediately.
