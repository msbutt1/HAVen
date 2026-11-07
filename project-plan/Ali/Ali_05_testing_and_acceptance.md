# Ali: testing and acceptance

## Role-specific checks
- Disconnects visibly invalidate stale readings.
- Units, calibration state, and signal-quality limitations remain visible.
- Station controls match the engine state and include abort.
- Simulated history and any accelerated time are visibly labeled.
- The full demo is legible on the judging laptop and works after refresh.

## Shared acceptance
- Wearer calibration and live muscle-activation changes work.
- Vibration changes are visible within sensor capabilities.
- A wearable session and station result are saved.
- Station abort and missing responses behave clearly.
- A disconnect produces visible status, not silently stale data.
- Restart works locally and recorded backup is usable.
- Simulated history is labeled and experimental metrics are identified.

Record evidence in a short log: build/version, scenario, expected result, observed result, owner, and unresolved limitation. Prioritize meaningful integration and state-machine tests; avoid spending time on tests that merely mirror implementation.
