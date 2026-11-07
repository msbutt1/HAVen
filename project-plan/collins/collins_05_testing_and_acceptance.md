# collins: testing and acceptance

## Role-specific checks
- Actual supply and logic/analog voltage compatibility is confirmed before connection.
- Gel electrodes contact skin; sleeve secures components without excessive compression.
- Cables have strain relief and movement does not pull electrodes loose.
- Motor uses an appropriate driver and obeys stop/abort commands.
- Body-connected acquisition follows the supplied device safety/power guidance.

## Shared acceptance
- Wearer calibration and live muscle-activation changes work.
- Vibration changes are visible within sensor capabilities.
- A wearable session and station result are saved.
- Station abort and missing responses behave clearly.
- A disconnect produces visible status, not silently stale data.
- Restart works locally and recorded backup is usable.
- Simulated history is labeled and experimental metrics are identified.

Record evidence in a short log: build/version, scenario, expected result, observed result, owner, and unresolved limitation. Prioritize meaningful integration and state-machine tests; avoid spending time on tests that merely mirror implementation.
