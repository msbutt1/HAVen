"""Known-good backup recordings: export a finished session, replay it as a labelled fallback.

A backup is a small folder you can commit and carry on the demo laptop:

    BACKUP.json          what it is (source, recording id, frame count, SHA-256 of every file)
    telemetry.ndjson     the device stream exactly as received
    commands.ndjson      calibration commands (and any pre-session calibration) to reissue
    manifest.json        processing config and session description (paths removed)

Replaying it streams ``provenance.source = "replay"`` with the original source and recording
id preserved, so a dashboard can never mistake it for live data.

What ``verify_backup`` promises: the files are complete, parse, and match the hashes in
BACKUP.json. That catches accidental damage. BACKUP.json itself is not signed, so someone who
edits a file AND recomputes its hash passes; this is an integrity check, not authentication.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

from haven_signals import contract

from .adapters import ReplayAdapter
from .hub import utc_now_iso
from .runtime import RuntimeConflict
from .service import Backend, _config_from_manifest, _read_commands, processing_config_for

BACKUP_FILES = ("telemetry.ndjson", "commands.ndjson", "manifest.json")
BACKUP_SCHEMA = "vibeguard.backup"
COMMAND_ACTIONS = ("relaxed", "squeeze", "cancel", "load_calibration")


class BackupError(Exception):
    """The backup is missing, damaged or not usable."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_telemetry(path: Path) -> dict:
    """Every frame must be contract-valid; returns counts used in BACKUP.json."""
    frames = 0
    boots: list[str] = []
    sources: set[str] = set()
    with path.open("rb") as fh:
        for lineno, line in enumerate(fh, start=1):
            record, issues = contract.parse_line(line)
            if record is None or issues:
                raise BackupError(f"{path.name} line {lineno} is not a valid contract record"
                                  + (f": {issues[0]}" if issues else "") + (" (a torn last line from a crash?)"
                                                                           if not line.endswith(b"\n") else ""))
            frames += 1
            if record["boot_id"] not in boots:
                boots.append(record["boot_id"])
            sources.add(record["provenance"]["source"])
    if frames == 0:
        raise BackupError(f"{path.name} is empty")
    return {"frames": frames, "boot_ids": boots, "sources": sorted(sources)}


def _check_commands(path: Path) -> int:
    """Each line must be a JSON object with a known action (a torn line must not crash serve --backup)."""
    n = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cmd = json.loads(line)
        except ValueError as exc:
            raise BackupError(f"{path.name} line {lineno} is not valid JSON ({exc})") from exc
        if not isinstance(cmd, dict) or cmd.get("action") not in COMMAND_ACTIONS:
            raise BackupError(f"{path.name} line {lineno} is not a known calibration command")
        if cmd["action"] == "load_calibration" and not isinstance(cmd.get("record"), dict):
            raise BackupError(f"{path.name} line {lineno}: load_calibration has no calibration record")
        n += 1
    return n


def _check_manifest(path: Path) -> dict:
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BackupError(f"{path.name} is not valid JSON ({exc})") from exc
    if not isinstance(m, dict):
        raise BackupError(f"{path.name} is not a JSON object")
    return m


def export_backup(backend: Backend, session_id: str, dest: str | Path, note: str | None = None) -> dict:
    """Copy a finished session into ``dest`` as a verified backup.

    The copy is built in a temporary sibling folder and renamed into place only when complete, so an
    interrupted export never leaves a half-written backup behind."""
    row = backend.session(session_id)
    if row["state"] == "recording":
        raise RuntimeConflict("the session is still recording; stop it first (if no server is running, "
                              "it was cut off by a crash: start the server once to mark it interrupted)")
    src = Path(row["dir"])
    dest = Path(dest)
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise BackupError(f"{dest} already exists and is not empty (or is not a folder); refusing to overwrite a backup")
    for name in ("telemetry.ndjson", "commands.ndjson"):
        if not (src / name).is_file():
            raise BackupError(f"session {session_id} has no {name}")
    # The store recorded a hash when the session was stopped: a damaged recording must not be
    # turned into a "known good" backup.
    for name, info in (row.get("files") or {}).items():
        f = src / name
        if name in BACKUP_FILES and f.is_file() and (f.stat().st_size != info["bytes"] or _sha256(f) != info["sha256"]):
            raise BackupError(f"{name} of session {session_id} no longer matches the hash recorded when "
                              f"it stopped; it was modified or damaged")
    info = _check_telemetry(src / "telemetry.ndjson")
    _check_commands(src / "commands.ndjson")
    interrupted = row["state"] == "interrupted"

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.partial-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        for name in ("telemetry.ndjson", "commands.ndjson"):
            shutil.copy2(src / name, tmp / name)
        manifest: dict = {}
        if (src / "manifest.json").is_file():
            manifest = _check_manifest(src / "manifest.json")
        session = dict(manifest.get("session") or row)
        session.pop("dir", None)
        session.pop("files", None)
        manifest = {"processing_config": manifest.get("processing_config"), "session": session,
                    "runtime_meta": manifest.get("runtime_meta"), "adapter": manifest.get("adapter")}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
        source = row["original_source"] or row["source"]
        meta = {
            "schema": BACKUP_SCHEMA, "version": 1, "exported_at": utc_now_iso(),
            "from_session_id": session_id, "label": row["label"], "source": row["source"],
            "original_source": source, "recording_id": row["recording_id"],
            "frames": info["frames"], "boot_ids": info["boot_ids"],
            "calibration_id": row["calibration_id"], "score": (row.get("score") or {}).get("status"),
            "interrupted": interrupted,
            "label_for_dashboard": ("SIMULATED recording" if source == "simulated" else
                                    "recording of LIVE device data, shown as REPLAY"),
            "note": note,
            "files": {n: {"bytes": (tmp / n).stat().st_size, "sha256": _sha256(tmp / n)}
                      for n in BACKUP_FILES},
        }
        (tmp / "BACKUP.json").write_text(json.dumps(meta, indent=2) + "\n")
        if dest.exists():
            dest.rmdir()  # an existing empty folder
        tmp.rename(dest)
    except BaseException:  # includes Ctrl-C: never leave a partial backup
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return meta


def verify_backup(path: str | Path) -> dict:
    """Check a backup folder is complete, parses, and matches its recorded hashes."""
    path = Path(path)
    meta_file = path / "BACKUP.json"
    if not meta_file.is_file():
        raise BackupError(f"{path} has no BACKUP.json")
    try:
        meta = json.loads(meta_file.read_text())
    except ValueError as exc:
        raise BackupError(f"BACKUP.json is not valid JSON: {exc}") from exc
    if not isinstance(meta, dict) or meta.get("schema") != BACKUP_SCHEMA or meta.get("version") != 1:
        raise BackupError("BACKUP.json is not a version 1 HAVen backup")
    for key, kind in (("frames", int), ("files", dict), ("from_session_id", str), ("label_for_dashboard", str)):
        if not isinstance(meta.get(key), kind) or isinstance(meta.get(key), bool):
            raise BackupError(f"BACKUP.json is missing or has a wrong value for '{key}'")
    for name in BACKUP_FILES:
        f = path / name
        if not f.is_file():
            raise BackupError(f"{name} is missing")
        entry = meta["files"].get(name)
        expected = entry.get("sha256") if isinstance(entry, dict) else None
        if expected != _sha256(f):
            raise BackupError(f"{name} was modified or damaged (hash mismatch)")
    info = _check_telemetry(path / "telemetry.ndjson")
    if info["frames"] != meta["frames"]:
        raise BackupError(f"frame count {info['frames']} does not match BACKUP.json ({meta['frames']})")
    _check_commands(path / "commands.ndjson")
    manifest = _check_manifest(path / "manifest.json")
    if not isinstance(manifest.get("processing_config"), dict):
        meta["warnings"] = ["manifest.json has no processing_config: the replay uses defaults, so vibration "
                            "may be in raw counts and the score unavailable"]
    return meta


def start_backup_replay(backend: Backend, path: str | Path, name: str = "wearable",
                        speed: float = 1.0, fast: bool = False, extra_meta: dict | None = None):
    """Start the backup as a runtime (default name ``wearable``, so a dashboard that watches the
    wearable switches to the fallback without any change). Always labelled replay."""
    meta = verify_backup(path)
    path = Path(path)
    original = meta.get("original_source") or meta.get("source")
    config = processing_config_for("replay-sim" if original == "simulated" else "replay")
    config = _config_from_manifest(path / "manifest.json", config)
    hw_meta = {}  # which hardware the recording came from (the profile stored with the session)
    rm = (_check_manifest(path / "manifest.json").get("runtime_meta") or {})
    for k in ("hardware_profile", "hardware_profile_name", "effective_serial"):
        if rm.get(k) is not None:
            hw_meta[k] = rm[k]
    adapter = ReplayAdapter(path / "telemetry.ndjson",
                            recording_id=meta.get("recording_id") or f"backup-{meta['from_session_id']}",
                            speed=speed, fast=fast)
    return backend.add_runtime(
        name, adapter, config, role="replay", restore_calibration=False,
        calibration_schedule=_read_commands(path / "commands.ndjson"),
        meta={"backup": str(path.resolve()), "backup_label": meta["label_for_dashboard"],
              "recording_id": meta.get("recording_id"), "original_source": original,
              "speed": speed, "fast": fast, "exported_from": meta["from_session_id"],
              "interrupted_recording": bool(meta.get("interrupted")), **hw_meta, **(extra_meta or {})})
