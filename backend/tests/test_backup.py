"""Known-good backup recordings: export, verify, tamper detection, labelled replay. SIMULATED data."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
from conftest import ScriptedAdapter, sim_frames, wait_until

from vibeguard_backend.backup import BackupError, export_backup, start_backup_replay, verify_backup
from vibeguard_backend.runtime import RuntimeConflict
from vibeguard_backend.service import processing_config_for


def _index_at(frames, seconds):
    for i, f in enumerate(frames):
        t = json.loads(f).get("timing")
        if t and (t["t0_us"] if t["mode"] == "uniform" else t["t_us"][0]) / 1e6 >= seconds:
            return i
    return len(frames)


@pytest.fixture
def session(backend):
    """A completed, calibrated simulated session (calibration commands recorded inside it)."""
    frames = sim_frames(duration_s=30)
    i0, i_rel, i_sq = _index_at(frames, 0.2), _index_at(frames, 0.5), _index_at(frames, 4.5)
    g0, g1, g2 = threading.Event(), threading.Event(), threading.Event()
    script = [*frames[:i0], ("gate", g0), *frames[i0:i_rel], ("gate", g1), *frames[i_rel:i_sq],
              ("gate", g2), *frames[i_sq:]]
    rt = backend.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == i0)
    s = backend.start_session("wearable", "known good")
    g0.set()
    assert wait_until(lambda: rt.counters["records"] == i_rel)
    backend.calibrate("wearable", "relaxed")
    g1.set()
    assert wait_until(lambda: rt.counters["records"] == i_sq)
    backend.calibrate("wearable", "squeeze")
    g2.set()
    assert wait_until(lambda: rt.counters["records"] == len(frames), 20)
    return backend.stop_session(s["session_id"])


def test_export_writes_a_verified_labelled_backup(backend, session, tmp_path):
    meta = export_backup(backend, session["session_id"], tmp_path / "bk", note="unit test")
    assert meta["original_source"] == "simulated" and meta["label_for_dashboard"] == "SIMULATED recording"
    assert meta["frames"] == session["counters"]["frames"] and set(meta["files"]) == {
        "telemetry.ndjson", "commands.ndjson", "manifest.json"}
    assert verify_backup(tmp_path / "bk")["recording_id"] == session["recording_id"]
    manifest = json.loads((tmp_path / "bk" / "manifest.json").read_text())
    assert "dir" not in manifest["session"] and "files" not in manifest["session"]  # no machine paths
    with pytest.raises(BackupError, match="not empty"):  # never overwrite a known-good backup
        export_backup(backend, session["session_id"], tmp_path / "bk")


def test_export_refuses_a_session_that_is_still_recording(backend, tmp_path):
    frames = sim_frames(duration_s=5)
    rt = backend.add_runtime("wearable", ScriptedAdapter(frames[:20], clock="device", hold_open=True),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == 20)
    s = backend.start_session("wearable")
    with pytest.raises(RuntimeConflict):
        export_backup(backend, s["session_id"], tmp_path / "bk")
    assert not (tmp_path / "bk").exists()


@pytest.mark.parametrize("damage", ["flip_byte", "delete_file", "truncate", "bad_frame", "no_meta"])
def test_damaged_or_incomplete_backups_are_refused(backend, session, tmp_path, damage):
    d = tmp_path / "bk"
    export_backup(backend, session["session_id"], d)
    tel = d / "telemetry.ndjson"
    if damage == "flip_byte":
        data = bytearray(tel.read_bytes()); data[100] ^= 1; tel.write_bytes(bytes(data))
    elif damage == "delete_file":
        (d / "commands.ndjson").unlink()
    elif damage == "truncate":
        tel.write_bytes(tel.read_bytes()[:-500])
    elif damage == "bad_frame":
        meta = json.loads((d / "BACKUP.json").read_text())
        tel.write_bytes(b"not json\n" + tel.read_bytes())
        import hashlib
        meta["files"]["telemetry.ndjson"]["sha256"] = hashlib.sha256(tel.read_bytes()).hexdigest()
        (d / "BACKUP.json").write_text(json.dumps(meta))  # hashes consistent: the frame check must catch it
    elif damage == "no_meta":
        (d / "BACKUP.json").unlink()
    with pytest.raises(BackupError):
        verify_backup(d)
    with pytest.raises(BackupError):
        start_backup_replay(backend, d, name="x")


def test_backup_replays_labelled_as_replay_and_reproduces_the_score(backend, session, tmp_path):
    d = tmp_path / "bk"
    export_backup(backend, session["session_id"], d)
    backend.runtimes["wearable"].stop()
    backend.remove_runtime("wearable")
    shutil.rmtree(Path(session["dir"]))  # the backup must not depend on the original session files
    rt = start_backup_replay(backend, d, name="wearable", fast=True)
    assert wait_until(lambda: rt.finished, 20)
    assert rt.error is None and rt.role == "replay"
    prov = rt.provenance
    assert prov["source"] == "replay" and prov["original_source"] == "simulated"
    assert prov["recording_id"] == session["recording_id"]
    assert rt.counters["frames"] == session["counters"]["frames"]
    assert rt.calibration_state in ("calibrated", "invalidated")
    assert rt.latest["score"]["cumulative"]["value"] == pytest.approx(
        session["score"]["at_end"]["cumulative_au"], abs=1e-4)
    assert rt.meta["backup_label"] == "SIMULATED recording"
