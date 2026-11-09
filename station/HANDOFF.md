# HAVen station — status and handoff

> **FOR TEAMMATES (Ali, Abduljabbar, Salaar, Collins).** This is Fahad's handoff for the
> station test engine. Find your section under "For teammates" below, and note the
> assumptions that need your confirmation.

**Owner:** Fahad (station engine, integration). **Status (2026-11-07):** the engine, simulated
adapter, controller-link adapter (against a simulated controller), records, validators, local
store, CLI and example payloads are built and tested. Everything was verified on **simulated
data only**. Nothing has run against the real motor, buttons or Salaar's firmware, so no
claim about real perception, motor output or timing is made. The station is a
vibration-perception prototype, not a diagnostic test.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11. Run from `station/`.

```sh
uv sync
uv run pytest -q                                          # all tests
uv run vg-station run --mode preview --seed 1 --out out/station
uv run vg-station run --mode full --seed 2 --miss-rate 0.2 --false-positive-rate 0.3
uv run vg-station run --abort-after 3                     # abort path
uv run vg-station examples                                # regenerate contract/examples/
```

## Ready to connect

When the wiring and firmware exist, follow [`HARDWARE.md`](HARDWARE.md): `vg-station ports`,
`vg-station selftest --port COMx`, then `vg-station live --port COMx --mode preview`. The laptop side
(serial transport, handshake, real-time runner, self-test, result saving) is built and tested against
simulated controllers, a pyserial loopback port, and a compiled PC build of the reference firmware core.
Not yet run against a real ESP32, motor or buttons. `vg-station live --simulate` rehearses the whole path.

## What is built

| Module | Purpose |
|---|---|
| `config.py` | `StationConfig` (full / preview); assumed motor-setting range, flagged as assumed |
| `staircase.py` | Pure staircase rule: step, reversals, stop at 8, mean of last 6, range limits |
| `engine.py` | Clock-driven state machine: ready → testing → completed, plus abort / error |
| `records.py` | Trial records, session result, quality limitations, labels |
| `adapters.py` | Adapter interface, `SimulatedStationAdapter`, `SimulatedSubject`, `ScriptedResponder`, `VirtualClock` |
| `link.py` | `LinkStationAdapter` (real adapter path over a line transport) + `SimulatedController` |
| `store.py` | `ResultStore`: one JSON file per session, never overwritten |
| `contract.py` | Validators for results, trials, events, commands, link messages |
| `simulation.py`, `examples.py`, `cli.py` | Virtual-time driver, example generator, `vg-station` |
| `serial_transport.py`, `live.py`, `selftest.py`, `hardware_cli.py` | USB serial transport (pyserial), real-time runner, hardware self-test, `ports` / `selftest` / `live` commands |
| `firmware/station_controller/` | **Reference** ESP32 controller: portable protocol core (tested on a PC) + board sketch (uncompiled, pins unset on purpose) |
| `HARDWARE.md` | Wiring checklist, bring-up steps, troubleshooting |

## Acceptance checks (from `project-plan/Fahad/Fahad_05_testing_and_acceptance.md`)

| Requirement | Where it is tested |
|---|---|
| Timeout recorded as missing, not "not felt" | `test_engine.py::test_timeout_is_recorded_as_missing_never_as_not_felt` and neighbours; validator rejects the opposite |
| Abort stops stimulation and leaves a clear state | `test_abort_*` (direct and over the link) |
| Synthetic sequences cover up/down steps, reversals, termination | `test_staircase.py`, `test_synthetic_sequence_*` |
| Saved once, labelled by units and source | `test_result_is_handed_to_the_sink_exactly_once`, `test_store_never_overwrites_*`, `test_result_is_labelled_*` |
| Preview identified as a preview | `test_preview_is_identified_as_a_preview_and_is_shorter`, validator |

## Assumptions I made without asking teammates (all are config, not code)

| Choice | Default | Needs |
|---|---|---|
| Motor setting range / start / step | 1–100, start 60, step 5 (preview 10) | Collins: driver and usable range |
| Response window / stimulus / inter-trial | 4 s / 1000 ms / 1 s | judging flow |
| Catch trials | ~1 in 6, none in preview | team |
| Missing response | same level repeated, no staircase movement; 3 in a row ends the test | team |
| Preview length | 4 reversals, mean of last 2, no catch trials | demo timing |
| Stimulus/response framing | response window counted from the stimulus command | firmware ack timing |

## For teammates

- **Ali:** subscribe to the events (`tick()` returns new ones) and poll `snapshot()`. Show
  `provenance`, the `PREVIEW` label, `framing`, and the `quality.limitations`. Abort calls
  `engine.abort()`. On-screen felt / not felt buttons call `submit_response(..., source="on_screen")`
  and are listed as a limitation.
- **Abduljabbar:** pass any `sink(result)`; raise `store.AlreadySavedError` if the session id was
  already stored. Example result files are in `contract/examples/`. Open: does the engine run
  inside your process, and who owns the station serial port?
- **Salaar:** the proposed controller line protocol is in `contract/STATION_CONTRACT.md`
  (deliver / stop / ack / response). Please confirm or amend it; `link.py` is the host side.
- **Collins:** two buttons (felt / not felt) are needed. The motor setting range is assumed.

## Review

An independent adversarial review found no problem in the staircase or the missing-response
rule, and 8 edge-case issues (unexpected adapter exceptions, NaN times, a CLI that claimed a
failed save, store filename collisions, duplicate-vs-late labelling, short runs able to carry
the full-test label, an inconsistent deadline, and a fragile "already saved" signal). All are
fixed and covered by `tests/test_review_fixes.py`. Known and accepted: link response times are
stamped when polled (a stalled UI loop can turn a valid press into a missing one); UI presses
without a trial id can be misattributed if `inter_trial_s` is 0; no locking, so call
`abort()` and `tick()` from one thread.

A second review of the hardware path (serial transport, handshake, reference firmware) found 8
more issues, all fixed with regression tests in `tests/test_review_fixes2.py`: the sketch would not
compile on Arduino-ESP32 core 2.x; session ids of 59+ characters made every trial fail (limit is now
58); a transport that said nothing was labelled live (now fails closed to simulated); a garbage line
could raise `RecursionError`; the controller could answer "busy" just after a stimulus ended; the
controller's JSON reader accepted leading/trailing junk and ignored a damaged stop; an ack without a
valid status counted as accepted; events right after the hello were dropped.

## Not done / deferred

- Nothing has run on real hardware: the serial transport, the handshake and the reference
  firmware are tested only against simulation, a pyserial loopback port and a PC build of the core.
  The `.ino` board layer has never been compiled (no ESP32 toolchain on this PC). Pins, PWM
  frequency and the motor driver are Collins's to confirm.
- No pressure-pad (Velostat) gating; contact pressure is reported as uncontrolled.
- Persistence into the backend; the local `ResultStore` is the stand-in.
- No UI. No integration checklist / failure log, pitch, slides or submission material yet.
- HeadGuard is not in the repo plan and is not implemented.
