"""Persistent records in one local SQLite file (no cloud services).

Tables:
- devices              every device_id seen, last boot, last hello
- wearable_sessions    recorded wearable sessions (files live in data/sessions/<id>/)
- calibrations         every calibration record the processor produced
- station_sessions     perception-test sessions
- station_trials       one row per stimulus presentation
- idempotency          request_id -> stored response, so repeated commands are safe

Provenance is a first-class column everywhere: ``live`` / ``simulated`` / ``replay``
plus ``original_source`` and ``recording_id`` for replays. Simulated history rows
also carry ``simulated_history = 1``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .hub import utc_now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id      TEXT PRIMARY KEY,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    last_boot_id   TEXT,
    last_source    TEXT,
    hello_json     TEXT
);
CREATE TABLE IF NOT EXISTS wearable_sessions (
    session_id        TEXT PRIMARY KEY,
    runtime           TEXT NOT NULL,
    device_id         TEXT,
    state             TEXT NOT NULL CHECK (state IN ('recording','completed','interrupted')),
    end_reason        TEXT,
    label             TEXT,
    source            TEXT,
    original_source   TEXT,
    recording_id      TEXT,
    simulated_history INTEGER NOT NULL DEFAULT 0,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    boot_ids_json     TEXT NOT NULL DEFAULT '[]',
    calibration_id    TEXT,
    score_json        TEXT,
    counters_json     TEXT,
    files_json        TEXT,
    dir               TEXT
);
CREATE INDEX IF NOT EXISTS wearable_sessions_started ON wearable_sessions(started_at);
CREATE TABLE IF NOT EXISTS calibrations (
    calibration_id TEXT PRIMARY KEY,
    device_id      TEXT,
    source         TEXT,
    created_at     TEXT NOT NULL,
    session_id     TEXT,
    record_json    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station_sessions (
    station_session_id TEXT PRIMARY KEY,
    state              TEXT NOT NULL CHECK (state IN ('testing','completed','aborted','interrupted')),
    label              TEXT,
    source             TEXT NOT NULL CHECK (source IN ('live','simulated','replay')),
    simulated_history  INTEGER NOT NULL DEFAULT 0,
    wearable_session_id TEXT,
    protocol_json      TEXT,
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    end_reason         TEXT,
    summary_json       TEXT
);
CREATE TABLE IF NOT EXISTS station_trials (
    trial_id            TEXT PRIMARY KEY,
    station_session_id  TEXT NOT NULL REFERENCES station_sessions(station_session_id),
    trial_index         INTEGER NOT NULL,
    stimulus_json       TEXT NOT NULL,
    measured_amplitude_json TEXT,
    is_catch            INTEGER NOT NULL DEFAULT 0,
    response            TEXT NOT NULL CHECK (response IN ('felt','not_felt','missing')),
    presented_at        TEXT,
    responded_at        TEXT,
    response_latency_ms REAL,
    recorded_at         TEXT NOT NULL,
    note                TEXT,
    UNIQUE (station_session_id, trial_index)
);
CREATE TABLE IF NOT EXISTS idempotency (
    request_id    TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""

_JSON_COLS = {"boot_ids_json", "score_json", "counters_json", "files_json", "hello_json",
              "record_json", "protocol_json", "summary_json", "stimulus_json",
              "measured_amplitude_json"}


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if k in _JSON_COLS:
            out[k[:-5]] = None if v is None else json.loads(v)
        elif k in ("simulated_history", "is_catch"):
            out[k] = bool(v)
        else:
            out[k] = v
    return out


class IdempotencyConflict(Exception):
    """A request_id was reused for a different kind of command."""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def _one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self._lock:
            return _row(self._db.execute(sql, params).fetchone())

    def _all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, params).fetchall()]  # type: ignore[misc]

    # ---------------------------------------------------------------- recovery

    def recover_after_restart(self) -> dict:
        """Anything still 'recording'/'testing' was cut off by a crash or kill."""
        now = utc_now_iso()
        with self._lock:
            w = self._db.execute(
                "UPDATE wearable_sessions SET state='interrupted', end_reason='backend_restart', "
                "ended_at=COALESCE(ended_at, ?) WHERE state='recording'", (now,)).rowcount
            s = self._db.execute(
                "UPDATE station_sessions SET state='interrupted', end_reason='backend_restart', "
                "ended_at=COALESCE(ended_at, ?) WHERE state='testing'", (now,)).rowcount
        return {"wearable_sessions_interrupted": w, "station_sessions_interrupted": s}

    # ---------------------------------------------------------------- idempotency

    def idem_get(self, request_id: str | None, kind: str) -> Any | None:
        if not request_id:
            return None
        row = self._one("SELECT kind, response_json FROM idempotency WHERE request_id=?", (request_id,))
        if row is None:
            return None
        if row["kind"] != kind:
            raise IdempotencyConflict(
                f"request_id {request_id!r} was already used for {row['kind']!r}")
        return json.loads(row["response_json"])

    def prune_idempotency(self, older_than_s: float) -> int:
        """Forget request ids older than the retention window (the table would grow forever)."""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_s)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        return self._exec("DELETE FROM idempotency WHERE created_at < ?", (cutoff,)).rowcount

    def idem_put(self, request_id: str | None, kind: str, response: Any) -> None:
        if not request_id:
            return
        self._exec("INSERT OR IGNORE INTO idempotency VALUES (?,?,?,?)",
                   (request_id, kind, json.dumps(response), utc_now_iso()))

    # ---------------------------------------------------------------- devices

    def touch_device(self, device_id: str, boot_id: str | None, source: str | None,
                     hello: dict | None = None) -> None:
        now = utc_now_iso()
        with self._lock:
            self._db.execute(
                "INSERT INTO devices (device_id, first_seen, last_seen, last_boot_id, last_source, hello_json) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET last_seen=excluded.last_seen, "
                "last_boot_id=excluded.last_boot_id, last_source=excluded.last_source, "
                "hello_json=COALESCE(excluded.hello_json, devices.hello_json)",
                (device_id, now, now, boot_id, source, None if hello is None else json.dumps(hello)))

    def devices(self) -> list[dict]:
        return self._all("SELECT * FROM devices ORDER BY last_seen DESC")

    # ---------------------------------------------------------------- wearable sessions

    def create_wearable_session(self, row: dict) -> dict:
        cols = ("session_id", "runtime", "device_id", "state", "label", "source", "original_source",
                "recording_id", "simulated_history", "started_at", "dir", "boot_ids_json")
        vals = {**{c: None for c in cols}, **row}
        vals["simulated_history"] = int(bool(vals["simulated_history"]))
        vals["boot_ids_json"] = json.dumps(row.get("boot_ids", []))
        self._exec(f"INSERT INTO wearable_sessions ({','.join(cols)}) VALUES "
                   f"({','.join(':' + c for c in cols)})", vals)
        return self.wearable_session(row["session_id"])  # type: ignore[return-value]

    def update_wearable_session(self, session_id: str, **fields: Any) -> dict | None:
        sets, params = [], {"session_id": session_id}
        for k, v in fields.items():
            col = f"{k}_json" if f"{k}_json" in _JSON_COLS else k
            sets.append(f"{col}=:{col}")
            params[col] = json.dumps(v) if col in _JSON_COLS else v
        if sets:
            self._exec(f"UPDATE wearable_sessions SET {', '.join(sets)} WHERE session_id=:session_id", params)
        return self.wearable_session(session_id)

    def wearable_session(self, session_id: str) -> dict | None:
        return self._one("SELECT * FROM wearable_sessions WHERE session_id=?", (session_id,))

    def has_simulated_history(self, recording_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM wearable_sessions WHERE recording_id=? AND simulated_history=1",
                         (recording_id,)) is not None

    def wearable_sessions(self, source: str | None = None, state: str | None = None,
                          limit: int = 200) -> list[dict]:
        sql, params = "SELECT * FROM wearable_sessions WHERE 1=1", []
        if source:
            sql += " AND source=?"
            params.append(source)
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        return self._all(sql, tuple(params))

    # ---------------------------------------------------------------- calibrations

    def save_calibration(self, record: dict, device_id: str | None, source: str | None,
                         session_id: str | None) -> None:
        self._exec("INSERT OR IGNORE INTO calibrations VALUES (?,?,?,?,?,?)",
                   (record["calibration_id"], device_id, source, utc_now_iso(), session_id,
                    json.dumps(record)))

    def latest_calibration(self, device_id: str, source: str | None = None) -> dict | None:
        sql, params = "SELECT * FROM calibrations WHERE device_id=?", [device_id]
        if source:
            sql += " AND source=?"
            params.append(source)
        return self._one(sql + " ORDER BY created_at DESC, rowid DESC LIMIT 1", tuple(params))

    def calibrations(self, device_id: str | None = None, limit: int = 50) -> list[dict]:
        if device_id:
            return self._all("SELECT * FROM calibrations WHERE device_id=? ORDER BY created_at DESC LIMIT ?",
                             (device_id, limit))
        return self._all("SELECT * FROM calibrations ORDER BY created_at DESC LIMIT ?", (limit,))

    # ---------------------------------------------------------------- station

    def create_station_session(self, row: dict) -> dict:
        self._exec(
            "INSERT INTO station_sessions (station_session_id, state, label, source, simulated_history, "
            "wearable_session_id, protocol_json, started_at) VALUES (?,?,?,?,?,?,?,?)",
            (row["station_session_id"], "testing", row.get("label"), row["source"],
             int(bool(row.get("simulated_history"))), row.get("wearable_session_id"),
             json.dumps(row.get("protocol")), row.get("started_at") or utc_now_iso()))
        return self.station_session(row["station_session_id"])  # type: ignore[return-value]

    def station_session(self, sid: str, with_trials: bool = True) -> dict | None:
        s = self._one("SELECT * FROM station_sessions WHERE station_session_id=?", (sid,))
        if s is not None and with_trials:
            s["trials"] = self.station_trials(sid)
        return s

    def station_sessions(self, source: str | None = None, limit: int = 200) -> list[dict]:
        if source:
            rows = self._all("SELECT * FROM station_sessions WHERE source=? ORDER BY started_at DESC LIMIT ?",
                             (source, limit))
        else:
            rows = self._all("SELECT * FROM station_sessions ORDER BY started_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["trial_count"] = self._one(
                "SELECT COUNT(*) AS n FROM station_trials WHERE station_session_id=?",
                (r["station_session_id"],))["n"]  # type: ignore[index]
        return rows

    def finish_station_session(self, sid: str, state: str, reason: str | None,
                               summary: dict | None, ended_at: str | None = None) -> dict | None:
        self._exec("UPDATE station_sessions SET state=?, end_reason=?, ended_at=?, summary_json=? "
                   "WHERE station_session_id=? AND state='testing'",
                   (state, reason, ended_at or utc_now_iso(), json.dumps(summary), sid))
        return self.station_session(sid)

    def add_station_trial(self, row: dict) -> dict:
        self._exec(
            "INSERT INTO station_trials (trial_id, station_session_id, trial_index, stimulus_json, "
            "measured_amplitude_json, is_catch, response, presented_at, responded_at, "
            "response_latency_ms, recorded_at, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["trial_id"], row["station_session_id"], row["trial_index"],
             json.dumps(row["stimulus"]), None if row.get("measured_amplitude") is None
             else json.dumps(row["measured_amplitude"]), int(bool(row.get("is_catch"))),
             row["response"], row.get("presented_at"), row.get("responded_at"),
             row.get("response_latency_ms"), utc_now_iso(), row.get("note")))
        return self._one("SELECT * FROM station_trials WHERE trial_id=?", (row["trial_id"],))  # type: ignore[return-value]

    def station_trials(self, sid: str) -> list[dict]:
        return self._all("SELECT * FROM station_trials WHERE station_session_id=? ORDER BY trial_index",
                         (sid,))

    def next_trial_index(self, sid: str) -> int:
        r = self._one("SELECT COALESCE(MAX(trial_index), -1) + 1 AS n FROM station_trials "
                      "WHERE station_session_id=?", (sid,))
        return r["n"]  # type: ignore[index]
