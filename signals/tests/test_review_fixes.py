"""Regression tests for the post-Prompt-3 checkpoint review (findings 1-6).

Synthetic inputs only; these pin processing/simulator semantics.
"""

import copy
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vibeguard_signals import contract
from vibeguard_signals.processing import ProcessingConfig, Processor, StreamConfig
from vibeguard_signals.sim_cli import main as sim_main
from vibeguard_signals.simulator import SimConfig, Simulation

HAPPY = Path(__file__).resolve().parents[1] / "contract" / "fixtures" / "sim" / "happy_path_seed1_3s.ndjson"


def load():
    return [json.loads(l) for l in HAPPY.read_text().splitlines()]


def run(records, config=None):
    proc = Processor(config)
    out = []
    for r in records:
        out += proc.feed(r)
    return proc, out


def feats(out, stream):
    return [o for o in out if o["type"] == "stream_features" and o["stream"] == stream]


def events(out, kind):
    return [o for o in out if o["type"] == "stream_event" and o["event"] == kind]


def emg_only_stream(segments, nominal=1000.0):
    """hello + contiguous EMG batches; segments = [(rate_hz, n_batches), ...]."""
    base = {"schema": contract.SCHEMA, "version": 1, "device_id": "t-dev", "boot_id": "t-boot",
            "provenance": {"source": "simulated"}}
    recs = [{**base, "type": "hello", "seq": 0, "t_us": 1000,
             "firmware": {"name": "test", "version": "0"},
             "streams": {"emg": {"nominal_rate_hz": nominal, "channels": ["emg0"],
                                 "units": {"emg0": "adc_counts"}, "raw_limits": None,
                                 "sensor_model": None}}}]
    seq, idx, t = 1, 0, 5000.0
    for rate, nb in segments:
        dt = 1e6 / rate
        for _ in range(nb):
            recs.append({**base, "type": "emg_batch", "seq": seq, "sample_rate_hz": rate,
                         "first_sample_index": idx,
                         "timing": {"mode": "uniform", "t0_us": round(t), "interval_us": dt},
                         "channels": ["emg0"], "units": {"emg0": "adc_counts"},
                         "samples": [[2048 + (i % 7) - 3] for i in range(50)],
                         "quality": {"flags": []}})
            seq, idx, t = seq + 1, idx + 50, t + 50 * dt
    for r in recs:
        contract.require_valid(r)
    return recs


# -- Finding 1: rate mismatch is an availability condition ----------------------

@pytest.mark.parametrize("configured", [800.0, 700.0, 500.0, 1200.0])
def test_moderate_configured_rate_mismatch_never_ok(configured):
    cfg = ProcessingConfig(emg=StreamConfig(window_s=0.1, expected_rate_hz=configured))
    _, out = run(load(), cfg)
    fs = feats(out, "emg")
    assert fs and not [f for f in fs if f["status"] in ("ok", "degraded", "warming_up")]
    assert all("rate_mismatch" in f["reasons"] for f in fs)
    assert len(events(out, "rate_mismatch")) == 1


def test_rate_within_tolerance_is_accepted():
    cfg = ProcessingConfig(emg=StreamConfig(window_s=0.1, expected_rate_hz=1050.0))  # 5 %
    _, out = run(load(), cfg)
    assert not events(out, "rate_mismatch")
    assert [f for f in feats(out, "emg") if f["status"] == "ok"]


def test_device_rate_disagreeing_with_hello_is_mismatch_then_recovers():
    recs = emg_only_stream([(800.0, 10), (1000.0, 10)])  # hello declares 1000 Hz
    _, out = run(recs)
    fs = feats(out, "emg")
    first, second = fs[:10], fs[10:]
    assert all(f["status"] == "unavailable" and f["reasons"] == ["rate_mismatch"] for f in first)
    assert len(events(out, "rate_mismatch")) == 1 and len(events(out, "rate_mismatch_cleared")) == 1
    assert second[0]["status"] == "warming_up" and "rate_mismatch" in second[0]["reasons"]
    assert second[-1]["status"] == "ok"
    for f in second:  # no ok window may contain mismatched-rate samples
        if f["status"] == "ok":
            assert f["window"]["start_us"] >= recs[11]["timing"]["t0_us"]


# -- Finding 2: rejected records never mutate state ------------------------------

def _altered(rec):
    r = copy.deepcopy(rec)
    r["provenance"] = {"source": "live"}
    return r


def test_duplicate_with_altered_provenance_leaves_state_unchanged():
    recs = load()
    _, base = run(recs)
    proc = Processor()
    out = []
    for r in recs[:60]:
        out += proc.feed(r)
    prov_before = dict(proc._prov)
    rejected = proc.feed(_altered(recs[40]))
    assert [o["event"] for o in rejected] == ["duplicate_record"]
    assert proc._prov == prov_before
    for r in recs[60:]:
        out += proc.feed(r)
    assert out == base


def test_late_record_with_altered_provenance_leaves_state_unchanged():
    recs = load()
    cfg = ProcessingConfig(recent_seq_memory=1)  # force the "late", not "duplicate", path
    _, base = run(recs, cfg)
    proc = Processor(cfg)
    out = []
    for r in recs[:60]:
        out += proc.feed(r)
    rejected = proc.feed(_altered(recs[30]))
    assert [o["event"] for o in rejected] == ["late_record"]
    for r in recs[60:]:
        out += proc.feed(r)
    assert out == base


def test_new_record_with_altered_provenance_still_invalidates():
    recs = load()
    recs[60:] = [_altered(r) for r in recs[60:]]
    _, out = run(recs)
    assert len(events(out, "provenance_changed")) == 1


# -- Finding 3: device clipping flag honoured even when host limits pass ---------

def test_device_clipping_flag_degrades_when_values_inside_limits():
    recs = load()
    for r in recs:
        if r["type"] == "emg_batch" and r["seq"] > 40:
            r["quality"]["flags"] = ["clipped"]
    _, out = run(recs)
    last = feats(out, "emg")[-1]
    assert last["quality"]["clipping_detection"] == "device_declared_limits"
    assert last["quality"]["clipped_samples"] == 0
    assert last["status"] == "degraded" and last["reasons"] == ["device_flagged_clipping"]


# -- Finding 4: host-clock CLI reports silence before EOF ------------------------

def test_open_silent_stdin_triggers_stale_before_eof():
    lines = HAPPY.read_bytes().splitlines(keepends=True)[:40]
    proc = subprocess.Popen(
        [sys.executable, "-m", "vibeguard_signals.process_cli", "--clock", "host",
         "--poll-interval", "0.05", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    got, seen = [], threading.Event()

    def reader():
        for line in proc.stdout:
            rec = json.loads(line)
            got.append(rec)
            if rec.get("event") == "stream_stale":
                seen.set()

    threading.Thread(target=reader, daemon=True).start()
    try:
        proc.stdin.write(b"".join(lines))
        proc.stdin.flush()  # stdin stays open and silent from here
        assert seen.wait(timeout=5.0), "no stale output while stdin was open"
        stale = [r for r in got if r.get("event") == "stream_stale"]
        assert {r["stream"] for r in stale} == {"emg", "imu"}
        assert proc.poll() is None  # still running: this happened before EOF
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    assert got[-1]["event"] == "end_of_input"


# -- Finding 5: run identity separate from reproducible sample generation -------

def _batches(sim):
    return [(r.record["type"], r.record["first_sample_index"], r.record["samples"])
            for r in sim.records() if r.record["type"].endswith("_batch")]


def test_independent_runs_have_distinct_boot_and_recording_ids_same_samples():
    a, b = Simulation(SimConfig(duration_s=1)), Simulation(SimConfig(duration_s=1))
    assert a.recording_id != b.recording_id
    ra, rb = list(a.records()), list(b.records())
    assert ra[0].record["boot_id"] != rb[0].record["boot_id"]
    a2, b2 = Simulation(SimConfig(duration_s=1)), Simulation(SimConfig(duration_s=1))
    assert _batches(a2) == _batches(b2)
    assert ra[0].record["provenance"]["detail"]["config_digest"] == \
        rb[0].record["provenance"]["detail"]["config_digest"]


def test_explicit_fixture_identity_is_byte_reproducible():
    def frames():
        sim = Simulation(SimConfig(scenario="fault_mix", duration_s=2), run_id="fixture-x")
        return "".join(contract.serialize_record(r.record) for r in sim.records())
    assert frames() == frames()
    assert '"boot_id":"sim-fixture-x-b0"' in frames()


@pytest.mark.parametrize("bad", ["", "has space", "x" * 49])
def test_invalid_run_id_rejected(bad):
    with pytest.raises(ValueError):
        Simulation(SimConfig(duration_s=1), run_id=bad)


def test_cli_runs_get_distinct_boot_ids(tmp_path):
    a, b = tmp_path / "a.ndjson", tmp_path / "b.ndjson"
    for p in (a, b):
        assert sim_main(["--scenario", "relaxed", "--duration", "0.5", "--fast", "-o", str(p)]) == 0
    boot = lambda p: json.loads(p.read_text().splitlines()[0])["boot_id"]
    assert boot(a) != boot(b)


# -- Finding 6: low rates produce valid timestamps; bad configs fail up front ----

@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("imu_rate,emg_rate,duration", [
    (0.1, 1000.0, 25.0), (0.5, 1.0, 12.0), (1.0, 0.2, 12.0), (2.0, 5.0, 3.0), (20000.0, 20000.0, 0.05),
])
def test_supported_rate_extremes_produce_valid_timestamps(imu_rate, emg_rate, duration, seed):
    sim = Simulation(SimConfig(scenario="relaxed", imu_rate_hz=imu_rate, emg_rate_hz=emg_rate,
                               duration_s=duration, seed=seed))
    for r in sim.records():  # require_valid inside records() would raise on a bad timestamp
        rec = r.record
        if rec["type"] == "imu_batch":
            assert min(rec["timing"]["t_us"]) >= 0
    assert sim.summary.imu.samples_acquired >= 1


@pytest.mark.parametrize("args", [
    ["--imu-rate", "0"], ["--emg-rate", "25000"], ["--duration", "0"], ["--imu-rate", "-1"],
    ["--scenario", "vibration", "--imu-rate", "0.1"],
])
def test_unsupported_config_fails_before_any_output(args):
    proc = subprocess.run([sys.executable, "-m", "vibeguard_signals.sim_cli", "--fast", *args],
                          capture_output=True, text=True)
    assert proc.returncode == 2 and proc.stdout == "" and "vg-sim:" in proc.stderr
