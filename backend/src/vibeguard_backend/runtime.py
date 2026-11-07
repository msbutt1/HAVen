"""DeviceRuntime: one acquisition thread per device source.

The thread owns the adapter and the device's ``Processor``; nothing else touches
them. HTTP handlers send commands (calibrate, start/stop session) through
``call()``, which runs them on the acquisition thread between frames. Acquisition
therefore never waits for the UI, and the processor is never used concurrently.

Per frame:  adapter → raw recording (if a session is active) → contract.parse_line
            → Processor.feed (host receive time) → hub + derived recording + store
Every 50 ms: Processor.poll(), so a silent or unplugged device becomes visibly stale,
            and the session files are flushed at least once a second.

A frame that fails the contract is rejected whole: it is counted, recorded raw,
reported as ``invalid_record``, and has no other effect. No single frame (and no
unexpected error while handling one) can stop ingestion.

Host state (what the dashboard shows as the device's state):
    disconnected → ready ⇄ calibrating / recording → completed (finite sources)
    error (unexpected adapter failure the loop could not recover from)
"""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import hashlib
import json
import logging
import queue
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from vibeguard_signals import contract
from vibeguard_signals.processing import ProcessingConfig, Processor, to_ndjson_line

from .adapters import AdapterDisconnected, DeviceAdapter, EndOfStream, new_id
from .hub import Hub, utc_now_iso
from .store import Store

log = logging.getLogger("vibeguard.runtime")

POLL_S = 0.05
FLUSH_S = 1.0
SILENT_S = 0.5          # a connected host-clock device with no data this long is "silent"
FOREIGN_SWITCH_AFTER = 3  # consecutive valid frames from another device_id before we switch to it
#: stream_event kinds that are counted per runtime and per session
COUNTED_EVENTS = ("seq_gap", "sample_gap", "stream_stale", "stream_recovered", "boot_change",
                  "invalid_record", "duplicate_record", "late_record", "foreign_device",
                  "provenance_changed", "device_sensor_state", "timing_discontinuity",
                  "rate_mismatch", "retired_boot_record", "sample_overlap", "buffer_overflow",
                  "channels_changed", "rate_changed", "score_accumulation_reset")
CAPTURING = ("capturing_relaxed", "capturing_squeeze")


class RuntimeConflict(Exception):
    """The command is not allowed in the runtime's current state (HTTP 409)."""


class RuntimeGone(Exception):
    """The runtime's thread is not running (HTTP 409)."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_info(path: Path) -> dict:
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": _sha256(path)}


class SessionRecorder:
    """Files of one wearable session.

    telemetry.ndjson  every frame exactly as received (replayable with vg-replay/vg-process)
    rx_times.csv      line number, host wall-clock receive time, host monotonic time
    derived.ndjson    every processor output during the session
    commands.ndjson   calibration commands with the (boot_id, seq) of the last recorded
                      frame before them, so a replay reissues them at the same point
    """

    FILES = ("telemetry.ndjson", "rx_times.csv", "derived.ndjson", "commands.ndjson")

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=False)
        self._fhs = []
        try:
            self._tel = self._open("telemetry.ndjson", "wb")
            self._rx = self._open("rx_times.csv", "w")
            self._rx.write("line,rx_wall,rx_monotonic_s\n")
            self._der = self._open("derived.ndjson", "w")
            self._cmd = self._open("commands.ndjson", "w")
        except Exception:
            self.abort()
            raise
        self.lines = 0
        self.derived_count = 0
        self._last_flush = time.monotonic()
        self.closed = False

    def _open(self, name: str, mode: str):
        fh = (self.dir / name).open(mode) if "b" in mode else \
            (self.dir / name).open(mode, encoding="utf-8", newline="\n")
        self._fhs.append(fh)
        return fh

    def raw(self, line: bytes, rx_wall: str, rx_mono: float) -> None:
        n = self.lines + 1
        self._tel.write(line if line.endswith(b"\n") else line + b"\n")
        self._rx.write(f"{n},{rx_wall},{rx_mono:.6f}\n")
        self.maybe_flush(time.monotonic())
        self.lines = n  # counted only once written (and flushed, when due)

    def derived(self, out: dict) -> None:
        self.derived_count += 1
        self._der.write(to_ndjson_line(out))

    def command(self, cmd: dict) -> None:
        self._cmd.write(json.dumps(cmd, separators=(",", ":")) + "\n")
        self._cmd.flush()

    def maybe_flush(self, now: float) -> None:
        """Bounded data loss on a crash (≈ FLUSH_S), without a flush per line."""
        if now - self._last_flush >= FLUSH_S:
            self.flush()
            self._last_flush = now

    def flush(self) -> None:
        if not self.closed:
            for fh in self._fhs:
                fh.flush()

    def close(self) -> dict:
        self.closed = True
        for fh in self._fhs:
            fh.close()
        return {name: file_info(self.dir / name) for name in self.FILES}

    def abort(self) -> None:
        """Undo a recorder whose session could not be created."""
        self.closed = True
        for fh in self._fhs:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
        for name in self.FILES:
            (self.dir / name).unlink(missing_ok=True)
        try:
            self.dir.rmdir()
        except OSError:
            pass


@dataclasses.dataclass
class _ActiveSession:
    session_id: str
    rec: SessionRecorder
    started_mono: float
    start_score: dict | None
    source: str | None = None
    current_source: str | None = None
    original_source: str | None = None
    recording_id: str | None = None
    persisted_provenance: bool = False
    first_score: dict | None = None
    last_key: tuple[str, int] | None = None  # last valid frame recorded in THIS session
    boot_ids: list[str] = dataclasses.field(default_factory=list)
    sources_seen: list[str] = dataclasses.field(default_factory=list)
    events: Counter = dataclasses.field(default_factory=Counter)
    counters: Counter = dataclasses.field(default_factory=Counter)
    calibration_ids: list[str] = dataclasses.field(default_factory=list)
    pre_session_calibration: dict | None = None


def _score_view(score: dict | None) -> dict | None:
    if score is None:
        return None
    return {"accumulation": score.get("accumulation"),
            "cumulative_au": (score.get("cumulative") or {}).get("value"),
            "valid_duration_s": score.get("valid_duration_s"),
            "excluded_duration_s": score.get("excluded_duration_s"),
            "boot_segments": score.get("boot_segments"),
            "excluded_by_reason": score.get("excluded_by_reason") or {},
            "status": score.get("status"), "t_us": score.get("t_us")}


def session_score_summary(start: dict | None, first: dict | None, end: dict | None,
                          mixed_sources: bool = False) -> dict:
    """Score attributable to one session = end − start of the same accumulation.

    The processor's score is cumulative per accumulation (a provenance change
    starts a new one). If the accumulation changed during the session, or the
    session mixed data sources, the difference is meaningless and is reported as
    unavailable with the reason. ``start`` None means nothing had been scored
    before the session, so it started from zero.
    """
    e = _score_view(end)
    out: dict[str, Any] = {
        "label": "experimental score, arbitrary units (not a dose, not A(8))",
        "at_start": _score_view(start), "at_end": e, "session_au": None,
        "session_valid_s": None, "status": "unavailable", "reason": None,
        # A session that starts while the processor is already warm counts its first frames at once;
        # a replay starts cold, so its score is lower by about the warm-up time (about 1.2 s).
        "started_mid_stream": start is not None,
    }
    if e is None:
        out["reason"] = "no_score_records_during_session"
        return out
    if start is not None:
        s = _score_view(start)
    else:
        f = _score_view(first)
        s = {"accumulation": f["accumulation"] if f else e["accumulation"],
             "cumulative_au": 0.0, "valid_duration_s": 0.0}
    if s["accumulation"] != e["accumulation"]:  # type: ignore[index]
        out["reason"] = "score_accumulation_reset_during_session"
        return out
    if mixed_sources:  # e.g. a reboot onto another source: the processor did not reset the score
        out["reason"] = "session_mixes_data_sources"
        return out
    out["session_au"] = round(e["cumulative_au"] - s["cumulative_au"], 6)  # type: ignore[index]
    out["session_valid_s"] = round((e["valid_duration_s"] or 0) - (s["valid_duration_s"] or 0), 4)  # type: ignore[index]
    # Why time was not scored during THIS session (end minus start, per reason).
    before = (s.get("excluded_by_reason") or {}) if start is not None else {}
    delta = {k: round(v - before.get(k, 0.0), 3) for k, v in (e.get("excluded_by_reason") or {}).items()}
    out["excluded_by_reason"] = {k: v for k, v in sorted(delta.items(), key=lambda kv: -kv[1]) if v > 0}
    if out["session_valid_s"] > 0:
        out["status"] = "ok"
        out["explanation"] = (f"{out['session_valid_s']} s of this session had both a fresh vibration and a "
                              f"calibrated activation reading and were scored: {out['session_au']} au.")
    else:
        out["status"] = "no_valid_time"
        out["reason"] = "no_time_with_both_inputs_valid"
        out["explanation"] = ("No time in this session had both a fresh vibration and a calibrated activation "
                              "reading, so nothing was scored. 0 au here means 'not measured', not 'low'. "
                              "See excluded_by_reason; calibrate (relaxed, then squeeze) and record again.")
    return out


def replay_calibration(record: dict, replay_provenance: dict) -> dict:
    """A copy of a pre-session calibration, re-labelled for a replay of that session.

    The processor only applies a calibration whose provenance matches the data, so a
    replay cannot use the original record as is. The copy keeps every reference
    value, gets the replay's provenance and a new id, and names its origin.
    """
    return {**record, "provenance": dict(replay_provenance),
            "calibration_id": f"{record['calibration_id']}-replay",
            "replay_of_calibration_id": record["calibration_id"]}


class DeviceRuntime:
    def __init__(self, name: str, adapter: DeviceAdapter, config: ProcessingConfig, hub: Hub,
                 store: Store, data_dir: Path, role: str = "wearable",
                 calibration_schedule: list[dict] | None = None,
                 restore_calibration: bool = True, meta: dict | None = None,
                 call_timeout_s: float = 5.0):
        self.name = name
        self.adapter = adapter
        self.role = role
        self.hub = hub
        self.store = store
        self.data_dir = Path(data_dir)
        self.config = dataclasses.replace(config, clock=adapter.clock)
        self.proc = Processor(self.config)
        self.meta = meta or {}
        self.restore_calibration = restore_calibration
        self.call_timeout_s = call_timeout_s
        self._schedule = list(calibration_schedule or [])
        self._schedule_started = False

        self.connection = "connecting"  # connecting | connected | disconnected
        self.connection_detail: str | None = None
        self.finished = False
        self.error: str | None = None
        self.calibration_state = "uncalibrated"
        self.calibration_reason: str | None = None
        self.device_id: str | None = None
        self.boot_id: str | None = None
        self.provenance: dict | None = None
        self.hello: dict | None = None
        self.device_status: dict | None = None
        self.latest: dict[str, dict | None] = {"emg": None, "imu": None, "score": None}
        self.counters: Counter = Counter()
        self.events: Counter = Counter()
        self.last_rx_wall: str | None = None
        self.last_rx_mono: float | None = None
        self.last_invalid: dict | None = None
        self.last_frame_error: str | None = None
        self.silent = False
        self._foreign: tuple[str, int] | None = None
        self.started_at = utc_now_iso()
        self.session: _ActiveSession | None = None
        self._last_key: tuple[str, int] | None = None
        self._cal_id: str | None = None
        self._restored = False
        self._last_state: str | None = None
        self._last_poll = 0.0
        self._cmds: queue.Queue = queue.Queue()
        self._gate = threading.Lock()  # orders call() enqueues against thread exit
        self._closed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"runtime:{name}", daemon=True)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> "DeviceRuntime":
        self._thread.start()
        return self

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self, timeout: float = 5.0) -> bool:
        """Ask the thread to stop and wait; True if it has exited."""
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def call(self, fn: Callable[[], Any], timeout: float | None = None) -> Any:
        """Run ``fn`` on the acquisition thread and return its result (or raise its error).

        On timeout the command is cancelled if it has not started, so a timeout
        always means "not executed". A command that already started is awaited.
        """
        if threading.current_thread() is self._thread:
            return fn()
        fut: cf.Future = cf.Future()
        with self._gate:
            if self._closed or not self._thread.is_alive():
                raise RuntimeGone(f"runtime {self.name!r} is not running")
            self._cmds.put((fn, fut))
        try:
            return fut.result(self.call_timeout_s if timeout is None else timeout)
        except cf.TimeoutError:
            if fut.cancel():
                raise
            return fut.result()  # already running: it will finish promptly

    # ------------------------------------------------------------------ state

    @property
    def host_state(self) -> str:
        if self.error:
            return "error"
        if self.finished:
            return "completed"
        if self.connection != "connected":
            return "disconnected"
        if self.calibration_state in CAPTURING:
            return "calibrating"
        if self.session is not None:
            return "recording"
        return "ready"

    def status(self) -> dict:
        now = time.monotonic()
        sess = self.session
        last_rx = self.last_rx_mono
        return {
            "runtime": self.name, "role": self.role, "state": self.host_state,
            "connection": self.connection, "connection_detail": self.connection_detail,
            "error": self.error, "adapter": self.adapter.describe(),
            "device_id": self.device_id, "boot_id": self.boot_id,
            "provenance": dict(self.provenance) if self.provenance else None,
            "calibration": {"state": self.calibration_state, "reason": self.calibration_reason,
                            "calibration_id": self._cal_id},
            "active_session_id": sess.session_id if sess else None,
            "last_rx_wall": self.last_rx_wall,
            "last_rx_age_s": None if last_rx is None else round(now - last_rx, 3),
            "data_fresh": not self.silent and self.connection == "connected",
            "counters": dict(self.counters.copy()), "events": dict(self.events.copy()),
            "last_invalid_frame": self.last_invalid, "last_frame_error": self.last_frame_error,
            "started_at": self.started_at, "meta": self.meta,
        }

    def snapshot(self) -> dict:
        """Latest values for a dashboard that connects mid-stream.

        While the device is not connected, the latest values are reported as
        unavailable (the last ones are kept under ``last_seen`` for context), so
        nothing old is ever presented as current.
        """
        latest = dict(self.latest)
        reason = None
        if self.error or (not self._thread.is_alive() and not self.finished):
            reason = "runtime_error"
        elif self.connection != "connected" and not self.finished:
            reason = "device_disconnected"
        elif self.silent:
            reason = "device_silent"
        if reason:
            def stale(v: dict | None) -> dict | None:
                # Streams the processor already invalidated keep their own reasons, but the score
                # must be masked too: it keeps its last rate until the next record arrives.
                if v is None or (v.get("status") == "unavailable" and v.get("type") != "experimental_score"):
                    return v
                return {"status": "unavailable", "reason": reason, "reasons": [reason], "last_seen": v}
            latest = {k: stale(v) for k, v in latest.items()}
        return {**self.status(), "latest": latest, "hello": self.hello,
                "device_status": self.device_status}

    def _publish_state(self, force: bool = False) -> None:
        st = self.host_state
        if force or st != self._last_state:
            self._last_state = st
            self.hub.publish("runtime", self.name, self.status())

    # ------------------------------------------------------------------ thread

    def _run(self) -> None:
        backoff = 0.25
        self._publish_state(force=True)
        try:
            while not self._stop.is_set():
                self._drain_cmds()
                if self.connection != "connected":
                    try:
                        self.adapter.open()
                    except AdapterDisconnected as exc:
                        self._set_connection("disconnected", str(exc))
                        self._idle(backoff)
                        backoff = min(backoff * 2, 2.0)
                        continue
                    self._set_connection("connected", None)
                    backoff = 0.25
                try:
                    line = self.adapter.read_line(POLL_S)
                except AdapterDisconnected as exc:
                    self._set_connection("disconnected", str(exc))
                    continue
                except EndOfStream:
                    self._end_of_stream()
                    break
                now = time.monotonic()
                if line is not None:
                    self._on_line(line, now)
                self._tick(now)
        except Exception as exc:  # noqa: BLE001 - adapter failures we cannot recover from
            log.exception("runtime %s crashed", self.name)
            self.error = f"{type(exc).__name__}: {exc}"
            self._publish_state(force=True)
        finally:
            try:
                if self.session is not None:
                    self._stop_session("interrupted", "runtime_stopped" if not self.error else "runtime_error")
            except Exception:  # noqa: BLE001
                log.exception("could not close session on %s", self.name)
            try:
                self.adapter.close()
            finally:
                with self._gate:
                    self._closed = True
                self._fail_pending()

    def _tick(self, now: float) -> None:
        """Periodic work: staleness polling (host clock) and bounded-loss flushing."""
        if self.config.clock == "host" and now - self._last_poll >= POLL_S:
            self._last_poll = now
            self._emit(self.proc.poll(now))
            if (self.connection == "connected" and self.last_rx_mono is not None
                    and not self.silent and now - self.last_rx_mono > SILENT_S):
                self.silent = True  # link up but nothing arriving (hung firmware, wrong baud rate)
                self.counters["silent_episodes"] += 1
                self.hub.publish("runtime", self.name, {"event": "device_silent", "at": utc_now_iso(),
                                                         "silent_for_s": round(now - self.last_rx_mono, 2)})
                self._publish_state(force=True)
        if self.session is not None:
            self.session.rec.maybe_flush(now)

    def _idle(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < end:
            self._drain_cmds()
            self._tick(time.monotonic())
            time.sleep(POLL_S)

    def _drain_cmds(self) -> None:
        while True:
            try:
                fn, fut = self._cmds.get_nowait()
            except queue.Empty:
                return
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as exc:  # noqa: BLE001 - returned to the caller
                    fut.set_exception(exc)

    def _fail_pending(self) -> None:
        while True:
            try:
                _, fut = self._cmds.get_nowait()
            except queue.Empty:
                return
            if fut.set_running_or_notify_cancel():
                fut.set_exception(RuntimeGone(f"runtime {self.name!r} stopped"))

    def _set_connection(self, conn: str, detail: str | None) -> None:
        if conn == self.connection and detail == self.connection_detail:
            return
        changed = conn != self.connection
        self.connection, self.connection_detail = conn, detail
        if changed:
            self.counters[f"connection_{conn}"] += 1
            if self.session is not None:
                self.session.counters[f"connection_{conn}"] += 1
                if conn == "disconnected":
                    self.session.rec.flush()  # what arrived before the unplug is on disk now
            self.hub.publish("runtime", self.name, {"event": "device_connection", "connection": conn,
                                                     "detail": detail, "at": utc_now_iso()})
        self._publish_state(force=changed)

    def _end_of_stream(self) -> None:
        self._emit(self.proc.finish())
        if self._schedule:
            self.counters["calibration_commands_unfired"] += len(self._schedule)
        if self.session is not None:
            self._stop_session("completed", "end_of_input")
        self.finished = True  # only after the session row is final
        self._publish_state(force=True)

    # ------------------------------------------------------------------ frames

    def _on_line(self, line: bytes, now: float) -> None:
        self.counters["frames"] += 1
        self.last_rx_mono = now
        self.last_rx_wall = utc_now_iso()
        if self.silent:
            self.silent = False
            self.hub.publish("runtime", self.name, {"event": "device_resumed", "at": self.last_rx_wall})
            self._publish_state(force=True)
        try:
            if self.session is not None:
                self.session.rec.raw(line, self.last_rx_wall, now)
            try:
                record, issues = contract.parse_line(line)
            except (RecursionError, ValueError, TypeError) as exc:  # e.g. pathological nesting
                record, issues = None, [contract.Issue("malformed_json", "", f"undecodable frame: {type(exc).__name__}")]
            if record is None or issues:
                self._on_invalid(issues, len(line))
                return
            self.counters["records"] += 1
            self._before_feed(record)
            rx = now if self.config.clock == "host" else None
            self._emit(self.proc.feed(record, rx_time_s=rx))
            if record["device_id"] == self.device_id and record["boot_id"] == self.proc.boot_id:
                # A duplicate or late record must not move the anchor backwards: calibration
                # commands are keyed on it, and a replay fires them at the same key.
                key = (record["boot_id"], record["seq"])
                if self._last_key is None or self._last_key[0] != key[0] or key[1] > self._last_key[1]:
                    self._last_key = key
                s = self.session
                if s is not None and (s.last_key is None or s.last_key[0] != key[0] or key[1] > s.last_key[1]):
                    s.last_key = key
                self._run_schedule(after=self._last_key)
            rtype = record["type"]
            self.hub.publish("device" if rtype in ("hello", "status") else "telemetry", self.name, record)
        except Exception as exc:  # noqa: BLE001 - one frame must never stop ingestion
            log.exception("runtime %s: error while handling a frame", self.name)
            self.counters["frame_errors"] += 1
            self.last_frame_error = f"{type(exc).__name__}: {exc}"
            self.hub.publish("runtime", self.name, {"event": "frame_error", "error": self.last_frame_error,
                                                     "at": self.last_rx_wall})

    def _on_invalid(self, issues: list, nbytes: int) -> None:
        self.counters["invalid_frames"] += 1
        if self.session is not None:
            self.session.counters["invalid_frames"] += 1
        self.last_invalid = {"at": self.last_rx_wall, "codes": sorted({i.code for i in issues}),
                             "first_issue": str(issues[0]) if issues else None, "bytes": nbytes}
        self._emit(self.proc.feed_invalid_frame(issues, self.adapter.kind))

    def _before_feed(self, record: dict) -> None:
        """Bookkeeping for a contract-valid record, before the processor sees it."""
        dev, boot, prov = record["device_id"], record["boot_id"], record["provenance"]
        if self.device_id is None:
            self.device_id = dev
        if dev != self.device_id:
            # Another device on the same link. The processor reports each frame as foreign_device.
            # Several valid frames in a row from the same new id mean the wearable was swapped:
            # switch to it instead of ignoring it forever while the status says "ready".
            n = (self._foreign[1] + 1) if self._foreign and self._foreign[0] == dev else 1
            self._foreign = (dev, n)
            self.counters["foreign_frames"] += 1
            if n < FOREIGN_SWITCH_AFTER:
                return
            self._switch_device(dev)
        else:
            self._foreign = None
        if boot != self.boot_id or record["type"] == "hello":
            self.store.touch_device(dev, boot, prov["source"], record if record["type"] == "hello" else None)
        self.boot_id = boot
        self.provenance = prov
        if record["type"] == "hello":
            self.hello = record
        elif record["type"] == "status":
            self.device_status = record
        s = self.session
        if s is not None:
            self._session_provenance(s, prov)
            if boot not in s.boot_ids:
                s.boot_ids.append(boot)
        if self._schedule and not self._schedule_started:
            self._schedule_started = True
            self._run_schedule(after=None)
        # After a backend restart: reuse the last calibration for this device. The
        # processor only applies it once the data proves it compatible.
        if (self.restore_calibration and not self._restored and self.proc.calibration_record() is None
                and self.calibration_state == "uncalibrated"):
            self._restored = True
            saved = self.store.latest_calibration(dev, prov["source"])
            if saved is not None:
                self._emit(self.proc.load_calibration(saved["record"]))
                self.counters["calibration_restored"] += 1

    def _switch_device(self, new_id: str) -> None:
        """The device on this link changed. Close any open session, start from a clean processor."""
        old = self.device_id
        if self.session is not None:
            self._stop_session("interrupted", "device_changed")
        self.proc = Processor(self.config)
        self.device_id, self.boot_id, self.provenance = new_id, None, None
        self.hello = self.device_status = None
        self.latest = {"emg": None, "imu": None, "score": None}
        self.calibration_state, self.calibration_reason, self._cal_id = "uncalibrated", None, None
        self._restored, self._last_key, self._foreign = False, None, None
        self.counters["device_changes"] += 1
        self.events["device_changed"] += 1
        self.hub.publish("runtime", self.name, {"event": "device_changed", "from": old, "to": new_id,
                                                 "at": utc_now_iso(),
                                                 "note": "a different device is on this link; calibrate it again"})
        self._publish_state(force=True)

    def _session_provenance(self, s: _ActiveSession, prov: dict, announce: bool = True) -> None:
        src = prov["source"]
        if src not in s.sources_seen:
            s.sources_seen.append(src)
        if s.current_source is not None and src != s.current_source:
            s.counters["provenance_changes"] += 1
        s.current_source = src
        if s.source is None:
            s.source = src
            s.original_source = prov.get("original_source")
            s.recording_id = prov.get("recording_id") or (f"rec-{s.session_id}" if src == "live" else None)
        if not s.persisted_provenance:
            s.persisted_provenance = True
            row = self.store.update_wearable_session(
                s.session_id, source=s.source, original_source=s.original_source,
                recording_id=s.recording_id, device_id=self.device_id)
            if announce:  # a session that starts with provenance known announces it in session_started
                self.hub.publish("session", self.name, {"event": "session_provenance", "session": row})

    def _run_schedule(self, after: tuple[str, int] | None) -> None:
        """Replay: reissue recorded calibration commands right after the same frame.

        Commands are matched by key, not only at the head of the list, so a command
        whose frame never appears cannot block the ones after it.
        """
        due = [c for c in self._schedule
               if (None if c.get("after_boot_id") is None else (c["after_boot_id"], c["after_seq"])) == after]
        for cmd in due:
            self._schedule.remove(cmd)
            if cmd["action"] == "load_calibration":
                rec = replay_calibration(cmd["record"], self._replay_provenance())
                self._emit(self.proc.load_calibration(rec))
            else:
                self._apply_calibration(cmd["action"], scheduled=True)
            self.counters["calibration_commands_replayed"] += 1

    def _replay_provenance(self) -> dict:
        p = self.provenance or {}
        if p.get("source") == "replay":
            return {k: p[k] for k in ("source", "recording_id", "original_source") if k in p}
        prov = {"source": "replay", "recording_id": self.meta.get("recording_id"),
                "original_source": self.meta.get("original_source")}
        return {k: v for k, v in prov.items() if v is not None}

    def _emit(self, outs: list[dict]) -> None:
        if not outs:
            return
        s = self.session
        for out in outs:
            kind = out["type"]
            if kind == "stream_features":
                self.latest[out["stream"]] = out
                if out["stream"] == "emg":
                    act = (out.get("features") or {}).get("emg_activation") or {}
                    cal = act.get("calibration") or {}
                    if cal.get("state"):
                        self.calibration_state = cal["state"]
                    cid = cal.get("calibration_id")
                    if (s is not None and cid and (act.get("normalized") or {}).get("status") in ("ok", "degraded")
                            and cid not in s.calibration_ids):
                        s.calibration_ids.append(cid)  # a calibration that normalised data in this session
            elif kind == "experimental_score":
                self.latest["score"] = out
                if s is not None and s.first_score is None:
                    s.first_score = out
            elif kind == "stream_event":
                ev = out["event"]
                detail = out.get("detail") or {}
                counted = ev in COUNTED_EVENTS and not (ev == "boot_change" and detail.get("previous_boot_id") is None)
                if counted:
                    n = detail.get("missing_records", 1) if ev == "seq_gap" else 1
                    self.events[ev] += n
                    if s is not None:
                        s.events[ev] += n
                if ev == "calibration_state":
                    self.calibration_state = detail.get("state", self.calibration_state)
                    self.calibration_reason = detail.get("reason")
            if s is not None:
                s.rec.derived(out)
            self.hub.publish("derived", self.name, out)
        cal = self.proc.calibration_record()
        if cal is not None and cal.get("calibration_id") != self._cal_id:
            self._cal_id = cal["calibration_id"]
            self.store.save_calibration(cal, self.device_id, (self.provenance or {}).get("source"),
                                        s.session_id if s else None)
        elif cal is None:
            self._cal_id = None
        self._publish_state()

    # ------------------------------------------------------------------ commands (acquisition thread)

    def _apply_calibration(self, action: str, scheduled: bool = False) -> list[dict]:
        if action in ("relaxed", "squeeze"):
            outs = self.proc.calibration_start(action)
        elif action == "cancel":
            outs = self.proc.calibration_cancel()
        else:
            raise ValueError(f"unknown calibration action {action!r}")
        s = self.session
        if s is not None:
            k = s.last_key  # None: before the session's first recorded frame
            s.rec.command({"action": action, "at": utc_now_iso(), "scheduled": scheduled,
                           "after_boot_id": k[0] if k else None, "after_seq": k[1] if k else None})
        self._emit(outs)
        self._publish_state(force=True)
        return outs

    def _start_session(self, label: str | None) -> dict:
        if self.finished:
            raise RuntimeConflict("the source has ended; start a new replay instead")
        if self.session is not None:
            raise RuntimeConflict(f"session {self.session.session_id} is already recording on {self.name}")
        if self.connection != "connected":
            raise RuntimeConflict(f"device on {self.name} is not connected")
        sid = new_id("ws")
        directory = self.data_dir / "sessions" / sid
        rec = SessionRecorder(directory)
        prov = self.provenance or {}
        sess = _ActiveSession(session_id=sid, rec=rec, started_mono=time.monotonic(),
                              start_score=self.latest["score"])
        if self.boot_id:
            sess.boot_ids.append(self.boot_id)
        try:
            row = self.store.create_wearable_session({
                "session_id": sid, "runtime": self.name, "device_id": self.device_id, "state": "recording",
                "label": label, "started_at": utc_now_iso(), "dir": str(directory), "boot_ids": sess.boot_ids})
        except Exception:
            rec.abort()
            raise
        if prov:
            self._session_provenance(sess, prov, announce=False)
            row = self.store.wearable_session(sid)
        # A calibration made before the session is not in its telemetry; keep it so a
        # replay can apply it (re-labelled as replay) before the first frame.
        active = self.proc.calibration_record()
        if active is not None:
            sess.pre_session_calibration = active
            rec.command({"action": "load_calibration", "at": utc_now_iso(), "scheduled": False,
                         "after_boot_id": None, "after_seq": None, "record": active,
                         "note": "calibration active when the session started"})
        if self.calibration_state in CAPTURING:
            sess.counters["started_during_calibration_capture"] += 1
        self.session = sess
        self._write_manifest(sess, row)
        self.hub.publish("session", self.name, {"event": "session_started", "session": row})
        self._publish_state(force=True)
        return row  # type: ignore[return-value]

    def _stop_session(self, state: str = "completed", reason: str = "stopped") -> dict:
        s = self.session
        if s is None:
            raise RuntimeConflict(f"no session is recording on {self.name}")
        self.session = None
        files = s.rec.close()
        mixed = len(s.sources_seen) > 1
        score = session_score_summary(s.start_score, s.first_score, self.latest["score"], mixed_sources=mixed)
        counters = {**dict(s.counters), "frames": s.rec.lines, "derived_records": s.rec.derived_count,
                    "events": dict(s.events), "sources_seen": s.sources_seen,
                    "calibration_ids": s.calibration_ids,
                    "duration_s": round(time.monotonic() - s.started_mono, 3)}
        row = self.store.update_wearable_session(
            s.session_id, state=state, end_reason=reason, ended_at=utc_now_iso(),
            device_id=self.device_id, source=s.source, original_source=s.original_source,
            recording_id=s.recording_id, boot_ids=s.boot_ids,
            calibration_id=self._cal_id or (s.calibration_ids[-1] if s.calibration_ids else None),
            score=score, counters=counters, files=files)
        self._write_manifest(s, row)
        self.hub.publish("session", self.name, {"event": f"session_{state}", "session": row})
        self._publish_state(force=True)
        return row  # type: ignore[return-value]

    def _write_manifest(self, s: _ActiveSession, row: dict | None) -> None:
        """Written when the session starts (so a crash still leaves the processing config a replay
        needs) and rewritten, complete, when it stops."""
        manifest = {"session": row, "processing_config": self.config.to_dict(),
                    "adapter": self.adapter.describe(), "runtime_meta": self.meta,
                    "calibration_record_at_stop": self.proc.calibration_record() if self.session is None else None,
                    "pre_session_calibration": s.pre_session_calibration,
                    "note": "telemetry.ndjson is the device stream exactly as received. A replay "
                            "(provenance=replay) reissues commands.ndjson at the same frames; a "
                            "pre-session calibration is applied as a re-labelled copy."}
        tmp = s.rec.dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2, default=str) + "\n")
        tmp.replace(s.rec.dir / "manifest.json")

    # ------------------------------------------------------------------ public commands

    def calibrate(self, action: str) -> dict:
        def run() -> dict:
            if self.finished:
                raise RuntimeConflict("the source has ended")
            if action in ("relaxed", "squeeze") and self.connection != "connected":
                raise RuntimeConflict(f"device on {self.name} is not connected")
            events = self._apply_calibration(action)
            return {"runtime": self.name, "action": action, "events": events,
                    "calibration": self.status()["calibration"]}
        return self.call(run)

    def start_session(self, label: str | None = None) -> dict:
        return self.call(lambda: self._start_session(label))

    def stop_session(self, session_id: str | None = None, state: str = "completed",
                     reason: str = "stopped_by_user") -> dict:
        def run() -> dict:
            if self.session is None or (session_id and self.session.session_id != session_id):
                raise RuntimeConflict(f"session {session_id} is not recording on {self.name}")
            return self._stop_session(state, reason)
        return self.call(run)

    def is_recording(self, session_id: str) -> bool:
        s = self.session
        return s is not None and s.session_id == session_id

    def inject_disconnect(self, seconds: float) -> dict:
        fn = getattr(self.adapter, "inject_disconnect", None)
        if fn is None:
            raise RuntimeConflict("only simulated devices can fake an unplug")
        return self.call(lambda: (fn(seconds), {"runtime": self.name, "unplugged_for_s": seconds})[1])
