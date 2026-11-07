# vibeguard-signals

Salaar-owned host-side reference package for VibeGuard. It holds the device telemetry
contract, its validator, and a deterministic simulated wearable, and the host processing skeleton.

All data produced here is labelled simulated. Nothing here establishes clinical accuracy,
calibrated grip force, or standards-compliant vibration dose.

- Contract spec: [`contract/TELEMETRY_CONTRACT.md`](contract/TELEMETRY_CONTRACT.md) (proposed)
- Simulator: [`SIMULATOR.md`](SIMULATOR.md)
- Processing: [`PROCESSING.md`](PROCESSING.md)
- Commands, decisions, progress: [`HANDOFF.md`](HANDOFF.md)

```sh
uv sync && uv run pytest -q
```
