"""Local result store: one JSON file per session, written at most once.

This is the station's stand-in until the backend (Abduljabbar) owns persistence;
the engine only needs a callable ``sink(result_dict)``, so the backend can pass
its own. Used as a sink, ``ResultStore`` raises ``AlreadySavedError`` when the
session id already has a result; the engine reports that and never overwrites.
``save`` is the same operation returning a bool.

File names escape ``:`` reversibly and carry a short hash of the exact id, so ids
that differ only by case or by ``:`` versus ``_`` can never share a file (Windows
file names are case-insensitive). Files are written to a temporary name and then
hard-linked into place, so a crash cannot leave a truncated file that blocks a
later save.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SUFFIX = ".station_result.json"


class AlreadySavedError(Exception):
    """This session id already has a saved result; it was left untouched."""


def result_json(result: dict) -> str:
    """Deterministic, human-readable JSON (strict: no NaN/Infinity)."""
    return json.dumps(result, indent=2, sort_keys=False, allow_nan=False, ensure_ascii=False) + "\n"


class ResultStore:
    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)

    def path_for(self, session_id: str) -> Path:
        if not _ID.match(session_id):
            raise ValueError("invalid session_id")
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]
        # '.'-led names are fine; the hash also keeps reserved Windows names (CON, NUL, ...) harmless.
        return self.dir / f"{session_id.replace(':', '%3A')}.{digest}{_SUFFIX}"

    def save(self, result: dict) -> bool:
        """Write the result if this session has none. True = saved now, False = already saved."""
        path = self.path_for(result["session_id"])
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = result_json(result)  # serialise first: a bad result must not leave a stub file
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, path)  # fails if the target exists: never overwrites
            except FileExistsError:
                return False
            return True
        finally:
            Path(tmp).unlink(missing_ok=True)

    def __call__(self, result: dict) -> None:
        """Engine sink: raises ``AlreadySavedError`` instead of overwriting."""
        if not self.save(result):
            raise AlreadySavedError(result["session_id"])

    def exists(self, session_id: str) -> bool:
        return self.path_for(session_id).exists()

    def load(self, session_id: str) -> dict:
        return json.loads(self.path_for(session_id).read_text(encoding="utf-8"))

    def list_sessions(self) -> list[str]:
        if not self.dir.exists():
            return []
        ids = []
        for p in self.dir.glob(f"*{_SUFFIX}"):
            ids.append(json.loads(p.read_text(encoding="utf-8"))["session_id"])
        return sorted(ids)
