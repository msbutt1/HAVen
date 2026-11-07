"""Prompt 2 acceptance tests for the deterministic simulated wearable.

These check simulator behaviour and contract conformance on synthetic data
only. They demonstrate nothing about real sensors, electrodes, or links.
"""

import math
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pytest

from vibeguard_signals import contract
from vibeguard_signals.sim_cli import main as sim_main
from vibeguard_signals.simulator import (
    SCENARIOS, SIM_COUNTS_PER_G, SIM_EMG_LIMITS, SIM_EMG_OFFSET, SimConfig, Simulation,
)

SIM_FIXTURES = Path(__file__).resolve().parents[1] / "contract" / "fixtures" / "sim"
FAULT_FLAGS = {"clipped", "missing_values", "sensor_disconnected", "overrun", "stale"}


def run(**kw):
    sim = Simulation(SimConfig(**{"duration_s": 4.0, "seed": 7, **kw}), run_id="test-run")
    recs = list(sim.records())
    return sim, recs


def emitted(recs):
    return [r.record for r in recs if not r.lost]


def frames(**kw):
    _, recs = run(**kw)
    return "".join(contract.serialize_record(r) for r in emitted(recs))


def batches(records, rtype):
    return [r for r in records if r["type"] == rtype]


def sim_seconds(sim, rec):
    """Simulation time of a batch's first sample (test helper, single-boot runs)."""
    t = rec["timing"]
    return (t["t0_us"] if t["mode"] == "uniform" else t["t_us"][0]) / 1e6


# -- conformance and determinism ------------------------------------------------

@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_is_schema_valid_and_simulated(name):
    _, recs = run(scenario=name)
    assert recs
    for r in recs:
        frame = contract.serialize_record(r.record)  # raises if invalid
        assert contract.parse_line(frame)[1] == []
        prov = r.record["provenance"]
        assert prov["source"] == "simulated" and "original_source" not in prov


def test_same_seed_and_config_is_byte_identical():
    assert frames(scenario="fault_mix") == frames(scenario="fault_mix")


def test_different_seed_changes_values_not_structure():
    a = emitted(run(scenario="happy_path", seed=1)[1])
    b = emitted(run(scenario="happy_path", seed=2)[1])
    # IMU timestamp jitter is seeded, so EMG/IMU interleaving may differ; per-stream
    # structure (batch boundaries, sample indices) must not.
    for t in ("emg_batch", "imu_batch", "status"):
        key = lambda r: (r.get("first_sample_index"), len(r.get("samples", [])))
        assert [key(r) for r in batches(a, t)] == [key(r) for r in batches(b, t)]
    assert batches(a, "emg_batch")[5]["samples"] != batches(b, "emg_batch")[5]["samples"]


def test_streams_are_independent_of_each_others_rate():
    a = batches(emitted(run(imu_rate_hz=400.0)[1]), "emg_batch")
    b = batches(emitted(run(imu_rate_hz=200.0)[1]), "emg_batch")
    assert [x["samples"] for x in a] == [x["samples"] for x in b]


def test_run_metadata_reports_seed_config_and_faults():
    sim, recs = run(scenario="fault_mix")
    hello = recs[0].record
    detail = hello["provenance"]["detail"]
    assert hello["type"] == "hello" and hello["seq"] == 0
    assert detail["seed"] == 7 and detail["scenario"] == "fault_mix"
    assert detail["config"]["emg_rate_hz"] == 1000.0 and detail["config"]["imu_rate_hz"] == 400.0
    assert {f["kind"] for f in detail["declared_faults"]} == {
        "transport_loss", "overrun", "null_samples", "disconnect", "stale", "reboot"}
    assert all(r.record["provenance"]["recording_id"] == sim.recording_id for r in recs)


# -- sequencing, indices, time ---------------------------------------------------

def test_one_seq_counter_per_boot_across_record_types():
    _, recs = run(scenario="reboot")
    by_boot = defaultdict(list)
    for r in recs:
        by_boot[r.record["boot_id"]].append(r.record)
    assert len(by_boot) == 2
    for rs in by_boot.values():
        assert [r["seq"] for r in rs] == list(range(len(rs)))
        assert {r["type"] for r in rs} >= {"hello", "status", "emg_batch", "imu_batch"}
        assert rs[0]["type"] == "hello"


def test_sample_indices_and_device_time_per_stream_and_boot():
    _, recs = run(scenario="fault_mix")
    state = {}
    for rec in emitted(recs):
        if rec["type"] not in ("emg_batch", "imu_batch"):
            continue
        key = (rec["boot_id"], rec["type"])
        t = rec["timing"]
        n = len(rec["samples"])
        times = ([t["t0_us"] + i * t["interval_us"] for i in range(n)]
                 if t["mode"] == "uniform" else t["t_us"])
        first = rec["first_sample_index"]
        dropped = rec["quality"].get("dropped_samples_before", 0)
        if key in state:
            nxt, last_t = state[key]
            assert first >= nxt + dropped  # forward only; extra gap only from transport loss
            assert times[0] > last_t
        else:
            assert first - dropped >= 0
        state[key] = (first + n, times[-1])
        assert n <= contract.MAX_SAMPLES_PER_BATCH
    # reboot restarts indices and device time
    boots = sorted({k[0] for k in state})
    assert len(boots) == 2


def test_reboot_restarts_seq_index_and_device_time():
    _, recs = run(scenario="reboot")
    b1 = [r.record for r in recs if r.record["boot_id"].endswith("-b1")]
    assert b1[0]["type"] == "hello" and b1[0]["seq"] == 0 and b1[0]["t_us"] < 10_000
    first_emg = batches(b1, "emg_batch")[0]
    assert first_emg["first_sample_index"] == 0 and first_emg["timing"]["t0_us"] < 10_000


def test_batches_are_bounded_by_batch_duration():
    _, recs = run(emg_rate_hz=1000.0, imu_rate_hz=400.0, batch_ms=50.0)
    assert max(len(r["samples"]) for r in batches(emitted(recs), "emg_batch")) == 50
    assert max(len(r["samples"]) for r in batches(emitted(recs), "imu_batch")) == 20


def test_imu_explicit_timestamps_measure_configured_rate():
    _, recs = run(imu_rate_hz=400.0)
    ts = [t for r in batches(emitted(recs), "imu_batch") for t in r["timing"]["t_us"]]
    mean_dt = (ts[-1] - ts[0]) / (len(ts) - 1)
    assert abs(mean_dt - 2500) < 5


# -- happy path and signal sanity (synthetic surrogates only) -------------------

def test_happy_path_has_no_fault_flags_and_no_loss():
    sim, recs = run(scenario="happy_path")
    assert not any(r.lost for r in recs)
    for rec in emitted(recs):
        if "quality" in rec:
            assert not set(rec["quality"]["flags"]) & FAULT_FLAGS
        if rec["type"] == "status":
            assert rec["sensors"] == {"emg": "ok", "imu": "ok"}


def test_strong_activation_is_distinct_from_relaxed():
    sim, recs = run(scenario="strong_activation", duration_s=10.0)
    relaxed, strong = [], []
    for r in batches(emitted(recs), "emg_batch"):
        t = sim_seconds(sim, r)
        vals = [row[0] for row in r["samples"]]
        (strong if 3.2 < t < 6.8 else relaxed if t < 2.8 or t > 7.2 else []).extend(vals)
    # activation 0.8 vs 0.05 -> synthetic sigma ~321 vs ~26 counts
    assert statistics.pstdev(strong) > 10 * statistics.pstdev(relaxed)
    assert abs(statistics.mean(relaxed) - SIM_EMG_OFFSET) < 2


def test_stationary_gravity_magnitude_and_no_dynamic_component():
    _, recs = run(scenario="stationary_gravity")
    rows = [row for r in batches(emitted(recs), "imu_batch") for row in r["samples"]]
    mags = [math.sqrt(sum(v * v for v in row)) for row in rows]
    assert abs(statistics.mean(mags) - SIM_COUNTS_PER_G) < 5
    assert all(statistics.pstdev([row[k] for row in rows]) < 6 for k in range(3))


def test_vibration_on_window_has_expected_synthetic_rms():
    sim, recs = run(scenario="vibration", duration_s=8.0)
    on = [row for r in batches(emitted(recs), "imu_batch") if 2.2 < sim_seconds(sim, r) < 5.8
          for row in r["samples"]]
    off = [row for r in batches(emitted(recs), "imu_batch") if sim_seconds(sim, r) < 1.8
           for row in r["samples"]]
    # 0.5 g sinusoid along a unit axis: dynamic magnitude RMS = 0.5/sqrt(2) g
    dyn = [math.sqrt(sum((row[k] - statistics.mean(r[k] for r in on)) ** 2 for k in range(3)))
           for row in on[:400]]
    rms = math.sqrt(statistics.mean(d * d for d in dyn))
    assert abs(rms - 0.5 / math.sqrt(2) * SIM_COUNTS_PER_G) < 0.05 * SIM_COUNTS_PER_G
    assert all(statistics.pstdev([row[k] for row in off]) < 6 for k in range(3))


def test_mixed_scenario_contaminates_relaxed_emg():
    sim, recs = run(scenario="vibration_emg_mixed", duration_s=10.0)
    emg = batches(emitted(recs), "emg_batch")
    def tone_amplitude(lo, hi, hz=60.0):
        s = c = 0.0
        n = 0
        for r in emg:
            if not lo < sim_seconds(sim, r) < hi:
                continue
            t0, dt = r["timing"]["t0_us"], r["timing"]["interval_us"]
            for i, row in enumerate(r["samples"]):
                w = 2 * math.pi * hz * (t0 + i * dt) / 1e6
                s += (row[0] - SIM_EMG_OFFSET) * math.sin(w)
                c += (row[0] - SIM_EMG_OFFSET) * math.cos(w)
                n += 1
        return 2 * math.hypot(s, c) / n

    # coupling 150 counts/g x 0.5 g = 75-count synthetic tone at 60 Hz
    assert abs(tone_amplitude(2.2, 4.8) - 75) < 8
    assert tone_amplitude(0.0, 1.8) < 5


def test_vibration_above_nyquist_margin_rejected():
    with pytest.raises(ValueError, match="aliasing"):
        Simulation(SimConfig(scenario="vibration", imu_rate_hz=100.0))
    Simulation(SimConfig(scenario="vibration", imu_rate_hz=100.0, vibration_hz=30.0))


@pytest.mark.parametrize("bad", [
    {"duration_s": 0}, {"duration_s": 3601}, {"emg_rate_hz": 0}, {"scenario": "nope"},
    {"batch_ms": 0.5}, {"device_id": "bad id"},
])
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError):
        Simulation(SimConfig(**bad))


# -- declared faults are produced (and stay schema-valid) ----------------------

def test_clipping_flagged_only_while_overdriven():
    sim, recs = run(scenario="clipping", duration_s=10.0)
    emg = batches(emitted(recs), "emg_batch")
    clipped = [r for r in emg if "clipped" in r["quality"]["flags"]]
    assert clipped and all(2.9 < sim_seconds(sim, r) < 7.0 for r in clipped)
    assert all(v in SIM_EMG_LIMITS for r in clipped for row in r["samples"]
               for v in row if v in SIM_EMG_LIMITS) and sim.summary.emg.clipped_batches == len(clipped)


def test_missing_batches_show_as_seq_gap_matching_lost_count():
    sim, recs = run(scenario="missing_batches")
    seqs = [r["seq"] for r in emitted(recs)]
    gaps = sum(b - a - 1 for a, b in zip(seqs, seqs[1:]))
    assert sim.summary.records_lost > 0 and gaps == sim.summary.records_lost
    assert all(r.record["type"] != "hello" for r in recs if r.lost)


def test_invalid_samples_scenario_faults():
    sim, recs = run(scenario="invalid_samples", duration_s=10.0)
    out = emitted(recs)
    emg, imu = batches(out, "emg_batch"), batches(out, "imu_batch")
    disc = [r for r in emg if "sensor_disconnected" in r["quality"]["flags"]]
    assert disc and all(v is None for r in disc for row in r["samples"] for v in row)
    assert all(4.9 < sim_seconds(sim, r) < 7.0 for r in disc)
    sparse = [r for r in emg + imu if "missing_values" in r["quality"]["flags"]
              and "sensor_disconnected" not in r["quality"]["flags"]]
    assert sparse and all(1.9 < sim_seconds(sim, r) < 4.1 for r in sparse)
    stale = [r for r in imu if "stale" in r["quality"]["flags"]]
    assert stale and all(len({tuple(row) for row in r["samples"]}) == 1 for r in stale)
    statuses = [r for r in out if r["type"] == "status"]
    assert any(s["sensors"]["emg"] == "disconnected" for s in statuses)
    assert any(s["sensors"]["imu"] == "stale" for s in statuses)
    assert statuses[-1]["sensors"] == {"emg": "ok", "imu": "ok"}


def test_overrun_reports_dropped_samples_and_index_jump():
    sim, recs = run(scenario="fault_mix", duration_s=10.0)
    for rtype in ("emg_batch", "imu_batch"):
        bs = batches(emitted(recs), rtype)
        over = [r for r in bs if "overrun" in r["quality"]["flags"]]
        assert over
        for r in over:
            prev = max((p for p in bs if p["boot_id"] == r["boot_id"]
                        and p["first_sample_index"] < r["first_sample_index"]),
                       key=lambda p: p["first_sample_index"])
            expected = prev["first_sample_index"] + len(prev["samples"])
            assert r["first_sample_index"] - expected == r["quality"]["dropped_samples_before"]
    assert sim.summary.emg.samples_dropped > 0 and sim.summary.imu.samples_dropped > 0


# -- CLI: stdout/stderr split, file output, pacing independence ----------------

def test_cli_stdout_is_pure_ndjson_and_stderr_has_diagnostics():
    proc = subprocess.run(
        [sys.executable, "-m", "vibeguard_signals.sim_cli", "--scenario", "relaxed",
         "--duration", "1", "--fast", "--seed", "3"],
        capture_output=True, text=True, check=True)
    lines = proc.stdout.splitlines()
    assert lines and all(contract.parse_line(l)[1] == [] for l in lines)
    assert "SIMULATED" in proc.stderr and "records emitted" in proc.stderr


def test_wall_paced_output_equals_fast_output(tmp_path):
    fast, paced = tmp_path / "fast.ndjson", tmp_path / "paced.ndjson"
    common = ["--scenario", "fault_mix", "--duration", "1", "--seed", "5", "--run-id", "pace-test"]
    assert sim_main(common + ["--fast", "-o", str(fast)]) == 0
    assert sim_main(common + ["--speed", "20", "-o", str(paced)]) == 0
    assert fast.read_bytes() == paced.read_bytes()


@pytest.mark.parametrize("name,args", [
    ("happy_path_seed1_3s.ndjson", ["--scenario", "happy_path", "--seed", "1", "--duration", "3",
                                    "--run-id", "fixture-happy-seed1"]),
    ("fault_mix_seed1_5s.ndjson", ["--scenario", "fault_mix", "--seed", "1", "--duration", "5",
                                   "--run-id", "fixture-fault-seed1"]),
])
def test_tracked_recordings_regenerate_exactly(tmp_path, name, args):
    out = tmp_path / name
    assert sim_main(args + ["--fast", "-o", str(out)]) == 0
    assert out.read_bytes() == (SIM_FIXTURES / name).read_bytes()
