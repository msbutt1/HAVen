"""Embedding the VibeGuard reference processor in a host service (SIMULATED DATA).

Shows the lifecycle a backend needs, with no CLI subprocess:

1. One Processor per device, created with an explicit config.
2. Feed every decoded record in arrival order; route outputs by ``type``.
3. In live (host-clock) mode, stamp receive times and call ``poll()`` from a timer.
4. Calibration commands from the backend's API go to ``calibration_start``.
5. Persist ``calibration_record()``; reload with ``load_calibration`` after a
   restart (it is used only once incoming data proves compatibility).
6. Call ``finish()`` when the stream ends, so nothing keeps looking current.

Run from signals/:  uv run python examples/embed_processor.py
"""

from __future__ import annotations

import json
from collections import Counter

from vibeguard_signals import contract
from vibeguard_signals.demo import demo_processing_config
from vibeguard_signals.processing import ProcessingConfig, Processor, to_ndjson_line
from vibeguard_signals.simulator import SimConfig, Simulation


def device_frames():
    """Stand-in for a serial/mock adapter: yields NDJSON frames (bytes) as a device would."""
    sim = Simulation(SimConfig(scenario="demo", duration_s=30.0, seed=1))
    for r in sim.records():
        if not r.lost:
            yield contract.serialize_record(r.record).encode()


class Sink:
    """Where a backend would publish/store outputs. Here: count and keep a few."""

    def __init__(self) -> None:
        self.counts = Counter()
        self.latest: dict[str, dict] = {}

    def publish(self, out: dict) -> None:
        kind = out["type"] if out["type"] != "stream_features" else f"features:{out['stream']}"
        self.counts[kind] += 1
        self.latest[kind] = out
        _ = to_ndjson_line(out)  # serialization a backend would store / send


def main() -> None:
    # Demo config: cites the SIMULATOR's synthetic accelerometer scale as the conversion.
    # For hardware, replace it with the confirmed sensor conversion and its source.
    config: ProcessingConfig = demo_processing_config()
    proc = Processor(config)  # device clock: deterministic for files/replay/tests
    # Live ingestion would use ProcessingConfig(..., clock="host") and then:
    #   outputs = proc.feed(record, rx_time_s=time.monotonic())
    #   every ~50 ms from a timer: outputs = proc.poll(time.monotonic())
    sink = Sink()
    calibration_api_calls = {0.5: "relaxed", 4.5: "squeeze"}  # device seconds; normally user-driven

    for frame in device_frames():
        record, issues = contract.parse_line(frame)
        if record is None:  # undecodable frame: report it, keep going
            outputs = proc.feed_invalid_frame(issues, "adapter")
        else:
            t = record.get("timing", {})
            t_s = (t.get("t0_us") if t.get("mode") == "uniform" else (t.get("t_us") or [None])[0])
            for at in sorted(calibration_api_calls):
                if t_s is not None and t_s / 1e6 >= at:
                    for out in proc.calibration_start(calibration_api_calls.pop(at)):
                        sink.publish(out)
            outputs = proc.feed(record)
        for out in outputs:
            sink.publish(out)

    saved = proc.calibration_record()  # persist this (JSON) with the session
    for out in proc.finish():
        sink.publish(out)

    emg = sink.latest["features:emg"]["features"]["emg_activation"]
    score = sink.latest["experimental_score"]
    print("outputs by type:", dict(sorted(sink.counts.items())))
    print("calibration persisted:", saved["calibration_id"] if saved else None,
          f"({len(json.dumps(saved))} bytes JSON)" if saved else "")
    print("final EMG state:", emg["envelope"]["status"], emg["envelope"]["reasons"])
    print(f"final score: {score['cumulative']['value']} {score['cumulative']['units']} over "
          f"{score['valid_duration_s']} s valid, {score['excluded_duration_s']} s excluded; "
          f"provenance={score['provenance']['source']}")

    # After a backend restart: a new Processor plus the stored calibration.
    restarted = Processor(config)
    events = restarted.load_calibration(saved)
    print("reloaded calibration state:", events[-1]["detail"]["state"], "(verified against data on next frames)")


if __name__ == "__main__":
    main()
