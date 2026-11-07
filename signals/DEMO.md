# Offline end-to-end demo and replay (Prompt 6) — SIMULATED DATA ONLY

One command runs the scripted 30 s simulated session through the **same** simulator,
processor, calibration and serializer as everything else, then saves the derived output.
A second command replays the recording as `provenance: replay` and checks that the
processing reproduces it. No hardware, backend, database or UI is involved.

The data is synthetic, and the demo uses the simulator's own accelerometer scale
(4096 counts per g) as its cited conversion. Activation is a relative proxy, not
force/%MVC. Vibration is unweighted, and the score is experimental, in arbitrary units.

## Commands (run from `signals/`)

```sh
uv sync
uv run vg-demo run --out out/demo --seed 1 --run-id demo-seed1   # prints summary.txt
uv run vg-demo replay out/demo                                   # exit 0 iff replay matches
uv run vg-replay out/demo/telemetry.ndjson --speed 10 | uv run vg-process -   # paced replay pipeline
uv run python examples/embed_processor.py                        # API embedding example
```

`--run-id` fixes the identity so output files are byte-reproducible. Without it, each run
gets a fresh random identity, while sample content stays determined by `--seed`.
`out/` is git-ignored.

## The scripted session (`vg-sim --scenario demo`)

| Device time | What happens | What to look for |
|---|---|---|
| 0–4 s | Relaxed; relaxed calibration capture is commanded at 0.5 s | `capturing_relaxed` → `relaxed_captured`; score paused (`activation_uncalibrated` / `capturing_relaxed`) |
| 4–8 s | Stronger activation; squeeze capture is commanded at 4.5 s | Envelope ≈23 → ≈294 counts_rms; `calibrated` at ≈7.55 s |
| 8–11 s | Relaxed, no vibration | Activation ≈0; vibration ≈0.012 m/s² (noise); score ≈0 by formula |
| 11–15 s | Relaxed + 0.5 g synthetic vibration | Vibration ≈3.467 m/s²; score accumulates |
| 15–19 s | Stronger activation + vibration | Activation ≈1.0; score rate ≈2× (activation weighting, by formula) |
| 19.5–20.5 s | **EMG disconnected** (injected) | EMG `unavailable`; score `paused` (`activation_device_reports_disconnected`); cumulative frozen |
| 21.0–21.4 s | **Transport loss** (records dropped) | `seq_gap`, `sample_gap` on both streams, IMU `stream_stale`; score paused through the gap and re-warm-up (~1.25 s vibration) |
| 22.6–26 s | **Recovery**: moderate activation + 0.3 g | Activation ≈0.59, vibration ≈2.08 m/s², score accumulating again |
| 26–30 s | Relaxed, no vibration | End of input → every stream `unavailable` (`end_of_input`) |

Actual output of the `vg-demo run` command above (seed 1):

```text
  phase      envelope  activation  vib m/s^2    score au  valid s  label
  0-4         23.4293           -     0.0122         0.0      0.0  relaxed (relaxed calibration capture)
  4-8         293.548      1.0133     0.0124         0.0    0.449  stronger activation (squeeze calibration capture)
  8-11        23.7121      0.0008     0.0123         0.0      3.0  relaxed, no vibration
  11-15       23.0806     -0.0015     3.4666     0.01164      4.0  relaxed, synthetic vibration 0.5 g
  15-19      296.1633      1.0073     3.4669    0.026788      4.0  stronger activation + vibration 0.5 g
  19-26       183.105      0.5897     2.0802     0.00949    4.201  moderate activation + vibration 0.3 g; faults ...
  26-30       23.2291     -0.0009     0.0125    0.000683    4.045  relaxed, no vibration
Final: cumulative 0.048601 au over 19.695 s valid; excluded 10.251 s (by reason, in summary.txt)
```

## Output files (in `--out`)

| File | Content |
|---|---|
| `telemetry.ndjson` | The simulated device stream as a host would have received it (lost records absent); contract v1, `source: simulated` |
| `derived.ndjson` | Every processor output: `stream_features`, `stream_event`, `experimental_score` (format: `PROCESSING.md`) |
| `summary.csv` | One row per 0.5 s of device time, columns listed below |
| `summary.txt` | Per-phase medians, an event timeline, and the final score with excluded time by reason |
| `manifest.json` | `simulated: true`, recording_id, seed, full sim + processing config, calibration schedule, final calibration record, phase table, final score, SHA-256 of every file |
| `replay_derived.ndjson`, `replay_summary.csv` | Written by `vg-demo replay` |

`summary.csv` columns: `t_s, emg_envelope_counts_rms, emg_envelope_status,
activation_normalized, activation_display, activation_status, calibration_state,
vibration_combined_m_s2, vibration_status, score_status, score_rate_au_h,
score_cumulative_au, notes`. Empty cells mean unavailable, never 0. Each row shows the
latest record at or before `t_s`. Invalidation records are stamped with the stream's last
data time. For example, an IMU `stale` record detected at 21.35 s carries `t_us` 20.953 s,
so a row can show the vibration as unavailable slightly before the score record that
reacts to it.

## Replay

- `vg-demo replay DIR` re-processes `DIR/telemetry.ndjson` with
  `provenance = {source: replay, original_source: simulated, recording_id: <original>}`.
  It applies the recorded calibration schedule from `manifest.json` and compares the
  result with `derived.ndjson`.
- **Matching rule:** the outputs must be identical apart from `provenance` and the
  provenance-dependent `calibration_id`. Verified result: **YES**; any edit to
  `derived.ndjson` makes it report NO, with exit code 1.
- **`vg-replay FILE` (stand-alone).** Re-emits a recording with replay provenance.
  device_id, boot_id, seq, sample indices and device timestamps are unchanged, so the
  recording time remains the timebase.
- **Pacing.** `--speed X` paces wall-clock output at X× recording time (measured: 30 s
  recording at `--speed 10` took 3.04 s). Pacing is declared on stderr and never changes
  content: `--fast` and `--speed` output are byte-identical (tested). The score uses device
  time, so accelerated replay does not change it.
- This replay is for offline algorithm verification only. Production recording and replay
  belong to the backend.

## Embedding the processor (for Abduljabbar)

`examples/embed_processor.py` uses the API directly, with no CLI subprocess.

Lifecycle and reset requirements:

1. **One `Processor` per device.** Records from another `device_id` are rejected
   (`foreign_device`).
2. **Feed records in arrival order:** `proc.feed(record)` for decoded dicts, or
   `proc.feed_invalid_frame(issues, where)` for undecodable frames. Route outputs by
   `type`. Never mutate device fields.
3. **Live use:** `ProcessingConfig(clock="host")`, `proc.feed(record,
   rx_time_s=time.monotonic())`, plus `proc.poll(time.monotonic())` from a timer every
   ~50 ms. Without polling, total silence is never reported as stale.
4. **Calibration API → `proc.calibration_start("relaxed" | "squeeze")` /
   `calibration_cancel()`.** The returned events report state. Starting a relaxed capture
   discards the active calibration.
5. **Persist `proc.calibration_record()`.** After a backend restart, `load_calibration(rec)`
   shows `calibrated`, but normalisation stays `calibration_unverified` until the incoming
   data proves the same device, provenance and pipeline (and the same boot, unless
   `survives_reboot`).
6. **Resets are automatic and visible.** Boot changes, gaps, provenance changes and so on
   emit events and invalidate state; the backend should not try to "repair" them.
7. **`proc.finish()` at end of stream,** so no value keeps looking current.
8. **Config is immutable per Processor.** Changing it means a new Processor. Calibrations
   made under another config are invalidated (`processing_config_mismatch`).

Input example: any line of `telemetry.ndjson` (contract v1, `contract/TELEMETRY_CONTRACT.md`).
Output examples: `PROCESSING.md`, sections *Output records*, *EMG envelope and calibration*,
and *Vibration and experimental score*.

## Limitations

- Everything is synthetic. The demo shows wiring, availability and recovery behaviour,
  not sensor performance.
- The conversion is the simulator's own scale. Real hardware needs the confirmed
  accelerometer sensitivity and its source.
- No reboot in the scripted session; reboot handling is covered by tests (`reboot` and
  `fault_mix` scenarios). Summaries assume a single boot timebase.
- Excluded-time attribution names every blocking input over each finalised sub-interval
  (`PROCESSING.md`, *Interval semantics*).
- The processor's device-clock staleness rule (Prompt 3) reacts to which stream's first
  post-loss batch arrives first. Around a transport loss, two arrival orders can therefore
  produce slightly different (both conservative) invalidations, and so slightly different
  scores. The score never counts time its inputs report as invalid. Exact order
  invariance is tested for loss-free streams.
- The EMG vibration-interference limitation (`PROCESSING.md`) is not shown in this clean
  demo; see `vg-sim --scenario vibration_emg_mixed`.
