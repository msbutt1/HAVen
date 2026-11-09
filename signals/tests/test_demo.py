"""Prompt 6 tests: offline end-to-end demo, replay, and the embedding example.

SIMULATED data only. These confirm the wiring and the visible behaviour of
the scripted run; they say nothing about real sensors.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from haven_signals import contract
from haven_signals.demo import CSV_COLUMNS, replay_demo, run_demo
from haven_signals.replay import ReplayError, to_replay
from haven_signals.replay_cli import main as replay_main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo")
    res = run_demo(out, seed=1, run_id="test-demo")
    return out, res


def lines(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines()]


def scores(derived):
    return [o for o in derived if o["type"] == "experimental_score" and o["t_us"] is not None]


def cum_at(derived, t_s):
    return [s for s in scores(derived) if s["t_us"] <= t_s * 1e6][-1]["cumulative"]["value"]


def test_outputs_exist_and_are_marked_simulated(demo):
    out, res = demo
    for name in ("telemetry.ndjson", "derived.ndjson", "summary.csv", "summary.txt", "manifest.json"):
        assert (out / name).stat().st_size > 0
    m = json.loads((out / "manifest.json").read_text())
    assert m["simulated"] is True and m["provenance"] == "simulated"
    with (out / "telemetry.ndjson").open("rb") as fh:
        tel = list(contract.iter_ndjson(fh))
    assert tel and all(not issues and rec["provenance"]["source"] == "simulated" for _, rec, issues in tel)
    assert all(o["provenance"]["source"] == "simulated" for o in lines(out / "derived.ndjson") if o.get("provenance"))
    assert "SIMULATED DATA ONLY" in (out / "summary.txt").read_text()


def test_manifest_hashes_match_files(demo):
    import hashlib
    out, _ = demo
    m = json.loads((out / "manifest.json").read_text())
    for name, digest in m["files"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


def test_calibration_and_activation_levels(demo):
    _, res = demo
    states = [o["detail"]["state"] for o in res["derived"]
              if o["type"] == "stream_event" and o["event"] == "calibration_state"]
    assert states == ["capturing_relaxed", "relaxed_captured", "capturing_squeeze", "calibrated"]
    p = {ph["phase_s"]: ph for ph in res["manifest"]["phases"]}
    assert abs(p["8-11"]["activation_median"]) < 0.05 and abs(p["26-30"]["activation_median"]) < 0.05
    assert p["15-19"]["activation_median"] == pytest.approx(1.0, abs=0.1)
    assert 0.4 < p["19-26"]["activation_median"] < 0.8
    assert p["15-19"]["envelope_median"] > 8 * p["8-11"]["envelope_median"]


def test_vibration_changes_and_score_only_where_valid(demo):
    _, res = demo
    p = {ph["phase_s"]: ph for ph in res["manifest"]["phases"]}
    assert p["8-11"]["vibration_median_m_s2"] < 0.05
    assert p["11-15"]["vibration_median_m_s2"] == pytest.approx(0.5 * 9.80665 / 2 ** 0.5, rel=0.02)
    assert p["0-4"]["score_accumulated_au"] == 0.0           # uncalibrated: nothing accumulates
    assert p["8-11"]["score_accumulated_au"] < 1e-5          # no vibration -> ~0 by formula
    assert p["15-19"]["score_accumulated_au"] > 1.5 * p["11-15"]["score_accumulated_au"]  # activation weighting


def test_invalid_interval_pauses_score_and_recovers(demo):
    _, res = demo
    d = res["derived"]
    ev = [o for o in d if o["type"] == "stream_event"]
    kinds = [(e["event"], e["stream"]) for e in ev]
    assert ("device_sensor_state", "emg") in kinds and ("sample_gap", "emg") in kinds
    assert ("sample_gap", "imu") in kinds and ("seq_gap", None) in kinds
    assert cum_at(d, 20.5) == cum_at(d, 19.6)  # EMG disconnected: no accumulation
    assert cum_at(d, 22.55) == cum_at(d, 21.05)  # transport loss + re-warm-up: no accumulation
    later = [s for s in scores(d) if 23.0e6 < s["t_us"] < 25.9e6]
    assert later and all(s["status"] == "accumulating" for s in later)  # recovered
    final = scores(d)[-1]
    reasons = final["excluded_by_reason"]
    assert any("disconnected" in k for k in reasons) and any("gap" in k for k in reasons)


def test_summary_csv_shape(demo):
    out, _ = demo
    rows = (out / "summary.csv").read_text().splitlines()
    # rows every 0.5 s up to the last data (29.998 s): 0.5 .. 29.5
    assert rows[0].split(",") == list(CSV_COLUMNS) and len(rows) == 1 + 59


def test_demo_is_reproducible_with_fixed_identity(tmp_path):
    a = run_demo(tmp_path / "a", seed=1, run_id="same")["manifest"]["files"]
    b = run_demo(tmp_path / "b", seed=1, run_id="same")["manifest"]["files"]
    assert a == b


def test_replay_matches_original_and_preserves_identity(demo):
    out, res = demo
    r = replay_demo(out)
    assert r["matches_original"] is True
    assert r["recording_id"] == "sim-test-demo"
    replayed = lines(out / "replay_derived.ndjson")
    assert all(o["provenance"] == {"source": "replay", "original_source": "simulated",
                                   "recording_id": "sim-test-demo"} for o in replayed if o.get("provenance"))
    orig_t = [o["t_us"] for o in res["derived"]]
    assert [o["t_us"] for o in replayed] == orig_t  # recording time preserved


def test_replay_detects_tampering(demo, tmp_path):
    import shutil
    out, _ = demo
    copy = tmp_path / "copy"
    shutil.copytree(out, copy)
    d = lines(copy / "derived.ndjson")
    s = next(o for o in d if o["type"] == "experimental_score" and o["cumulative"]["value"] > 0)
    s["cumulative"]["value"] += 1
    (copy / "derived.ndjson").write_text("".join(json.dumps(o) + "\n" for o in d))
    assert replay_demo(copy)["matches_original"] is False


def test_vg_replay_rewrites_only_provenance_and_pacing_is_content_neutral(demo, tmp_path):
    out, _ = demo
    fast, paced = tmp_path / "fast.ndjson", tmp_path / "paced.ndjson"
    assert replay_main([str(out / "telemetry.ndjson"), "--fast", "-o", str(fast)]) == 0
    assert replay_main([str(out / "telemetry.ndjson"), "--speed", "200", "-o", str(paced)]) == 0
    assert fast.read_bytes() == paced.read_bytes()
    orig, rep = lines(out / "telemetry.ndjson"), lines(fast)
    assert len(orig) == len(rep)
    for a, b in zip(orig, rep):
        assert contract.validate_record(b) == []
        assert b["provenance"]["source"] == "replay" and b["provenance"]["original_source"] == "simulated"
        assert {k: v for k, v in a.items() if k != "provenance"} == {k: v for k, v in b.items() if k != "provenance"}


def test_replay_requires_identity_and_is_idempotent():
    rec = json.loads((ROOT / "contract/fixtures/valid/emg_batch_uniform.json").read_text())
    rec["provenance"] = {"source": "live"}
    with pytest.raises(ReplayError):
        to_replay(rec)
    once = to_replay(rec, recording_id="rec-1")
    assert once["provenance"] == {"source": "replay", "original_source": "live", "recording_id": "rec-1"}
    assert to_replay(once) == once


def test_cli_run_and_replay(tmp_path):
    out = tmp_path / "cli"
    run = subprocess.run([sys.executable, "-m", "haven_signals.demo_cli", "run", "--out", str(out),
                          "--run-id", "cli-test", "--quiet"], capture_output=True, text=True)
    assert run.returncode == 0 and "SIMULATED run saved" in run.stderr
    rep = subprocess.run([sys.executable, "-m", "haven_signals.demo_cli", "replay", str(out)],
                         capture_output=True, text=True)
    assert rep.returncode == 0 and "apart from provenance: YES" in rep.stdout


def test_embedding_example_runs():
    proc = subprocess.run([sys.executable, str(ROOT / "examples" / "embed_processor.py")],
                          capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert "final score:" in proc.stdout and "provenance=simulated" in proc.stdout
    assert "reloaded calibration state: calibrated" in proc.stdout
