"""Backend service: owns the store, the hub and every DeviceRuntime.

The HTTP layer (app.py) is a thin wrapper over this class, and the tests drive
it directly. Every state-changing command accepts an optional ``request_id``:
repeating a command with the same id (and the same target) returns the first
response and changes nothing, so a retried click or a resent request never
duplicates a session. Reusing an id for a different command or target is a 409,
never another record's result.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from vibeguard_signals.demo import demo_processing_config
from vibeguard_signals.processing import ProcessingConfig, StreamConfig

from .adapters import DeviceAdapter, ReplayAdapter, new_id
from .hub import Hub, utc_now_iso
from .runtime import DeviceRuntime, RuntimeConflict, RuntimeGone, file_info
from .store import IdempotencyConflict, Store

try:  # POSIX only; the demo machines are macOS/Linux
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("vibeguard.service")

IDEMPOTENCY_RETENTION_S = 48 * 3600  # a retry after two days is a new command
RESPONSES = ("felt", "not_felt", "missing")
SOURCES = ("live", "simulated", "replay")
MAX_TEXT = 500
MAX_ID = 128


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


class DataDirInUse(Exception):
    """Another backend process owns this data directory."""


def _text(value: Any, name: str, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest(f"{name} must be a string")
    if len(value) > limit:
        raise BadRequest(f"{name} is longer than {limit} characters")
    return value


def _request_id(value: Any) -> str | None:
    rid = _text(value, "request_id", MAX_ID)
    if rid is not None and not rid:
        raise BadRequest("request_id must not be empty")
    return rid


def _number(value: Any, name: str, *, minimum: float | None = None, exclusive: bool = False,
            maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRequest(f"{name} must be a number")
    try:
        v = float(value)
    except OverflowError:
        raise BadRequest(f"{name} is too large") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise BadRequest(f"{name} must be finite")
    if minimum is not None and (v <= minimum if exclusive else v < minimum):
        raise BadRequest(f"{name} must be {'>' if exclusive else '>='} {minimum:g}")
    if maximum is not None and v > maximum:
        raise BadRequest(f"{name} must be <= {maximum:g}")
    return v


MAX_JSON_BYTES = 16_384
MAX_JSON_DEPTH = 8
MAX_TRIAL_INDEX = 1_000_000


def _clean_json(value: Any, name: str) -> Any:
    """Reject nested values that cannot be stored or served safely.

    Strings (keys too) must be valid Unicode scalars (a lone surrogate would make every later
    read of the row fail), numbers finite, and the whole thing small and shallow."""
    def walk(v: Any, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise BadRequest(f"{name} is nested deeper than {MAX_JSON_DEPTH} levels")
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                raise BadRequest(f"{name} contains text that is not valid Unicode") from None
        elif isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                raise BadRequest(f"{name} contains a non-finite number")
        elif isinstance(v, int) and not isinstance(v, bool):
            if abs(v) >= 2 ** 63:
                raise BadRequest(f"{name} contains an integer that is too large")
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(k, depth + 1)
                walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)
        elif v is not None and not isinstance(v, bool):
            raise BadRequest(f"{name} contains an unsupported value")
    walk(value, 0)
    if len(json.dumps(value, ensure_ascii=True)) > MAX_JSON_BYTES:
        raise BadRequest(f"{name} is larger than {MAX_JSON_BYTES} bytes")
    return value


def processing_config_for(adapter_kind: str, counts_per_g: float | None = None,
                          conversion_source: str | None = None) -> ProcessingConfig:
    """Simulated devices use the simulator's own (cited) accelerometer scale.

    Real hardware gets m/s² only when the confirmed sensitivity and its source are
    given; otherwise vibration stays in raw counts and the score is unavailable.
    """
    if adapter_kind in ("sim", "replay-sim"):
        return demo_processing_config()
    if counts_per_g is not None:
        from .hardware import ProfileError, validate_counts_per_g
        try:
            counts_per_g = validate_counts_per_g(counts_per_g, "counts per g")
        except ProfileError as exc:
            raise ValueError(str(exc)) from exc
        if not conversion_source:
            raise ValueError("an accelerometer conversion needs its source (datasheet + range)")
        g = 9.80665
        conv = {a: (g / counts_per_g, 0.0) for a in ("ax", "ay", "az")}
        return ProcessingConfig(imu=StreamConfig(window_s=0.5, conversion=conv,
                                                 conversion_source=conversion_source))
    return ProcessingConfig()


class Backend:
    def __init__(self, data_dir: str | Path, hub: Hub | None = None, *, recover: bool = True,
                 exclusive: bool = False):
        """``exclusive`` takes the data-dir lock (the server does); ``recover`` marks sessions
        left 'recording' by a dead process as interrupted. Only the lock holder may recover:
        a second process on the same directory must not touch a live server's sessions."""
        self.data_dir = Path(data_dir).expanduser().resolve()  # stored paths must not depend on the cwd
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock_fh = None
        if exclusive:
            self._take_lock()
        self.store = Store(self.data_dir / "vibeguard.sqlite3")
        self.hub = hub or Hub()
        self.runtimes: dict[str, DeviceRuntime] = {}
        self._lock = threading.RLock()
        self._key_locks: dict[str, list] = {}  # request_id -> [lock, users]; removed when unused
        self._loop_stop = threading.Event()
        self._station_engine = None
        self._station_lock = threading.RLock()  # trial index allocation and session finish
        self.recovered = {"wearable_sessions_interrupted": 0, "station_sessions_interrupted": 0}
        if recover:
            self.store.prune_idempotency(IDEMPOTENCY_RETENTION_S)
            ids = [r["session_id"] for r in self.store.wearable_sessions(state="recording")]
            self.recovered = self.store.recover_after_restart()
            for sid in ids:
                self._describe_recovered(sid)
            if any(self.recovered.values()):
                log.warning("marked sessions cut off by the previous run as interrupted: %s", self.recovered)
            if self.recovered["station_sessions_interrupted"]:
                self.station_engine.make_safe()  # a motor left running by the crash must stop

    def _take_lock(self) -> None:
        if fcntl is None:
            return
        fh = (self.data_dir / ".lock").open("w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise DataDirInUse(f"another vg-backend is using {self.data_dir}") from exc
        self._lock_fh = fh

    def _describe_recovered(self, session_id: str) -> None:
        """A cut-off session keeps its files; record what is actually on disk."""
        row = self.store.wearable_session(session_id)
        if row is None or not row.get("dir"):
            return
        d = Path(row["dir"])
        tel = d / "telemetry.ndjson"
        if not tel.is_file():
            return
        data = tel.read_bytes()
        complete = data.count(b"\n")
        files = {n: file_info(d / n) for n in ("telemetry.ndjson", "rx_times.csv", "derived.ndjson",
                                                "commands.ndjson") if (d / n).is_file()}
        # The session ended when the last frame arrived, not when the backend was restarted.
        last_rx = None
        rx = d / "rx_times.csv"
        if rx.is_file():
            for line in reversed(rx.read_text(encoding="utf-8", errors="replace").splitlines()):
                parts = line.split(",")
                if len(parts) == 3 and parts[1].endswith("Z"):
                    last_rx = parts[1]
                    break
        recovered_at = row.get("ended_at")
        fields: dict = {"files": files, "counters": {
            "frames": complete, "recovered": True, "recovered_at": recovered_at,
            "partial_last_line": bool(data) and not data.endswith(b"\n")}}
        if last_rx:
            fields["ended_at"] = last_rx
        row = self.store.update_wearable_session(session_id, **fields)
        mf = d / "manifest.json"  # the on-disk manifest still says 'recording'
        if mf.is_file() and row is not None:
            try:
                m = json.loads(mf.read_text())
                m["session"] = {k: v for k, v in row.items() if k not in ("dir", "files")}
                tmp = d / "manifest.json.tmp"
                tmp.write_text(json.dumps(m, indent=2, default=str) + "\n")
                tmp.replace(mf)
            except (ValueError, OSError):
                pass

    @property
    def station_engine(self):
        """The station trial engine (PROPOSED interface, mock controller until one is agreed)."""
        if self._station_engine is None:
            from .station import StationEngine  # imported here: station imports this module
            self._station_engine = StationEngine(self)
        return self._station_engine

    # ------------------------------------------------------------------ lifecycle

    def add_runtime(self, name: str, adapter: DeviceAdapter, config: ProcessingConfig,
                    role: str = "wearable", **kwargs: Any) -> DeviceRuntime:
        with self._lock:
            old = self.runtimes.get(name)
            if old is not None and old.alive:
                raise RuntimeConflict(f"runtime {name!r} already exists")
            rt = DeviceRuntime(name, adapter, config, self.hub, self.store, self.data_dir,
                               role=role, **kwargs)
            self.runtimes = {**self.runtimes, name: rt}  # copy-on-write: readers never see a change mid-iteration
        return rt.start()

    def runtime(self, name: str) -> DeviceRuntime:
        rt = self.runtimes.get(name)
        if rt is None:
            raise NotFound(f"no runtime named {name!r}")
        return rt

    def restart_backup(self, name: str = "wearable") -> dict:
        """Play a finished backup fallback again from its first frame (a new, labelled replay run)."""
        from .backup import start_backup_replay

        rt = self.runtime(name)
        path = rt.meta.get("backup")
        if not path:
            raise RuntimeConflict(f"runtime {name!r} was not started from a backup")
        if not rt.finished and rt.alive:
            raise RuntimeConflict("the recording is still playing")
        loops = int(rt.meta.get("loop_count", 0)) + 1
        speed, fast = rt.meta.get("speed", 1.0), bool(rt.meta.get("fast"))
        rt.stop()  # finished: its thread has exited; the new runtime replaces it in one step
        new = start_backup_replay(self, path, name=name, speed=speed, fast=fast,
                                  extra_meta={"loop_count": loops})
        self.hub.publish("runtime", name, {"event": "backup_restarted", "loop_count": loops})
        return new.status()

    def loop_backup(self, name: str = "wearable", gap_s: float = 1.0) -> threading.Thread:
        """Keep the backup fallback playing: when it ends, start it again after ``gap_s``."""
        def watch() -> None:
            while not self._loop_stop.wait(0.5):
                rt = self.runtimes.get(name)
                if rt is not None and rt.finished and rt.meta.get("backup"):
                    if self._loop_stop.wait(gap_s):
                        return
                    try:
                        self.restart_backup(name)
                    except Exception:  # noqa: BLE001 - keep trying; an error must not end the loop
                        log.exception("could not restart the backup fallback")
        t = threading.Thread(target=watch, name=f"loop-backup:{name}", daemon=True)
        t.start()
        return t

    def remove_runtime(self, name: str) -> dict:
        rt = self.runtime(name)
        rt.stop()
        with self._lock:
            self.runtimes = {k: v for k, v in self.runtimes.items() if k != name}
        self.hub.publish("runtime", name, {"event": "runtime_removed", "runtime": name})
        return {"runtime": name, "removed": True}

    def shutdown(self, timeout: float = 15.0) -> None:
        """Stop every runtime first; close the store only when no thread can still write."""
        self._loop_stop.set()
        if self._station_engine is not None:
            self._station_engine.shutdown()  # motor safe, open trial closed as missing
        rts = list(self.runtimes.values())
        for rt in rts:
            rt.request_stop()
        stuck = [rt.name for rt in rts if not rt.stop(timeout)]
        if stuck:
            log.error("runtimes still running after %ss, leaving the store open: %s", timeout, stuck)
        else:
            self.store.close()
        if self._lock_fh is not None:
            self._lock_fh.close()
            self._lock_fh = None

    # ------------------------------------------------------------------ idempotency helper

    def _idempotent(self, request_id: str | None, kind: str, fn: Callable[[], Any],
                    refresh: Callable[[Any], Any] | None = None) -> Any:
        """Run ``fn`` once per (request_id, kind). ``kind`` includes the command's target.

        Same-id retries are serialised on a lock for that id only, so a slow command
        never blocks unrelated ones. ``refresh`` turns a stored response into the
        current state of the record it describes.
        """
        request_id = _request_id(request_id)
        if request_id is None:
            return fn()
        with self._lock:
            entry = self._key_locks.setdefault(request_id, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                prior = self.store.idem_get(request_id, kind)
                if prior is not None:
                    return refresh(prior) if refresh else prior
                result = fn()
                self.store.idem_put(request_id, kind, result)
                return result
        finally:
            with self._lock:
                entry[1] -= 1
                if entry[1] == 0:
                    self._key_locks.pop(request_id, None)

    def _session_refresh(self, stored: dict) -> dict:
        return self.store.wearable_session(stored["session_id"]) or stored

    # ------------------------------------------------------------------ runtimes / calibration

    def runtime_statuses(self) -> list[dict]:
        return [rt.status() for rt in list(self.runtimes.values())]

    def calibrate(self, runtime: str, action: str, request_id: str | None = None) -> dict:
        if action not in ("relaxed", "squeeze", "cancel"):
            raise BadRequest("action must be relaxed, squeeze or cancel")
        _request_id(request_id)
        return self._idempotent(request_id, f"calibrate:{runtime}:{action}",
                                lambda: self.runtime(runtime).calibrate(action))

    # ------------------------------------------------------------------ wearable sessions

    def start_session(self, runtime: str = "wearable", label: str | None = None,
                      request_id: str | None = None) -> dict:
        if not isinstance(runtime, str):
            raise BadRequest("runtime must be a string")
        label = _text(label, "label")
        return self._idempotent(request_id, f"start_session:{runtime}",
                                lambda: self.runtime(runtime).start_session(label),
                                refresh=self._session_refresh)

    def stop_session(self, session_id: str, request_id: str | None = None) -> dict:
        def run() -> dict:
            row = self.store.wearable_session(session_id)
            if row is None:
                raise NotFound(f"no session {session_id!r}")
            rt = self.runtimes.get(row["runtime"])
            # a runtime still recording this session wins over a row that says otherwise
            if rt is not None and rt.alive and rt.is_recording(session_id):
                try:
                    return rt.stop_session(session_id)
                except (RuntimeConflict, RuntimeGone):  # a concurrent stop (or the runtime ending) won the race
                    rt.stop(2.0)  # let the dying runtime finish closing the session
                    row = self.store.wearable_session(session_id)
                    if row is not None and row["state"] != "recording":
                        return row
                    raise
            if row["state"] != "recording":
                return row  # already stopped: stopping again changes nothing
            raise RuntimeConflict(f"runtime {row['runtime']!r} is not recording {session_id}")
        return self._idempotent(request_id, f"stop_session:{session_id}", run,
                                refresh=self._session_refresh)

    def session(self, session_id: str) -> dict:
        row = self.store.wearable_session(session_id)
        if row is None:
            raise NotFound(f"no session {session_id!r}")
        return row

    def session_file(self, session_id: str, name: str) -> Path:
        row = self.session(session_id)
        allowed = ("telemetry.ndjson", "derived.ndjson", "rx_times.csv", "commands.ndjson", "manifest.json")
        if name not in allowed:
            raise NotFound(f"file must be one of {allowed}")
        if row["state"] == "recording":
            raise RuntimeConflict("the session is still recording; stop it before downloading its files")
        path = Path(row["dir"]) / name
        if not path.is_file():
            raise NotFound(f"{name} is not available for {session_id}")
        return path

    # ------------------------------------------------------------------ replay

    def start_replay(self, session_id: str, speed: float = 1.0, fast: bool = False,
                     request_id: str | None = None) -> dict:
        speed = _number(speed, "speed", minimum=0, exclusive=True, maximum=1000)
        if not isinstance(fast, bool):
            raise BadRequest("fast must be true or false")

        def run() -> dict:
            row = self.session(session_id)
            if row["state"] == "recording":
                raise RuntimeConflict("the session is still recording")
            path = Path(row["dir"]) / "telemetry.ndjson"
            if not path.is_file():
                raise NotFound(f"session {session_id} has no telemetry recording")
            recording_id = row["recording_id"] or f"rec-{session_id}"
            adapter = ReplayAdapter(path, recording_id=recording_id, speed=speed, fast=fast)
            schedule = _read_commands(Path(row["dir"]) / "commands.ndjson")
            original = row.get("original_source") or row.get("source")
            config = processing_config_for("replay-sim" if original == "simulated" else "replay")
            manifest = Path(row["dir"]) / "manifest.json"
            if manifest.is_file():  # replay with the processing config the session was recorded with
                config = _config_from_manifest(manifest, config)
            name = new_id("replay")
            rt = self.add_runtime(name, adapter, config, role="replay", calibration_schedule=schedule,
                                  restore_calibration=False,
                                  meta={"replay_of_session": session_id, "recording_id": recording_id,
                                        "original_source": original, "speed": speed, "fast": fast})
            return rt.status()
        return self._idempotent(request_id, f"start_replay:{session_id}", run)

    # ------------------------------------------------------------------ station records

    def start_station_session(self, source: str | None = None, label: str | None = None,
                              protocol: dict | None = None, wearable_session_id: str | None = None,
                              request_id: str | None = None) -> dict:
        label = _text(label, "label")
        if protocol is not None and not isinstance(protocol, dict):
            raise BadRequest("protocol must be an object")
        _clean_json(protocol, "protocol")
        wearable_session_id = _text(wearable_session_id, "wearable_session_id", MAX_ID)
        if source is not None and source not in SOURCES:
            raise BadRequest(f"source must be one of {SOURCES}")

        def run() -> dict:
            src = source
            if wearable_session_id is not None:
                linked = self.session(wearable_session_id)
                if src is None:
                    src = linked["source"] or "live"
                elif src == "live" and linked["source"] in ("simulated", "replay"):
                    raise BadRequest(f"a live station session cannot be linked to a "
                                     f"{linked['source']} wearable session")
            row = self.store.create_station_session({
                "station_session_id": new_id("ss"), "source": src or "live", "label": label,
                "protocol": protocol, "wearable_session_id": wearable_session_id})
            self.hub.publish("station", None, {"event": "station_session_started", "station_session": row})
            return row
        return self._idempotent(request_id, "start_station_session", run)

    def add_trial(self, station_session_id: str, stimulus: dict, response: str,
                  trial_index: int | None = None, measured_amplitude: dict | None = None,
                  is_catch: bool = False, presented_at: str | None = None,
                  responded_at: str | None = None, response_latency_ms: float | None = None,
                  note: str | None = None, request_id: str | None = None) -> dict:
        if response not in RESPONSES:
            raise BadRequest(f"response must be one of {RESPONSES} (a timeout is 'missing')")
        if not isinstance(stimulus, dict) or not stimulus:
            raise BadRequest("stimulus must describe the motor setting, e.g. "
                             "{'motor_setting': 120, 'units': 'pwm_duty_0_255'}")
        if measured_amplitude is not None and (not isinstance(measured_amplitude, dict)
                                               or not {"value", "units", "source"} <= set(measured_amplitude)):
            raise BadRequest("measured_amplitude needs value, units and source (or omit it)")
        _clean_json(stimulus, "stimulus")
        _clean_json(measured_amplitude, "measured_amplitude")
        if trial_index is not None and (isinstance(trial_index, bool) or not isinstance(trial_index, int)
                                        or not 0 <= trial_index <= MAX_TRIAL_INDEX):
            raise BadRequest(f"trial_index must be an integer from 0 to {MAX_TRIAL_INDEX}")
        if not isinstance(is_catch, bool):
            raise BadRequest("is_catch must be true or false")
        presented_at = _text(presented_at, "presented_at", 64)
        responded_at = _text(responded_at, "responded_at", 64)
        note = _text(note, "note")
        if response_latency_ms is not None:
            response_latency_ms = _number(response_latency_ms, "response_latency_ms", minimum=0)
        if response == "missing" and (responded_at is not None or response_latency_ms is not None):
            raise BadRequest("a missing response has no response time")

        def run() -> dict:
          with self._station_lock:  # index allocation + insert + state check are one step
            s = self.store.station_session(station_session_id, with_trials=False)
            if s is None:
                raise NotFound(f"no station session {station_session_id!r}")
            if s["state"] != "testing":
                raise RuntimeConflict(f"station session is {s['state']}; trials are closed")
            idx = self.store.next_trial_index(station_session_id) if trial_index is None else trial_index
            try:
                trial = self.store.add_station_trial({
                    "trial_id": new_id("tr"), "station_session_id": station_session_id, "trial_index": idx,
                    "stimulus": stimulus, "measured_amplitude": measured_amplitude, "is_catch": is_catch,
                    "response": response, "presented_at": presented_at, "responded_at": responded_at,
                    "response_latency_ms": response_latency_ms, "note": note})
            except sqlite3.IntegrityError as exc:
                raise RuntimeConflict(f"trial {idx} is already recorded for this session") from exc
            self.hub.publish("station", None, {"event": "station_trial_recorded", "trial": trial})
            return trial
        return self._idempotent(request_id, f"add_trial:{station_session_id}", run)

    def finish_station_session(self, station_session_id: str, state: str, reason: str | None = None,
                               request_id: str | None = None) -> dict:
        if state not in ("completed", "aborted"):
            raise BadRequest("state must be completed or aborted")
        reason = _text(reason, "reason")

        def run() -> dict:
          with self._station_lock:  # a trial cannot slip in between the summary and the state change
            s = self.store.station_session(station_session_id)
            if s is None:
                raise NotFound(f"no station session {station_session_id!r}")
            if s["state"] != "testing":
                return s  # finishing twice changes nothing
            summary = station_summary(s["trials"])
            row = self.store.finish_station_session(station_session_id, state, reason, summary)
            self.hub.publish("station", None, {"event": f"station_session_{state}", "station_session": row})
            return row
        return self._idempotent(request_id, f"finish_station:{station_session_id}:{state}", run,
                                refresh=lambda r: self.store.station_session(r["station_session_id"]) or r)

    def station_session(self, sid: str) -> dict:
        s = self.store.station_session(sid)
        if s is None:
            raise NotFound(f"no station session {sid!r}")
        return s

    # ------------------------------------------------------------------ history

    def history(self, source: str | None = None, limit: int = 200) -> dict:
        wearable = self.store.wearable_sessions(source=source, limit=limit)
        station = self.store.station_sessions(source=source, limit=limit)
        items = ([{"kind": "wearable", **_history_view(r)} for r in wearable]
                 + [{"kind": "station", **r} for r in station])
        items.sort(key=lambda r: r["started_at"], reverse=True)
        return {"items": items[:limit], "generated_at": utc_now_iso(),
                "labels": {"simulated": "SIMULATED data, not a measurement",
                           "replay": "REPLAY of an earlier recording, not live",
                           "live": "live device data"}}


def _history_view(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "files"}


def station_summary(trials: list[dict]) -> dict:
    """Counts only. No threshold estimate: motor settings are not measured amplitudes."""
    real = [t for t in trials if not t["is_catch"]]
    catch = [t for t in trials if t["is_catch"]]

    def count(ts: list[dict], resp: str) -> int:
        return sum(1 for t in ts if t["response"] == resp)
    return {"trials": len(trials),
            "stimulus_trials": {r: count(real, r) for r in RESPONSES},
            "catch_trials": {r: count(catch, r) for r in RESPONSES},
            "note": "prototype perception workflow; motor settings are settings, not measured "
                    "amplitudes; not a diagnostic result"}


def _read_commands(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _config_from_manifest(manifest: Path, fallback: ProcessingConfig) -> ProcessingConfig:
    """Rebuild the IMU conversion the session used; everything else stays default."""
    try:
        cfg = json.loads(manifest.read_text())["processing_config"]
        imu = cfg["imu"]
        if imu.get("conversion"):
            conv = {k: tuple(v) for k, v in imu["conversion"].items()}
            return dataclasses.replace(fallback, imu=StreamConfig(
                window_s=imu["window_s"], conversion=conv, conversion_source=imu["conversion_source"]))
        return dataclasses.replace(fallback, imu=StreamConfig(window_s=imu["window_s"]))
    except (KeyError, ValueError, TypeError):
        return fallback
