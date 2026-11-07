# Fahad: testing and acceptance

## Role-specific checks
- A timeout is recorded as missing data, not automatically as not felt.
- Abort stops stimulation and leaves the session in a clear state.
- Synthetic response sequences exercise up/down steps, reversals, and termination.
- A result is saved once and is labeled by stimulus units and data source.
- The short demo is identified as a preview, not equivalent to a full test.

## Shared acceptance
- Wearer calibration and live muscle-activation changes work.
- Vibration changes are visible within sensor capabilities.
- A wearable session and station result are saved.
- Station abort and missing responses behave clearly.
- A disconnect produces visible status, not silently stale data.
- Restart works locally and recorded backup is usable.
- Simulated history is labeled and experimental metrics are identified.

Record evidence in a short log: build/version, scenario, expected result, observed result, owner, and unresolved limitation. Prioritize meaningful integration and state-machine tests; avoid spending time on tests that merely mirror implementation.
