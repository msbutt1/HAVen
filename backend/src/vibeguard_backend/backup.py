"""Known-good backup recordings: export a finished session, replay it as a labelled fallback.

A backup is a small folder you can commit and carry on the demo laptop:

    BACKUP.json          what it is (source, recording id, frame count, SHA-256 of every file)
    telemetry.ndjson     the device stream exactly as received
    commands.ndjson      calibration commands (and any pre-session calibration) to reissue
    manifest.json        processing config and session description (paths removed)

Replaying it streams ``provenance.source = "replay"`` with the original source and recording
id preserved, so a dashboard can never mistake it for live data. The files are verified
(hashes, every frame valid against the contract) on export and again before replay.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from vibeguard_signals import contract

from .adapters import ReplayAdapter
from .hub import utc_now_iso
from .service import Backend, _config_from_manifest, _read_commands, processing_config_for
from .runtime import RuntimeConflict

BACKUP_FILES = ("telemetry.ndjson", "commands.ndjson", "manifest.json")
BACKUP_SCHEMA = "vibeguard.backup"


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
                raise BackupError(f"{path.name} line {lineno} is not a valid contract record: "
                                  f"{issues[0] if issues else 'not an object'}")
            frames += 1
            if record["boot_id"] not in boots:
                boots.append(record["boot_id"])
            sources.add(record["provenance"]["source"])
    if frames == 0:
        raise BackupError(f"{path.name} is empty")
    return {"frames": frames, "boot_ids": boots, "sources": sorted(sources)}


def export_backup(backend: Backend, session_id: str, dest: str | Path, note: str | None = None) -> dict:
    """Copy a finished session into ``dest`` as a verified backup."""
    row = backend.session(session_id)
    if row["state"] == "recording":
        raise RuntimeConflict("the session is still recording; stop it first")
    src = Path(row["dir"])
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise BackupError(f"{dest} already exists and is not empty; refusing to overwrite a backup")
    for name in ("telemetry.ndjson", "commands.ndjson"):
        if not (src / name).is_file():
            raise BackupError(f"session {session_id} has no {name}")
    info = _check_telemetry(src / "telemetry.ndjson")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for name in ("telemetry.ndjson", "commands.ndjson"):
            shutil.copy2(src / name, dest / name)
        manifest = {}
        if (src / "manifest.json").is_file():
            manifest = json.loads((src / "manifest.json").read_text())
        session = dict(manifest.get("session") or row)
        session.pop("dir", None)
        session.pop("files", None)
        manifest = {"processing_config": manifest.get("processing_config"), "session": session,
                    "runtime_meta": manifest.get("runtime_meta"), "adapter": manifest.get("adapter")}
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
        source = row["original_source"] or row["source"]
        meta = {
            "schema": BACKUP_SCHEMA, "version": 1, "exported_at": utc_now_iso(),
            "from_session_id": session_id, "label": row["label"], "source": row["source"],
            "original_source": source, "recording_id": row["recording_id"],
            "frames": info["frames"], "boot_ids": info["boot_ids"],
            "calibration_id": row["calibration_id"], "score": (row.get("score") or {}).get("status"),
            "label_for_dashboard": ("SIMULATED recording" if source == "simulated" else
                                    "recording of LIVE device data, shown as REPLAY"),
            "note": note,
            "files": {n: {"bytes": (dest / n).stat().st_size, "sha256": _sha256(dest / n)}
                      for n in BACKUP_FILES},
        }
        (dest / "BACKUP.json").write_text(json.dumps(meta, indent=2) + "\n")
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return meta


def verify_backup(path: str | Path) -> dict:
    """Check a backup folder is complete, unmodified and replayable. Returns its BACKUP.json."""
    path = Path(path)
    meta_file = path / "BACKUP.json"
    if not meta_file.is_file():
        raise BackupError(f"{path} has no BACKUP.json")
    try:
        meta = json.loads(meta_file.read_text())
    except ValueError as exc:
        raise BackupError(f"BACKUP.json is not valid JSON: {exc}") from exc
    if meta.get("schema") != BACKUP_SCHEMA or meta.get("version") != 1:
        raise BackupError("BACKUP.json is not a version 1 VibeGuard backup")
    for name in BACKUP_FILES:
        f = path / name
        if not f.is_file():
            raise BackupError(f"{name} is missing")
        expected = (meta.get("files") or {}).get(name, {}).get("sha256")
        if expected != _sha256(f):
            raise BackupError(f"{name} was modified or damaged (hash mismatch)")
    info = _check_telemetry(path / "telemetry.ndjson")
    if info["frames"] != meta["frames"]:
        raise BackupError(f"frame count {info['frames']} does not match BACKUP.json ({meta['frames']})")
    return meta


def start_backup_replay(backend: Backend, path: str | Path, name: str = "wearable",
                        speed: float = 1.0, fast: bool = False):
    """Start the backup as a runtime (default name ``wearable``, so a dashboard that watches the
    wearable switches to the fallback without any change). Always labelled replay."""
    meta = verify_backup(path)
    path = Path(path)
    original = meta.get("original_source") or meta.get("source")
    config = processing_config_for("replay-sim" if original == "simulated" else "replay")
    config = _config_from_manifest(path / "manifest.json", config)
    adapter = ReplayAdapter(path / "telemetry.ndjson", recording_id=meta["recording_id"] or f"backup-{meta['from_session_id']}",
                            speed=speed, fast=fast)
    return backend.add_runtime(
        name, adapter, config, role="replay", restore_calibration=False,
        calibration_schedule=_read_commands(path / "commands.ndjson"),
        meta={"backup": str(path), "backup_label": meta.get("label_for_dashboard"),
              "recording_id": meta["recording_id"], "original_source": original,
              "speed": speed, "fast": fast, "exported_from": meta["from_session_id"]})
