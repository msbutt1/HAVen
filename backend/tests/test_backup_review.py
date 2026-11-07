"""Review round 3: backup / preflight / CLI defects. Each xfail(strict) test documents a real bug;
when the bug is fixed the test XPASSes and strict mode forces the marker to be removed.
SIMULATED data only, offline, no fixed ports."""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import ScriptedAdapter, sim_frames, wait_until

from vibeguard_backend import cli
from vibeguard_backend.adapters import ReplayAdapter
from vibeguard_backend.backup import BackupError, export_backup, start_backup_replay, verify_backup
from vibeguard_backend.preflight import FAIL, PASS, run_preflight
from vibeguard_backend.service import processing_config_for


@pytest.fixture
def session(backend):
    frames = sim_frames(duration_s=10)
    gate = threading.Event()
    rt = backend.add_runtime("wearable", ScriptedAdapter([*frames[:5], ("gate", gate), *frames[5:60]],
                                                         clock="device", hold_open=True),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == 5)
    s = backend.start_session("wearable", "review")
    gate.set()
    assert wait_until(lambda: rt.counters["records"] == 60)
    return backend.stop_session(s["session_id"])


# ---------------------------------------------------------------- export


def test_export_rejects_a_session_whose_files_no_longer_match_the_recorded_hashes(backend, session, tmp_path):
    tel = Path(session["dir"]) / "telemetry.ndjson"
    lines = tel.read_bytes().splitlines(keepends=True)
    tel.write_bytes(b"".join(lines[:-20]))  # still 100% contract-valid, just shorter than what was recorded
    with pytest.raises(BackupError):
        export_backup(backend, session["session_id"], tmp_path / "bk")


def test_a_backup_that_verifies_must_be_replayable_even_with_a_torn_commands_file(backend, session, tmp_path):
    # what a crash mid-write leaves behind in an interrupted session (no recorded file hashes then)
    with (Path(session["dir"]) / "commands.ndjson").open("ab") as fh:
        fh.write(b'{"action": "rela')
    backend.store.update_wearable_session(session["session_id"], state="interrupted", files=None)
    try:
        export_backup(backend, session["session_id"], tmp_path / "bk")
        verify_backup(tmp_path / "bk")
    except BackupError:
        return  # refusing is acceptable; "verified OK, then cannot start" is not
    backend.remove_runtime("wearable")
    start_backup_replay(backend, tmp_path / "bk")


def test_export_to_a_path_that_is_a_file_is_a_clean_error(backend, session, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(BackupError):
        export_backup(backend, session["session_id"], f)


def test_interrupted_export_leaves_nothing_behind(backend, session, tmp_path, monkeypatch):
    real = shutil.copy2

    def copy(src, dst, *a, **k):
        real(src, dst, *a, **k)
        if str(dst).endswith("commands.ndjson"):
            raise KeyboardInterrupt
    monkeypatch.setattr(shutil, "copy2", copy)
    with pytest.raises(KeyboardInterrupt):
        export_backup(backend, session["session_id"], tmp_path / "bk")
    assert not (tmp_path / "bk").exists() or not any((tmp_path / "bk").iterdir())


def test_export_cli_with_a_wrong_data_dir_does_not_create_it(tmp_path, capsys):
    rc = cli.main(["export-backup", "--data-dir", str(tmp_path / "typo"), "ws-x", str(tmp_path / "bk")])
    assert rc == 1
    assert not (tmp_path / "typo").exists()


# ---------------------------------------------------------------- verify


def _edit_meta(path: Path, fn):
    meta = json.loads((path / "BACKUP.json").read_text())
    out = fn(meta)
    (path / "BACKUP.json").write_text(json.dumps(meta if out is None else out))


@pytest.mark.parametrize("damage", ["not_an_object", "no_frames_key", "no_recording_id_key"])
def test_verify_backup_reports_malformed_metadata_as_backup_error(backend, session, tmp_path, damage):
    export_backup(backend, session["session_id"], tmp_path / "bk")
    d = tmp_path / "bk"
    if damage == "not_an_object":
        _edit_meta(d, lambda m: [])
    elif damage == "no_frames_key":
        _edit_meta(d, lambda m: m.pop("frames"))
    else:
        _edit_meta(d, lambda m: m.pop("recording_id"))
    try:
        verify_backup(d)
    except BackupError:
        return
    backend.remove_runtime("wearable")
    start_backup_replay(backend, d)  # verified OK, so it must start


# ---------------------------------------------------------------- serve --backup


@pytest.mark.parametrize("speed", ["nan", "inf"])
def test_serve_backup_refuses_a_non_finite_speed(backend, session, tmp_path, monkeypatch, speed):
    export_backup(backend, session["session_id"], tmp_path / "bk")
    import vibeguard_backend.app as appmod

    captured = []
    real_create = appmod.create_app
    monkeypatch.setattr(appmod, "create_app", lambda b, **kw: (captured.append(b), real_create(b, **kw))[1])
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    try:
        rc = cli.main(["serve", "--data-dir", str(tmp_path / "d2"), "--backup", str(tmp_path / "bk"),
                       "--speed", speed, "--http-port", "1"])
    finally:
        for b in captured:
            b.shutdown()
    assert rc == 2


def test_finished_backup_replay_is_not_reported_as_fresh(backend, session, tmp_path):
    export_backup(backend, session["session_id"], tmp_path / "bk")
    backend.remove_runtime("wearable")
    rt = start_backup_replay(backend, tmp_path / "bk", speed=1000.0)
    assert wait_until(lambda: rt.status()["state"] == "completed", 20)
    assert rt.status()["data_fresh"] is False


# ---------------------------------------------------------------- preflight against a fake server


class _Fake:
    def __init__(self, routes):
        self.routes = routes
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                body = outer.routes.get(path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _runtime(state="ready", frames=100):
    return {"runtime": "wearable", "state": state, "connection": "connected", "connection_detail": "",
            "error": None, "counters": {"frames": frames}, "events": {}, "last_rx_age_s": 0.1,
            "provenance": {"source": "replay", "original_source": "simulated"},
            "calibration": {"state": "uncalibrated", "reason": None, "calibration_id": None}}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "_opener", None)  # urlopen caches proxy settings in a global opener
    made = []

    def make(**over):
        routes = {"/api/health": {"version": "t", "data_dir": str(tmp_path), "recovered_on_start": {}},
                  "/api/runtimes/wearable": _runtime(), "/api/sessions": [],
                  "/api/history": {"items": []}}
        routes.update(over)
        f = _Fake(routes)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def _check(rep, name_part):
    return next(c for c in rep.checks if name_part in c.name)


def test_preflight_talks_to_localhost_directly_even_when_a_proxy_is_configured(fake, monkeypatch):
    f = fake()
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    rep = run_preflight(f.url, backup=None)
    assert _check(rep, "backend reachable").level == PASS


def test_preflight_with_an_odd_runtime_name_reports_instead_of_crashing(fake):
    f = fake()
    rep = run_preflight(f.url, runtime="a b")
    assert rep.failed


def test_preflight_does_not_claim_sha256_verification_when_nothing_was_hashed(fake):
    row = {"session_id": "ws-x", "state": "completed", "source": "simulated", "files": None}
    f = fake(**{"/api/sessions": [row]})
    rep = run_preflight(f.url)
    c = _check(rep, "saved sessions")
    assert not (c.level == PASS and "SHA-256" in c.detail)


def test_preflight_message_for_a_finished_replay_does_not_blame_the_usb_cable(fake):
    f = fake(**{"/api/runtimes/wearable": _runtime("completed", frames=362)})
    rep = run_preflight(f.url, expect="replay")
    c = _check(rep, "device connected")
    assert c.level == FAIL
    assert "USB" not in c.fix and "completed" in c.detail and c.detail.strip() != "state completed:"
