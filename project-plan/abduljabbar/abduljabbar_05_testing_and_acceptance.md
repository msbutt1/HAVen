# abduljabbar: testing and acceptance

## Role-specific checks
- Malformed messages do not crash ingestion.
- Sequence gaps and device disconnects are surfaced.
- Repeated commands do not accidentally duplicate completed sessions.
- Live, replay, and simulated records preserve their provenance.
- Sessions survive restart and the local demo works without cloud services.

## Shared acceptance
- Wearer calibration and live muscle-activation changes work.
- Vibration changes are visible within sensor capabilities.
- A wearable session and station result are saved.
- Station abort and missing responses behave clearly.
- A disconnect produces visible status, not silently stale data.
- Restart works locally and recorded backup is usable.
- Simulated history is labeled and experimental metrics are identified.

Record evidence in a short log: build/version, scenario, expected result, observed result, owner, and unresolved limitation. Prioritize meaningful integration and state-machine tests; avoid spending time on tests that merely mirror implementation.
