# Salaar: testing and acceptance

## Role-specific checks
- Actual sample rate and dropped samples are measured rather than assumed.
- Filter settings fit the actual sample rate and sensor bandwidth.
- Relaxed/squeeze calibration rejects invalid or near-zero ranges.
- Clipping, disconnected sensors, and poor-quality inputs are surfaced.
- Changing the grip weight changes the score by design; this is not presented as validation of transmission.

## Shared acceptance
- Wearer calibration and live muscle-activation changes work.
- Vibration changes are visible within sensor capabilities.
- A wearable session and station result are saved.
- Station abort and missing responses behave clearly.
- A disconnect produces visible status, not silently stale data.
- Restart works locally and recorded backup is usable.
- Simulated history is labeled and experimental metrics are identified.

Record evidence in a short log: build/version, scenario, expected result, observed result, owner, and unresolved limitation. Prioritize meaningful integration and state-machine tests; avoid spending time on tests that merely mirror implementation.
