# Backend and phone app integration: semi-final (for Abduljabbar)

**Status:** semi-final, 2026-11-07. Everything here runs on the laptop today against
the real wearable (board W). Calibration was run on the bench; the bottle-grip test
worked end to end in the browser page. Numbers from that test are not in this
document. Branch: `firmware-esp32`, commit `415eaa9`.

Owner of the firmware and signal processing: Salaar. Owner of ingestion, storage and
delivery to the phone: you. Nothing here is validated on a wearer for clinical use:
it is a screening and demo chain.

---

## 1. What the wearable sends

Board W (ESP32, Wi-Fi) streams **device telemetry contract v1** as NDJSON, one record
per line, over **TCP port 8765** to the laptop. It finds the laptop itself with a
UDP broadcast on 8766, so the laptop's hotspot IP can change.

- Records: `hello`, `status`, `emg_batch` (1 kHz, 50 rows per batch), `imu_batch`
  (500 Hz, 25 rows per batch, accelerometer only).
- `provenance.source` is `live`. Values are raw ADC counts (EMG) and raw counts
  (IMU; ±8 g = 4096 LSB/g).
- Validate every line with `haven_signals.contract.parse_line` before use.
  Invalid lines are rejected whole; never repair them.
- Field definitions: `signals/contract/TELEMETRY_CONTRACT.md`.
- Live Wi-Fi rates measured on board W: about 41 records/s, about 32 kB/s, 0 sample
  drops in the 2-minute idle test.

## 2. What the laptop computes (the processed outputs)

The processor runs on the laptop. Do **not** re-implement any of it. Embed it (section
4) or read the bridge's derived stream. Outputs (`vibeguard.features`, version 1):

| Record `type` | What | Key fields |
|---|---|---|
| `stream_features` (`stream`: `emg` / `imu`) | Features per batch | `features.emg_activation` (EMG) and `features.vibration` (IMU) |
| `stream_event` | Something happened | `event`, `stream`, `detail`. Calibration: `event: calibration_state`, `detail.state` and `detail.reason` |
| `tool_use` | Tool vs arm use | `state`, `status`, `tool_band_rms_ms2`, `motion_band_rms_ms2`, `dominant_hz`, `bout_s`, `bouts`, `longest_bout_s`, `session_tool_s`, `session_observed_s`, `warnings` |
| `experimental_score` | Experimental score | `rate`, `cumulative`, `status`. Not a dose, not validated |

Full formats: `signals/PROCESSING.md` (including the new `tool_use` section).

**The numbers the phone should show for grip:**
- `features.emg_activation.normalized.value.emg0`: the grip effort, 0 = relaxed
  baseline, 1 = calibrated maximum squeeze. Unbounded; may go above 1.
- `features.emg_activation.normalized.display.emg0`: the same value clamped to 0..1, for
  bars.
- `features.emg_activation.normalized.above_reference.emg0` and `below_relaxed.emg0`:
  flags for "above the calibrated maximum" and "below the relaxed baseline".
- `features.emg_activation.normalized.status`: `ok`, `degraded` (clipped samples),
  `warming_up`, `unavailable`. Show a dash when it isn't `ok`/`degraded`. Never show a
  missing value as 0.
- `features.emg_activation.calibration.state` and `.reason`: see section 3.

**Labels the app must show** (they are in the records too; keep them visible):
- Grip: "grip effort, % of your calibrated maximum squeeze" (**not** "grip force").
- Tool: "tool use, estimated from vibration" with the thresholds in `tool_use.thresholds`.
- Provenance: `live` on the data from the wearable.

## 3. The calibration procedure (what the phone must drive)

The processor's calibration is a state machine (`signals/PROCESSING.md`, "EMG envelope
and calibration"). Two captures, each about 3 s plus a 0.5 s discard at the start:

```
uncalibrated --relaxed--> capturing_relaxed --ok--> relaxed_captured
relaxed_captured --squeeze--> capturing_squeeze --ok--> calibrated
any capture --failure--> failed   (reason says why)
```

**Phone flow (what to build):**
1. Screen: "Relax your forearm completely, resting on a table." Button **Start relaxed**.
   Send the `relaxed` command. Show the countdown until the state changes to
   `relaxed_captured`. Keep the button in place until the next state.
2. Screen: "Squeeze as hard as you can and hold." Button **Start squeeze**. Send the
   `squeeze` command. Countdown until `calibrated`.
3. Done: show "Calibrated" and the live grip-effort %. Offer **Recalibrate**, which
   starts again at step 1.
4. On `failed`: show the reason in plain words and go back to step 1.

Calibration reasons the code emits (from `calibration.py` and `emg.py`):

| `reason` | Plain message for the user |
|---|---|
| `insufficient_separation` | "The squeeze wasn't clearly stronger than rest. Squeeze harder, or check the pads, and try again." |
| `insufficient_data` | "Not enough signal during the capture. Check the pads and try again." |
| `interrupted_boot_change` | "The wearable restarted during calibration. Start again." |
| `interrupted_processing_change` | "The processing settings changed during calibration. Start again." |
| `channel_mismatch`, `device_mismatch`, `boot_mismatch`, `provenance_mismatch` | "This calibration belongs to a different device or session. Recalibrate." |
| `processing_config_mismatch` | "The calibration was made with different settings. Recalibrate." |
| `calibration_unverified` | Shown after a backend restart until new data confirms the same device (see section 5). |

**Commands** the phone sends (via the backend, to the processor):
- `relaxed`, `squeeze`, `cancel`, `newshift`. The bridge also accepts these as one
  plain line each, and the local browser page uses JSON `{"cmd": "relaxed"}` on POST
  `/cmd`. The backend should call the processor methods directly (section 4).

**Important:** calibration is **not kept across a wearable reboot**
(`survives_reboot = False` by design). The phone must tell the wearer to recalibrate
after a `boot_change` event.

## 4. How to wire it into the backend

Two options. **Recommended: option A (embed the processor).** It needs no separate
process, and commands go straight to the processor.

**Option A: embed `Processor` in the backend** (`signals/examples/embed_processor.py` is
the reference; `signals/src/haven_signals/bridge.py` is a complete live example):

```python
import dataclasses, time
from haven_signals import contract
from haven_signals.processing import Processor, ProcessingConfig, StreamConfig, to_ndjson_line

G = 9.80665
def live_config(lsb_per_g: float = 4096.0) -> ProcessingConfig:
    conv = {a: (G / lsb_per_g, 0.0) for a in ("ax", "ay", "az")}
    src = "MPU-6500 configured +-8 g; 4096 LSB/g per TDK PS-MPU-9250A-01"
    return dataclasses.replace(ProcessingConfig(), clock="host", stale_after_s=1.0,
                               imu=StreamConfig(window_s=0.5, conversion=conv, conversion_source=src))

proc = Processor(live_config())            # ONE Processor per device_id
def on_line(line: bytes):                  # every line from TCP 8765
    rec, issues = contract.parse_line(line)
    outs = proc.feed_invalid_frame(issues, "wifi") if rec is None or issues \
        else proc.feed(rec, rx_time_s=time.monotonic())
    for o in outs: publish(o)              # to the database and the phone
def every_50ms():                          # keeps "silence" visible as stale
    for o in proc.poll(time.monotonic()): publish(o)
def calibrate(phase):                      # "relaxed" | "squeeze" | "cancel"
    outs = proc.calibration_cancel() if phase == "cancel" else proc.calibration_start(phase)
    for o in outs: publish(o)
def new_shift():
    for o in proc.tool_use_reset_session(): publish(o)
```

Rules from `signals/DEMO.md`, which still apply:
- One `Processor` per device. Feed records in arrival order.
- Call `poll()` about every 50 ms, otherwise total silence is never reported.
- Save `proc.calibration_record()` to your database after each `calibrated`. After a
  backend restart, call `proc.load_calibration(saved)`. It only takes effect after new
  data confirms the same device and session, so show `calibration_unverified` until
  then.
- Call `proc.finish()` when a session ends.

**Option B: read the bridge's stream.** Run `uv run vg-bridge` and read the derived
NDJSON from TCP **8767**. Send commands to the same port as plain lines. This is fine for
a demo, but Option A is simpler to make reliable.

**Ports (laptop):**

| Port | Who | Purpose |
|---|---|---|
| 8765 | wearable → bridge/backend | telemetry (TCP) |
| 8766 | wearable → broadcast | discovery (UDP) |
| 8767 | bridge → subscribers | derived records out, commands in (option B) |
| 8080 | `vg-live --web` (local) | the browser calibration page (testing only) |

## 5. Persistence and recovery

- **Store raw telemetry** exactly as received (the `vibeguard.device_telemetry`
  lines). This is the backup recording, and replay uses it (`vg-replay`). It must keep
  `provenance` as `live`.
- **Store derived records** as well, with their `provenance`.
- **Calibration:** `calibration_record()` JSON, per device, with its `calibration_id`.
- **Tool time:** the session totals live in the processor. Save the `tool_use` record
  after each update, so a restart can show the last known totals. A new shift calls
  `tool_use_reset_session()`.
- **Gaps and restarts:** the processor emits `boot_change`, `seq_gap`, `sample_gap`,
  `stream_stale` and `stream_recovered`. Show them to the user; do not smooth over them.

## 6. What the phone app shows

Minimum screens, plain text and numbers are enough for the demo:

1. **Live:** grip effort % (with a bar), calibration state, tool state (`tool` /
   `no_tool` / `unknown`), tool time this session, warnings (`warnings` list), data age.
2. **Calibrate:** the two-step flow in section 3 with countdowns.
3. **New shift:** one button, with a confirmation.
4. **Events:** the latest `stream_event`s, in plain words.

Show `unknown` / `unavailable` as a dash, never as 0. Keep "simulated" and "live"
labels visible on every screen.

## 7. What to test before the demo

| Check | How | Expected |
|---|---|---|
| Live data arrives | Wearable on, laptop on the hotspot | Records appear within a few seconds |
| Calibration completes | Relaxed, then squeeze, from the phone | `calibrated`, grip % appears |
| Failure is explained | Squeeze lightly | `insufficient_separation` shown in plain words |
| Recalibrate after reboot | Reset the wearable | `boot_change` shown; calibration needs redoing |
| Tool detection | Vibrating phone pressed on the IMU | `tool` after about 2 s; tool time climbs; stops after it's removed |
| Stale data | Pull the wearable's power | `stream_stale` within about 1 s; values become dashes |
| Restart recovery | Restart the backend | Calibration shown as `calibration_unverified` until new data confirms it |

Tests of the processing itself: `cd signals && uv run pytest` (330 tests, all passing).

## 8. Known limits (say these in the app or the demo)

- **Grip effort is relative.** It is muscle activity as a fraction of this wearer's
  maximum squeeze. It is not force, and it is not %MVC.
- **EMG needs electrodes on skin**, with the BioAmp's BANDPASS jumper bridged. The
  input is validated by the processor (`degraded` when clipped).
- **The tool thresholds are provisional** (`signals/src/haven_signals/processing/config.py`,
  `ToolUseConfig`). They were tuned on a phone motor, not a real power tool. The
  warnings are team choices, not regulatory limits.
- **The experimental score** is arbitrary units and is not a dose.
- **The phone and backend are not yet wired to this.** That is the integration task.

## 9. Open questions

1. Which backend stack will host the processor (a long-running Python process, or a
   subprocess per device)?
2. Where should calibration commands originate: the phone via the backend (recommended),
   or the wearable's own button (not built yet)?
3. Should warnings also buzz the sleeve? That needs a laptop-to-board command channel,
   which is not built.
