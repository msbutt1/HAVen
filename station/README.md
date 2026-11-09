# vibeguard-station

Fahad-owned station test engine for VibeGuard: a vibration-perception staircase test that runs
independently of any UI or hardware, through adapters. Everything run so far is **simulated**.
It is a prototype: the stimulus is a motor setting (not a measured amplitude) and the result is
not a diagnosis.

- Contract (proposed) and example payloads: [`contract/STATION_CONTRACT.md`](contract/STATION_CONTRACT.md)
- Status, verified commands, assumptions: [`HANDOFF.md`](HANDOFF.md)

```sh
uv sync && uv run pytest -q
uv run vg-station run --mode preview --seed 1 --out out/station
```
