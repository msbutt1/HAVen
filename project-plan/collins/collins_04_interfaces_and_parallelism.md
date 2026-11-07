# Interfaces and collaboration

## Architecture
Wearable sensors → acquisition firmware → local laptop backend → dashboard → saved sessions.
Station hardware ↔ controller ↔ backend/station engine ↔ station screens.

Use USB serial first. Acquisition and timing-sensitive motor control stay on the controller. Calibration management, scoring, orchestration, and history can run on the laptop; agree the precise processing boundary in hour one. Use a familiar stack rather than adding framework research to the critical path.

## Freeze a minimal contract in the first hour
Agree exact schemas and example payloads together; the following are required concepts, not a finalized wire protocol:
- Telemetry: device ID, sequence number, timestamp/timebase, sample rate, values, units, and quality flags.
- Commands: calibrate, start/stop wearable session, start/abort station test, deliver stimulus, and submit response.
- Responses: command identity, accepted/completed/error status, session identity.
- Provenance: live, recorded replay, or simulated; preserve the original recording identity for replay.
- States: disconnected, ready, calibrating, recording/testing, completed, error.
- Station records: trial ID, stimulus setting, measured amplitude only when available, response, missing-response status, and timing.

Mock and real adapters must expose the same interface. Publish example events and API responses so frontend and station work can proceed independently. Communicate breaking changes immediately to affected owners.

## Ownership and handoffs
Collins owns wiring and throwaway bring-up sketches. Salaar owns production firmware. Abduljabbar owns ingestion/storage/delivery. Ali owns all screens, including station screens. Fahad owns test behavior and integration coordination.

Physical handoff order: EMG verified → firmware processing; IMU verified → integrated acquisition; Collins moves to station wiring while wearable software is tested; mounting and reliability follow.

## Shared hardware
Confirm controller count and exact component models first. Two controllers permit independent devices. One controller requires separate modes and scheduled access. Keep one available IMU on the wearable initially. A station without amplitude measurement reports motor settings and its limitations.

## Working practices
Use separate owned modules/directories and small reviewable changes. Agree their exact paths in hour one. Maintain a short board: working / next deliverable / blocked by whom. Pair only to unblock a dependency. Schedule short hardware testing windows. Integration is continuous; meetings are brief.

## collins's boundary
Own component inventory, electrical compatibility, wiring, electrodes, sensor/motor bring-up, power setup, and physical assembly on the existing tattoo sleeve. Salaar owns production firmware and provides limited hardware help.
