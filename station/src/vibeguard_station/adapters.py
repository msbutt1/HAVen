"""Station adapter interface and the simulated adapter.

The engine only talks to this interface, so the simulated adapter and the real
one (controller firmware over USB serial, see ``link.py``) are interchangeable.

Adapter contract:
- ``deliver_stimulus`` starts a stimulus (setting 0 = motor off, used for catch
  trials). It must not block; failures raise ``AdapterError``.
- ``stop_stimulus`` stops the motor NOW. It is safe to call at any time and
  raises ``AdapterError`` only if the stop could not be confirmed.
- ``poll`` returns the events that happened since the last call, each stamped
  with a time on the engine's clock. It must not block.
- ``provenance`` says where the data comes from (``live`` only for real hardware;
  the simulated adapter can never claim it).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .config import CATCH_SETTING


class AdapterError(Exception):
    """The adapter or controller failed (link down, command rejected, stop unconfirmed)."""


@dataclass(frozen=True)
class AdapterEvent:
    kind: str  # "response" | "stimulus_started" | "stimulus_ended" | "fault"
    t_s: float  # engine-clock time the event happened (or was received)
    response: str | None = None  # "felt" | "not_felt" for kind == "response"
    source: str | None = None  # response source, e.g. "button"
    trial_id: str | None = None
    detail: str | None = None


class StationAdapter(Protocol):
    @property
    def provenance(self) -> dict: ...

    def deliver_stimulus(self, trial_id: str, setting: int, duration_ms: int) -> None: ...

    def stop_stimulus(self) -> None: ...

    def poll(self) -> list[AdapterEvent]: ...


# -- virtual time ---------------------------------------------------------------

class VirtualClock:
    """Manually advanced clock so simulated tests are instant and deterministic."""

    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance_to(self, t: float) -> None:
        if t < self._t:
            raise ValueError("virtual time cannot go backwards")
        self._t = float(t)

    def advance(self, dt: float) -> None:
        self.advance_to(self._t + dt)


# -- simulated responders -------------------------------------------------------

@dataclass(frozen=True)
class SimResponse:
    response: str  # "felt" | "not_felt"
    delay_s: float = 0.6  # after the stimulus command


# A responder decides what the simulated person does: (trial_index, setting) -> response or None.
Responder = Callable[[int, int], "SimResponse | None"]


class ScriptedResponder:
    """Replays a fixed list, one item per trial: "felt", "not_felt", None (no response),
    or a ``SimResponse``. Raises if the script runs out, so tests fail loudly."""

    def __init__(self, script, delay_s: float = 0.6):
        self.script = list(script)
        self.delay_s = delay_s
        self.calls: list[tuple[int, int]] = []

    def __call__(self, trial_index: int, setting: int) -> SimResponse | None:
        self.calls.append((trial_index, setting))
        if trial_index > len(self.script):
            raise IndexError(f"script exhausted at trial {trial_index}")
        item = self.script[trial_index - 1]
        if item is None:
            return None
        if isinstance(item, SimResponse):
            return item
        return SimResponse(item, self.delay_s)


class SimulatedSubject:
    """Synthetic person: logistic psychometric curve around ``true_threshold``.

    Pure software, so any result is labelled simulated. The model says nothing
    about real perception; it exists to exercise the staircase.
    """

    def __init__(self, true_threshold: float = 35.0, slope: float = 4.0,
                 miss_rate: float = 0.0, false_positive_rate: float = 0.0,
                 delay_s: float = 0.6, seed: int = 0):
        self.true_threshold = true_threshold
        self.slope = slope
        self.miss_rate = miss_rate
        self.false_positive_rate = false_positive_rate
        self.delay_s = delay_s
        self._rng = random.Random(seed)

    def p_felt(self, setting: int) -> float:
        if setting == CATCH_SETTING:
            return self.false_positive_rate
        z = (setting - self.true_threshold) / self.slope
        return 1.0 / (1.0 + math.exp(-z))

    def __call__(self, trial_index: int, setting: int) -> SimResponse | None:
        # Fixed draw order keeps runs reproducible for a given seed.
        u_miss, u_felt, u_delay = self._rng.random(), self._rng.random(), self._rng.random()
        if u_miss < self.miss_rate:
            return None
        felt = u_felt < self.p_felt(setting)
        return SimResponse("felt" if felt else "not_felt", self.delay_s * (0.75 + 0.5 * u_delay))


# -- simulated adapter ----------------------------------------------------------

class SimulatedStationAdapter:
    """Simulated motor and felt/not-felt buttons. Same interface the real adapter uses.

    ``clock`` must be the engine's clock (use ``VirtualClock`` for instant runs).
    Fault switches let tests exercise error paths.
    """

    def __init__(self, clock: Callable[[], float], responder: Responder, *,
                 detail: dict | None = None):
        self._clock = clock
        self._responder = responder
        self._detail = dict(detail or {})
        self._events: list[AdapterEvent] = []  # scheduled; delivered by poll() once due
        self._trial_no = 0
        self.motor_on = False
        self.motor_setting = 0
        self._motor_trial: str | None = None
        self.commands: list[dict] = []  # everything the engine asked of the "hardware"
        self.fail_on_deliver = False
        self.fail_on_stop = False
        self.fail_on_poll = False

    @property
    def provenance(self) -> dict:
        prov = {"source": "simulated"}
        if self._detail:
            prov["detail"] = dict(self._detail)
        return prov

    def deliver_stimulus(self, trial_id: str, setting: int, duration_ms: int) -> None:
        if self.fail_on_deliver:
            raise AdapterError("simulated deliver failure")
        now = self._clock()
        self._trial_no += 1
        self.commands.append({"type": "deliver_stimulus", "trial_id": trial_id,
                              "setting": setting, "duration_ms": duration_ms, "t_s": now})
        self.motor_setting = setting
        self.motor_on = setting != CATCH_SETTING
        self._motor_trial = trial_id
        self._events.append(AdapterEvent("stimulus_started", now, trial_id=trial_id))
        self._events.append(AdapterEvent("stimulus_ended", now + duration_ms / 1000.0, trial_id=trial_id))
        resp = self._responder(self._trial_no, setting)
        if resp is not None:
            self._events.append(AdapterEvent("response", now + resp.delay_s, response=resp.response,
                                             source="button", trial_id=trial_id))

    def stop_stimulus(self) -> None:
        self.commands.append({"type": "stop_stimulus", "t_s": self._clock()})
        if self.fail_on_stop:
            raise AdapterError("simulated stop failure")
        self.motor_on = False
        self.motor_setting = 0
        # Anything scheduled for the stopped trial never happens.
        self._events = []

    def poll(self) -> list[AdapterEvent]:
        if self.fail_on_poll:
            raise AdapterError("simulated link failure")
        now = self._clock()
        due = sorted((e for e in self._events if e.t_s <= now), key=lambda e: e.t_s)
        self._events = [e for e in self._events if e.t_s > now]
        for e in due:
            if e.kind == "stimulus_ended" and e.trial_id == self._motor_trial:
                self.motor_on = False
                self.motor_setting = 0
        return due

    def next_event_time(self) -> float | None:
        return min((e.t_s for e in self._events), default=None)

    # Test helper: a person pressing a button when no trial is waiting.
    def inject_response(self, response: str, delay_s: float = 0.0, source: str = "button") -> None:
        self._events.append(AdapterEvent("response", self._clock() + delay_s, response=response,
                                         source=source))
