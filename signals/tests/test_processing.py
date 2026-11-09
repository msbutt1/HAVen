"""Prompt 3 acceptance tests: host processing skeleton and quality handling.

All inputs are simulated. These tests verify bookkeeping and invalidation
semantics, not the quality of any algorithm or sensor.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from haven_signals import contract
from haven_signals.processing import (
    ProcessingConfig, Processor, StreamConfig, to_ndjson_line,
)
from haven_signals.simulator import SimConfig, Simulation

SIM = Path(__file__).resolve().parents[1] / "contract" / "fixtures" / "sim"
HAPPY = SIM / "happy_path_seed1_3s.ndjson"
FAULT = SIM / "fault_mix_seed1_5s.ndjson"


def load(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def sim_records(scenario, duration=5.0, seed=3, **kw):
    sim = Simulation(SimConfig(scenario=scenario, duration_s=duration, seed=seed, **kw))
    return [r.record for r in sim.records() if not r.lost]


def process(records, config=None, finish=True):
    proc = Processor(config)
    out = []
    for r in records:
        out += proc.feed(r)
    if finish:
        out += proc.finish()
    return proc, out


def feats(out, stream=None):
    return [o for o in out if o["type"] == "stream_features" and (stream is None or o["stream"] == stream)]


def events(out, kind=None):
    return [o for o in out if o["type"] == "stream_event" and (kind is None or o["event"] == kind)]


# -- fixtures through the API ---------------------------------------------------

@pytest.mark.parametrize("path", [HAPPY, FAULT], ids=lambda p: p.name)
def test_every_output_preserves_identity_provenance_and_timebase(path):
    records = load(path)
    _, out = process(records)
    assert out
    for o in out:
        assert o["schema"] == "vibeguard.features" and o["version"] == 1
        assert o["device_id"] == "sim-wearable-01"
        assert o["boot_id"] in {r["boot_id"] for r in records}
        assert o["provenance"]["source"] == "simulated"
        assert o["provenance"]["recording_id"] == records[0]["provenance"]["recording_id"]
        assert o["timebase"] == "device_monotonic_us"
        to_ndjson_line(o)  # serializable, finite


@pytest.mark.parametrize("path", [HAPPY, FAULT], ids=lambda p: p.name)
def test_processing_is_deterministic(path):
    a = "".join(map(to_ndjson_line, process(load(path))[1]))
    b = "".join(map(to_ndjson_line, process(load(path))[1]))
    assert a == b


def test_happy_path_has_no_fault_events_and_mostly_ok():
    _, out = process(load(HAPPY))
    assert {e["event"] for e in events(out)} == {"boot_change", "end_of_input"}
    for s in ("emg", "imu"):
        statuses = [f["status"] for f in feats(out, s)]
        assert statuses[0] == "warming_up" and statuses[-1] == "unavailable"
        assert set(statuses[:-1]) <= {"warming_up", "ok"}
        assert statuses.count("ok") > 0.8 * len(statuses)


def test_fault_fixture_surfaces_every_injected_fault():
    _, out = process(load(FAULT))
    kinds = {e["event"] for e in events(out)}
    assert {"seq_gap", "sample_gap", "boot_change", "device_sensor_state", "end_of_input"} <= kinds
    causes = {e["detail"]["cause"] for e in events(out, "sample_gap")}
    assert causes == {"device_overrun", "transport_or_unreported"}
    reasons = {r for f in feats(out) for r in f["reasons"]}
    assert {"clipped", "missing_values", "sensor_disconnected", "sensor_stale",
            "device_reports_disconnected", "device_reports_stale", "gap", "warming_up"} <= reasons


def test_placeholder_stats_on_relaxed_synthetic_emg():
    _, out = process(sim_records("relaxed", duration=2.0))
    ok = [f for f in feats(out, "emg") if f["status"] == "ok"]
    m = statistics_mean([f["features"]["window_stats"]["mean"]["emg0"] for f in ok])
    r = statistics_mean([f["features"]["window_stats"]["ac_rms"]["emg0"] for f in ok])
    assert abs(m - 2048) < 5 and 20 < r < 32  # simulator sigma ~25.7 counts
    assert all(f["window"]["measured_rate_hz"] == pytest.approx(1000, rel=1e-3) for f in ok)


def statistics_mean(xs):
    return sum(xs) / len(xs)


# -- availability semantics -----------------------------------------------------

def test_unavailable_is_null_never_zero():
    _, out = process(load(FAULT))
    for f in feats(out):
        ws = f["features"]["window_stats"]
        assert ws["status"] == f["status"]
        if f["status"] in ("ok", "degraded"):
            assert all(v is not None for v in ws["mean"].values())
        else:
            assert ws["mean"] is None and ws["ac_rms"] is None
            assert f["reasons"]


def test_warmup_requires_a_full_window_of_valid_data():
    _, out = process(sim_records("relaxed", duration=2.0))
    for s, window_s in (("emg", 0.1), ("imu", 0.5)):
        first_ok = next(f for f in feats(out, s) if f["status"] == "ok")
        first = feats(out, s)[0]
        assert first["status"] == "warming_up"
        # span from the first sample to the end of the first ok window >= window length
        assert (first_ok["window"]["end_us"] - 5_000) / 1e6 + 1 / first_ok["window"]["nominal_rate_hz"] \
            >= window_s - 1e-9


def test_gap_invalidates_and_no_ok_window_spans_it():
    _, out = process(sim_records("missing_batches", duration=5.0))
    for s in ("emg", "imu"):
        gaps = [e for e in events(out, "sample_gap") if e["stream"] == s]
        assert len(gaps) == 1 and gaps[0]["detail"]["cause"] == "transport_or_unreported"
        resume = gaps[0]["t_us"]
        after = [f for f in feats(out, s) if f["t_us"] is not None and f["t_us"] >= resume]
        assert after[0]["status"] in ("warming_up", "unavailable")
        for f in after:
            if f["status"] == "ok":
                assert f["window"]["start_us"] >= resume


def test_disconnect_never_shows_old_values():
    _, out = process(sim_records("invalid_samples", duration=10.0))
    disc = [e for e in events(out, "device_sensor_state")
            if e["stream"] == "emg" and e["detail"]["state"] == "disconnected"]
    back = [e for e in events(out, "device_sensor_state")
            if e["stream"] == "emg" and e["detail"]["state"] == "ok"]
    assert disc and back
    t0, t1 = disc[0]["t_us"], back[0]["t_us"]
    during = [f for f in feats(out, "emg") if t0 <= f["t_us"] < t1]
    assert during and all(f["status"] == "unavailable" for f in during)
    first_after = next(f for f in feats(out, "emg") if f["t_us"] > t1)
    assert first_after["status"] in ("warming_up", "unavailable")


def test_reboot_resets_all_streams():
    _, out = process(sim_records("reboot", duration=4.0))
    changes = events(out, "boot_change")
    assert len(changes) == 2 and changes[1]["detail"]["previous_boot_id"] == changes[0]["boot_id"]
    new_boot = changes[1]["boot_id"]
    for s in ("emg", "imu"):
        first = next(f for f in feats(out, s) if f["boot_id"] == new_boot)
        assert first["status"] == "warming_up"


def test_insufficient_coverage_and_degraded():
    cfg = ProcessingConfig(emg=StreamConfig(window_s=0.1, min_coverage=0.97))
    _, out = process(sim_records("invalid_samples", duration=10.0), cfg)
    statuses = {(f["status"], tuple(f["reasons"])) for f in feats(out, "emg")}
    assert ("unavailable", ("insufficient_coverage",)) in statuses
    assert ("degraded", ("missing_values",)) in statuses


# -- sequencing faults ---------------------------------------------------------

def test_duplicate_record_dropped():
    recs = load(HAPPY)
    dup = recs[:10] + [copy.deepcopy(recs[9])] + recs[10:]
    _, base = process(recs)
    _, out = process(dup)
    assert len(events(out, "duplicate_record")) == 1
    assert [o for o in out if o["type"] == "stream_features"] == feats(base)


def test_reordered_record_dropped_as_late():
    recs = load(HAPPY)
    swapped = recs[:10] + [recs[11], recs[10]] + recs[12:]
    _, out = process(swapped)
    assert len(events(out, "late_record")) == 1
    assert len(events(out, "seq_gap")) == 1
    assert events(out, "sample_gap")  # the dropped batch's samples are missing for its stream


def test_lost_record_shows_seq_gap_and_sample_gap():
    recs = load(HAPPY)
    victim = next(i for i, r in enumerate(recs) if i > 20 and r["type"] == "imu_batch")
    _, out = process(recs[:victim] + recs[victim + 1:])
    assert events(out, "seq_gap")[0]["detail"]["missing_records"] == 1
    gap = [e for e in events(out, "sample_gap") if e["stream"] == "imu"]
    assert gap and gap[0]["detail"]["missing_samples"] == len(recs[victim]["samples"])
    assert not [e for e in events(out, "sample_gap") if e["stream"] == "emg"]


def test_sample_overlap_dropped():
    recs = load(HAPPY)
    i = next(i for i, r in enumerate(recs) if i > 20 and r["type"] == "emg_batch")
    bad = copy.deepcopy(recs[i])
    bad["seq"] = recs[-1]["seq"] + 1  # new seq, but repeats old samples
    _, out = process(recs + [bad])
    assert len(events(out, "sample_overlap")) == 1


def test_records_from_retired_boot_are_dropped():
    recs = sim_records("reboot", duration=4.0)
    old = next(r for r in recs if r["boot_id"].endswith("-b0") and r["type"] == "emg_batch")
    _, out = process(recs + [old])
    assert len(events(out, "retired_boot_record")) == 1


def test_foreign_device_and_invalid_record():
    recs = load(HAPPY)
    other = copy.deepcopy(recs[5]); other["device_id"] = "someone-else"
    broken = copy.deepcopy(recs[6]); broken["samples"][0] = ["oops"]
    proc, out = process(recs[:5] + [other, broken] + recs[5:])
    assert len(events(out, "foreign_device")) == 1
    inv = events(out, "invalid_record")
    assert len(inv) == 1 and inv[0]["detail"]["codes"]
    tail = proc.feed_invalid_frame(contract.parse_line(b"{bad")[1], "x:1")
    assert tail[0]["event"] == "invalid_record" and tail[0]["detail"]["codes"] == ["malformed_json"]


def test_replay_provenance_is_carried_through():
    recs = load(HAPPY)
    for r in recs:
        r["provenance"] = {"source": "replay", "original_source": "simulated",
                           "recording_id": r["provenance"]["recording_id"]}
    _, out = process(recs)
    assert all(o["provenance"]["source"] == "replay"
               and o["provenance"]["original_source"] == "simulated" for o in out)


def test_provenance_change_mid_boot_invalidates():
    recs = load(HAPPY)
    for r in recs[60:]:
        r["provenance"] = {"source": "replay", "original_source": "simulated", "recording_id": "rec-x"}
    _, out = process(recs)
    assert len(events(out, "provenance_changed")) == 1


# -- staleness ---------------------------------------------------------------

def test_device_clock_stale_when_one_stream_goes_silent():
    recs = load(HAPPY)
    cut = [r for r in recs if not (r["type"] == "imu_batch" and 1_000_000 < r["timing"]["t_us"][0] < 1_600_000)]
    _, out = process(cut)
    stale = events(out, "stream_stale")
    assert len(stale) == 1 and stale[0]["stream"] == "imu"
    nxt = out[out.index(stale[0]) + 1]
    assert nxt["type"] == "stream_features" and nxt["status"] == "unavailable" and nxt["reasons"] == ["stale"]
    assert events(out, "stream_recovered")


def test_host_clock_requires_rx_and_poll_detects_silence():
    recs = load(HAPPY)
    proc = Processor(ProcessingConfig(clock="host"))
    with pytest.raises(ValueError):
        proc.feed(recs[0])
    out = []
    for i, r in enumerate(recs[:40]):
        out += proc.feed(r, rx_time_s=100.0 + i * 0.025)
    assert not events(out, "stream_stale")
    late = proc.poll(100.0 + 39 * 0.025 + 0.3)
    assert {e["stream"] for e in events(late, "stream_stale")} == {"emg", "imu"}
    assert all(f["status"] == "unavailable" for f in feats(late))


# -- configuration and unknown hardware values ---------------------------------

def test_unknown_limits_disable_clipping_detection_explicitly():
    cfg = ProcessingConfig(emg=StreamConfig(window_s=0.1, use_device_raw_limits=False))
    _, out = process(sim_records("clipping", duration=5.0), cfg)
    fs = feats(out, "emg")
    assert all(f["quality"]["clipping_detection"] == "unavailable_unknown_limits" for f in fs)
    assert all(f["quality"]["clipped_samples"] is None for f in fs)
    assert any("device_flagged_clipping" in f["reasons"] for f in fs)


def test_config_limits_and_rate_override_device_values():
    cfg = ProcessingConfig(emg=StreamConfig(window_s=0.1, raw_limits=(0, 4095), expected_rate_hz=1000.0))
    _, out = process(sim_records("clipping", duration=5.0), cfg)
    fs = [f for f in feats(out, "emg") if f["window"]["rate_source"]]
    assert all(f["quality"]["clipping_detection"] == "config_limits" for f in feats(out, "emg"))
    assert all(f["window"]["rate_source"] == "config" for f in fs)


def test_batch_declared_rate_used_when_hello_missing():
    recs = [r for r in load(HAPPY) if r["type"] != "hello"]
    _, out = process(recs)
    assert events(out, "boot_change")[0]["detail"]["hello_received"] is False
    f = next(f for f in feats(out, "emg") if f["status"] == "ok")
    assert f["window"]["rate_source"] == "batch_declared"
    assert f["quality"]["clipping_detection"] == "unavailable_unknown_limits"


@pytest.mark.parametrize("bad", [
    dict(clock="wall"), dict(stale_after_s=0), dict(emg=StreamConfig(window_s=0)),
    dict(imu=StreamConfig(window_s=0.5, raw_limits=(5, 5))), dict(continuity_tolerance=1.5),
])
def test_invalid_processing_config_rejected(bad):
    with pytest.raises(ValueError):
        Processor(ProcessingConfig(**bad))


# -- bounded buffering -----------------------------------------------------------

def test_buffers_bounded_independent_of_run_length():
    occupancy = {}
    for duration in (5.0, 60.0):
        proc = Processor()
        peak = {"emg": 0, "imu": 0}
        for r in sim_records("happy_path", duration=duration):
            proc.feed(r)
            for s, (n, cap) in proc.buffer_sizes().items():
                assert n <= cap
                peak[s] = max(peak[s], n)
        occupancy[duration] = peak
    assert occupancy[5.0] == occupancy[60.0]
    assert occupancy[60.0]["emg"] <= 100 and occupancy[60.0]["imu"] <= 201


def test_rate_mismatch_is_explicit_and_never_ok():
    cfg = ProcessingConfig(emg=StreamConfig(window_s=0.1, expected_rate_hz=100.0))
    proc = Processor(cfg)
    out = []
    for r in sim_records("relaxed", duration=2.0):  # actual synthetic rate 1000 Hz
        out += proc.feed(r)
        n, cap = proc.buffer_sizes()["emg"]
        assert n <= cap
    mm = events(out, "rate_mismatch")
    assert len(mm) == 1 and mm[0]["detail"]["expected_hz"] == 100.0
    assert mm[0]["detail"]["batch_declared_hz"] == 1000.0 and mm[0]["detail"]["expected_source"] == "config"
    assert not [f for f in feats(out, "emg") if f["status"] in ("ok", "degraded")]


def test_sample_window_hard_capacity():
    from haven_signals.processing.window import SampleWindow, WindowSample
    w = SampleWindow(length_us=1e9, capacity=5)
    flags = [w.append(WindowSample(float(i), (i,), None)) for i in range(12)]
    assert len(w) == 5 and w.overflow_count == 7 and flags.count(True) == 7
    assert [s.values[0] for s in w] == [7, 8, 9, 10, 11]
    w.evict_older_than(1e9 + 9.5)
    assert [s.values[0] for s in w] == [10, 11]


# -- CLI smoke ------------------------------------------------------------------

def test_cli_smoke_stdin_pipeline():
    sim = subprocess.run([sys.executable, "-m", "haven_signals.sim_cli", "--scenario",
                          "fault_mix", "--duration", "3", "--fast"],
                         capture_output=True, check=True)
    proc = subprocess.run([sys.executable, "-m", "haven_signals.process_cli", "-"],
                          input=sim.stdout, capture_output=True, check=True)
    lines = [json.loads(l) for l in proc.stdout.decode().splitlines()]
    assert lines and lines[-1]["event"] == "end_of_input"
    assert b"input records accepted" in proc.stderr
