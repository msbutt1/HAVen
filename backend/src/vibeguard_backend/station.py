"""Station trial engine: deliver stimulus -> wait for felt / not felt -> record the trial.

PROPOSED interface, pending review by Fahad (station) and Salaar (controller firmware).
See backend/STATION_INTERFACE.md. Until the controller contract is agreed, the engine
runs against ``MockStationController``; a real controller adapter implements the same
``StationController`` interface.

Rules the engine enforces (from the project plan):
- A stimulus is a motor SETTING. ``measured_amplitude`` is recorded only when the controller
  actually returns a measurement; otherwise it is absent, never a guess.
- A response is explicit ``felt`` or ``not_felt``. A timeout is ``missing`` (never ``not_felt``).
- Abort stops the motor and closes the open trial as ``missing`` (note ``aborted``); the session
  ends ``aborted``. Repeated commands (same ``request_id``) never duplicate a trial.
- Catch trials (``is_catch``) deliver nothing; the response is still collected.
- Timing comes from the host's monotonic clock: presented_at / responded_at (UTC wall time for
  the record) and response_latency_ms (monotonic).
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .adapters import new_id
from .hub import utc_now_iso
from .runtime import RuntimeConflict
from .service import BadRequest, NotFound, _number

DEFAULT_RESPONSE_TIMEOUT_S = 3.0
MAX_RESPONSE_TIMEOUT_S = 30.0


class StationError(Exception):
    """The controller refused or failed a command (HTTP 502)."""


class StationController(ABC):
    """What the backend needs from the station hardware (proposed; see STATION_INTERFACE.md)."""

    kind = "abstract"

    @abstractmethod
    def deliver(self, stimulus: dict, trial_id: str) -> dict:
        """Start the stimulus. Return an acknowledgement:
        {"accepted": bool, "controller_time_us": int | None,
         "measured_amplitude": {"value", "units", "source"} | None, "error": str | None}
        Must return promptly; ``accepted: False`` means nothing was delivered."""

    @abstractmethod
    def abort(self) -> dict:
        """Stop the motor now. Must always succeed in making the motor safe."""

    def describe(self) -> dict:
        return {"kind": self.kind}


class MockStationController(StationController):
    """Simulated station controller. It never measures amplitude (there is no sensor)."""

    kind = "mock"

    def __init__(self, reject_above: int | None = None, fail: bool = False):
        self.reject_above = reject_above
        self.fail = fail
        self.delivered: list[dict] = []
        self.aborts = 0
        self.motor_on = False

    def deliver(self, stimulus: dict, trial_id: str) -> dict:
        if self.fail:
            raise StationError("mock controller offline")
        setting = stimulus.get("motor_setting")
        if self.reject_above is not None and isinstance(setting, (int, float)) and setting > self.reject_above:
            return {"accepted": False, "controller_time_us": None, "measured_amplitude": None,
                    "error": f"motor_setting {setting} above the limit {self.reject_above}"}
        self.delivered.append({"trial_id": trial_id, "stimulus": dict(stimulus)})
        self.motor_on = True
        return {"accepted": True, "controller_time_us": None, "measured_amplitude": None, "error": None}

    def abort(self) -> dict:
        self.aborts += 1
        self.motor_on = False
        return {"stopped": True}

    def describe(self) -> dict:
        return {"kind": self.kind, "note": "SIMULATED controller; no measured amplitude",
                "delivered": len(self.delivered), "aborts": self.aborts}


@dataclass
class _OpenTrial:
    trial_id: str
    trial_index: int
    stimulus: dict
    is_catch: bool
    measured_amplitude: dict | None
    presented_at: str
    presented_mono: float
    timeout_s: float
    timer: threading.Timer | None = None
    note: str | None = None
    extra: dict = field(default_factory=dict)


class StationEngine:
    def __init__(self, backend: Any, controller: StationController | None = None):
        self.backend = backend
        self.controller = controller or MockStationController()
        self._lock = threading.RLock()
        self._open: dict[str, _OpenTrial] = {}

    # ------------------------------------------------------------------ queries

    def open_trial(self, station_session_id: str) -> dict | None:
        with self._lock:
            t = self._open.get(station_session_id)
            if t is None:
                return None
            return {"trial_id": t.trial_id, "trial_index": t.trial_index, "stimulus": t.stimulus,
                    "is_catch": t.is_catch, "presented_at": t.presented_at,
                    "waiting_s": round(time.monotonic() - t.presented_mono, 3),
                    "timeout_s": t.timeout_s}

    # ------------------------------------------------------------------ commands

    def present(self, station_session_id: str, stimulus: dict, is_catch: bool = False,
                response_timeout_s: float = DEFAULT_RESPONSE_TIMEOUT_S,
                request_id: str | None = None) -> dict:
        """Deliver a stimulus and open a trial that waits for a response."""
        b = self.backend
        timeout_s = _number(response_timeout_s, "response_timeout_s", minimum=0, exclusive=True,
                              maximum=MAX_RESPONSE_TIMEOUT_S)
        if not isinstance(stimulus, dict) or not stimulus:
            raise BadRequest("stimulus must describe the motor setting, e.g. "
                             "{'motor_setting': 120, 'units': 'pwm_duty_0_255'}")
        if not isinstance(is_catch, bool):
            raise BadRequest("is_catch must be true or false")

        def run() -> dict:
            s = b.store.station_session(station_session_id, with_trials=False)
            if s is None:
                raise NotFound(f"no station session {station_session_id!r}")
            if s["state"] != "testing":
                raise RuntimeConflict(f"station session is {s['state']}; no more stimuli")
            with self._lock:
                if station_session_id in self._open:
                    raise RuntimeConflict("a trial is already waiting for a response")
                trial_id = new_id("tr")
                ack = {"accepted": True, "measured_amplitude": None, "error": None}
                if not is_catch:
                    try:
                        ack = self.controller.deliver(stimulus, trial_id)
                    except StationError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - a controller bug must not look like success
                        raise StationError(f"controller error: {type(exc).__name__}: {exc}") from exc
                    if not ack.get("accepted"):
                        raise StationError(ack.get("error") or "the controller refused the stimulus")
                t = _OpenTrial(trial_id=trial_id, trial_index=b.store.next_trial_index(station_session_id),
                               stimulus=stimulus, is_catch=is_catch,
                               measured_amplitude=ack.get("measured_amplitude"),
                               presented_at=utc_now_iso(), presented_mono=time.monotonic(),
                               timeout_s=timeout_s)
                t.timer = threading.Timer(timeout_s, self._on_timeout, args=(station_session_id, trial_id))
                t.timer.daemon = True
                self._open[station_session_id] = t
                t.timer.start()
            out = self.open_trial(station_session_id)
            b.hub.publish("station", None, {"event": "station_stimulus_delivered",
                                            "station_session_id": station_session_id, "trial": out,
                                            "controller": self.controller.describe()})
            return out  # type: ignore[return-value]
        return b._idempotent(request_id, f"present:{station_session_id}", run)

    def respond(self, station_session_id: str, response: str, request_id: str | None = None) -> dict:
        """The wearer answers felt / not_felt for the open trial."""
        b = self.backend
        if response not in ("felt", "not_felt"):
            raise BadRequest("response must be felt or not_felt (a timeout is recorded as missing)")

        def run() -> dict:
            with self._lock:
                t = self._open.pop(station_session_id, None)
                if t is None:
                    raise RuntimeConflict("no trial is waiting for a response (it may have timed out)")
                if t.timer is not None:
                    t.timer.cancel()
            self._stop_motor()
            latency_ms = round((time.monotonic() - t.presented_mono) * 1000, 1)
            return self._record(station_session_id, t, response, responded_at=utc_now_iso(),
                                latency_ms=latency_ms)
        return b._idempotent(request_id, f"respond:{station_session_id}", run)

    def abort(self, station_session_id: str, reason: str | None = None,
              request_id: str | None = None) -> dict:
        """Stop the motor, close any open trial as missing, end the session as aborted."""
        b = self.backend

        def run() -> dict:
            self._stop_motor()  # first: make the hardware safe, whatever else happens
            with self._lock:
                t = self._open.pop(station_session_id, None)
                if t is not None and t.timer is not None:
                    t.timer.cancel()
            if t is not None:
                self._record(station_session_id, t, "missing", note="aborted before a response")
            return b.finish_station_session(station_session_id, "aborted", reason or "aborted_by_operator")
        return b._idempotent(request_id, f"station_abort:{station_session_id}", run)

    def complete(self, station_session_id: str, reason: str | None = None) -> dict:
        with self._lock:
            if station_session_id in self._open:
                raise RuntimeConflict("a trial is still waiting for a response; respond, wait for the "
                                      "timeout, or abort")
        return self.backend.finish_station_session(station_session_id, "completed", reason)

    def shutdown(self) -> None:
        """Backend stopping: make the motor safe and close open trials as missing."""
        self._stop_motor()
        with self._lock:
            open_ = list(self._open.items())
            self._open.clear()
        for sid, t in open_:
            if t.timer is not None:
                t.timer.cancel()
            try:
                self._record(sid, t, "missing", note="backend stopped before a response")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ internals

    def _stop_motor(self) -> None:
        try:
            self.controller.abort()
        except Exception:  # noqa: BLE001 - never let a stop failure hide the original outcome
            self.backend.hub.publish("station", None, {"event": "station_controller_error",
                                                       "error": "abort failed"})

    def _on_timeout(self, station_session_id: str, trial_id: str) -> None:
        with self._lock:
            t = self._open.get(station_session_id)
            if t is None or t.trial_id != trial_id:
                return  # answered or aborted first
            del self._open[station_session_id]
        self._stop_motor()
        try:
            self._record(station_session_id, t, "missing", note=f"no response within {t.timeout_s:g} s")
        except Exception:  # noqa: BLE001 - e.g. the session was finished meanwhile
            pass

    def _record(self, sid: str, t: _OpenTrial, response: str, responded_at: str | None = None,
                latency_ms: float | None = None, note: str | None = None) -> dict:
        trial = self.backend.add_trial(
            sid, t.stimulus, response, trial_index=t.trial_index, measured_amplitude=t.measured_amplitude,
            is_catch=t.is_catch, presented_at=t.presented_at, responded_at=responded_at,
            response_latency_ms=latency_ms, note=note or t.note)
        return trial
