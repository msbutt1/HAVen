"""Station test engine: a clock-driven state machine, independent of UI and hardware.

States::

    ready --start--> testing --stop rule met--> completed
    testing --abort--> aborted
    testing --adapter failure / unconfirmed stop--> error

Inside ``testing`` the engine cycles between trials::

    between_trials --inter-trial delay--> awaiting_response
    awaiting_response --response--> between_trials (or a terminal state)
    awaiting_response --window elapsed--> missing response --> between_trials

Rules:
- A timeout is recorded as ``missing`` and never as ``not_felt``; the same level
  is presented again and the staircase does not move.
- Catch trials (setting 0, no stimulus) never move the staircase. A "felt" on a
  catch trial is a catch-trial failure, reported as a quality limitation.
- A response is accepted only for the trial currently waiting and only inside its
  window. Late, stale (wrong trial) and unsolicited responses are ignored and
  counted, never attributed to a later trial.
- ``abort`` stops the motor first, then ends the session. If the stop cannot be
  confirmed the session ends in ``error``, not ``aborted``.
- The result is built once, on reaching a terminal state, and handed to the sink
  (``ResultStore.save`` or any callable) exactly once.

The engine never sleeps. The caller drives it with ``tick()`` (and
``submit_response()`` / ``abort()``), so UIs, serial loops and tests all use it
the same way. Times are seconds on the injected ``clock``; adapters must stamp
their events on that same clock.
"""

from __future__ import annotations

import copy
import math
import random
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from .adapters import AdapterError, AdapterEvent, StationAdapter
from .config import CATCH_SETTING, StationConfig
from .store import AlreadySavedError
from .records import (EVENT_SCHEMA, RESPONSES, VERSION, Counters, TrialRecord, build_result)
from .staircase import Staircase

STIMULUS_MARGIN_S = 0.05
STATES = ("ready", "testing", "completed", "aborted", "error")
SOURCES = ("live", "simulated", "replay")
# 58, not 64: trial ids are "<session_id>-tNNN" and must fit the 64-character id limit that the
# controller enforces.
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,58}$")


class StationStateError(Exception):
    """A command was issued in a state that does not allow it."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StationEngine:
    def __init__(self, adapter: StationAdapter, config: StationConfig | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], str] = _utc_now,
                 session_id: str | None = None, seed: int = 0,
                 sink: Callable[[dict], None] | None = None):
        self.cfg = config if config is not None else StationConfig.full()
        self.cfg.validate()
        self.adapter = adapter
        self._clock = clock
        self._wall = wall_clock
        self.session_id = f"station-{uuid.uuid4().hex[:12]}" if session_id is None else session_id
        if not _ID.match(self.session_id):
            raise ValueError("session_id must be 1-58 characters of [A-Za-z0-9._:-]")
        prov = copy.deepcopy(adapter.provenance)
        if prov.get("source") not in SOURCES:
            raise ValueError(f"adapter provenance.source must be one of {SOURCES}")
        self.provenance = prov
        self._rng = random.Random(seed)
        self._sink = sink

        self.state = "ready"
        self.stair = Staircase(self.cfg)
        self.trials: list[TrialRecord] = []
        self.result: dict | None = None
        self.save_error: str | None = None
        self.saved = False  # the sink accepted the result (once; never retried after success)
        self.already_saved = False  # the sink reported that this session id already had a result

        self._counters = Counters()
        self._events: list[dict] = []  # not yet drained by the caller
        self.event_log: list[dict] = []  # every event of the session, in order
        self._event_seq = 0
        self._t0: float | None = None
        self._started_at: str | None = None
        self._current: TrialRecord | None = None
        self._cmd_t: float | None = None  # absolute clock time of the current stimulus command
        self._deadline: float | None = None
        self._pending_at: float | None = None
        self._consecutive_missing = 0
        self._stimulus_trials = 0
        self._last_was_catch = False

    # -- public commands --------------------------------------------------------

    def start(self) -> None:
        """ready -> testing. The first trial begins on the next ``tick()``."""
        if self.state != "ready":
            raise StationStateError(f"cannot start from state {self.state!r}")
        self._t0 = self._clock()
        self._started_at = self._wall()
        self.state = "testing"
        self._pending_at = self._t0
        self._emit("state_changed", previous="ready", current="testing", mode=self.cfg.mode)

    def tick(self) -> list[dict]:
        """Advance the machine to the current clock time. Returns new events."""
        if self.state != "testing":
            return self.drain_events()
        for _ in range(1000):
            try:
                events = self.adapter.poll()
            except Exception as exc:  # any adapter failure ends the session visibly
                self._fail(f"{type(exc).__name__}: {exc}" if not isinstance(exc, AdapterError) else str(exc))
                break
            progressed = False
            for ev in events:
                if self.state != "testing":
                    break
                progressed = True
                self._handle_adapter_event(ev)
            if self.state != "testing":
                break
            now = self._clock()
            if self._current is not None and now >= self._deadline:
                self._close_missing()
                progressed = True
            if (self.state == "testing" and self._current is None
                    and self._pending_at is not None and now >= self._pending_at):
                self._begin_trial(now)
                progressed = True
            if not progressed or self.state != "testing":
                break
        return self.drain_events()

    def submit_response(self, response: str, source: str = "button", t: float | None = None) -> None:
        """Deliver a felt/not-felt response from a UI or button handler.

        Ignored if the session is not in ``testing``. ``t`` defaults to now.
        """
        if response not in RESPONSES:
            raise ValueError(f"response must be one of {RESPONSES}")
        if t is not None and not math.isfinite(t):
            raise ValueError("t must be a finite time on the engine clock")
        self._on_response(response, self._clock() if t is None else t, source, None)

    def abort(self, reason: str = "user_abort") -> bool:
        """Stop stimulation immediately and end the session. Returns True if a session ended.

        The motor stop is always sent, even if no test is running. In a terminal
        state this is a no-op that still re-sends the stop.
        """
        stop_error = None
        try:
            self.adapter.stop_stimulus()
        except Exception as exc:  # the stop must be reported, whatever went wrong
            stop_error = str(exc) or "stop could not be confirmed"
        if self.state != "testing":
            return False
        if self._current is not None:
            self._close_current(status="aborted", note=f"aborted: {reason}")
        if stop_error is not None:
            self._counters.stop_not_confirmed = True
            self._finish("error", "abort_stop_not_confirmed", abort_reason=reason, error=stop_error)
        else:
            self._finish("aborted", "aborted", abort_reason=reason)
        return True

    def retry_save(self) -> bool:
        """Retry handing the result to the sink after a failed save. Never saves twice."""
        if self.result is None or self._sink is None or self.saved:
            return False
        self._save()
        return self.saved

    # -- inspection -------------------------------------------------------------

    def next_deadline(self) -> float | None:
        """Absolute clock time of the next thing the engine is waiting for, if any."""
        if self.state != "testing":
            return None
        return self._deadline if self._current is not None else self._pending_at

    def snapshot(self) -> dict:
        """Current status for screens. Display-only; the result object is the record."""
        if self.state != "testing":
            phase = "done" if self.state in ("completed", "aborted", "error") else "idle"
        else:
            phase = "awaiting_response" if self._current is not None else "between_trials"
        cur = self._current
        return {
            "session_id": self.session_id, "state": self.state, "phase": phase,
            "mode": self.cfg.mode, "is_preview": self.cfg.mode == "preview",
            "provenance": copy.deepcopy(self.provenance),
            "trial_index": len(self.trials) + (1 if cur is not None else 0),
            "trials_completed": len(self.trials),
            "current_trial_id": cur.trial_id if cur else None,
            "current_kind": cur.kind if cur else None,
            "current_setting": cur.stimulus_setting if cur else None,
            "stimulus_units": "motor_setting",
            "reversals": self.stair.reversals, "reversals_required": self.cfg.reversals_to_stop,
            "outcome": self.result["outcome"] if self.result else None,
        }

    def drain_events(self) -> list[dict]:
        out, self._events = self._events, []
        return out

    # -- internals --------------------------------------------------------------

    def _rel(self, t: float | None) -> float | None:
        return None if t is None or self._t0 is None else t - self._t0

    def _emit(self, event_type: str, /, **fields) -> None:
        self._event_seq += 1
        ev = {"schema": EVENT_SCHEMA, "version": VERSION, "type": event_type,
              "session_id": self.session_id, "seq": self._event_seq,
              "t_s": round(self._rel(self._clock()) or 0.0, 4), "state": self.state,
              "provenance": copy.deepcopy(self.provenance)}
        ev.update(fields)
        self._events.append(ev)
        self.event_log.append(ev)

    def _should_catch(self) -> bool:
        if self.cfg.catch_probability <= 0 or self._last_was_catch:
            return False
        if self._stimulus_trials < self.cfg.catch_min_stimulus_trials:
            return False
        return self._rng.random() < self.cfg.catch_probability

    def _begin_trial(self, now: float) -> None:
        catch = self._should_catch()
        setting = CATCH_SETTING if catch else self.stair.setting
        index = len(self.trials) + 1
        trial = TrialRecord(trial_id=f"{self.session_id}-t{index:03d}", index=index,
                            kind="catch" if catch else "stimulus", stimulus_setting=setting,
                            stimulus_ms=self.cfg.stimulus_ms, command_t_s=self._rel(now))
        self._pending_at = None
        try:
            self.adapter.deliver_stimulus(trial.trial_id, setting, self.cfg.stimulus_ms)
        except Exception as exc:
            trial.response_status = "error"
            trial.note = f"stimulus command failed: {exc}"
            self.trials.append(trial)
            self._emit("trial_completed", trial=trial.to_dict())
            self._fail(str(exc))
            return
        self._last_was_catch = catch
        if not catch:
            self._stimulus_trials += 1
        self._current = trial
        self._cmd_t = now
        self._deadline = now + self.cfg.response_window_s
        self._emit("trial_started", trial_id=trial.trial_id, index=index, kind=trial.kind,
                   stimulus_setting=setting, stimulus_units="motor_setting",
                   response_window_s=self.cfg.response_window_s)

    def _handle_adapter_event(self, ev: AdapterEvent) -> None:
        cur = self._current
        if ev.kind == "stimulus_started":
            if cur is not None and ev.trial_id in (None, cur.trial_id):
                cur.stimulus_started_t_s = self._rel(ev.t_s)
        elif ev.kind == "stimulus_ended":
            pass
        elif ev.kind == "fault":
            self._fail(ev.detail or "controller fault")
        elif ev.kind == "response":
            if ev.response not in RESPONSES or not isinstance(ev.t_s, (int, float))                     or not math.isfinite(ev.t_s):
                self._fail(f"adapter sent an invalid response event ({ev.response!r}, t={ev.t_s!r})")
                return
            self._on_response(ev.response, ev.t_s, ev.source or "button", ev.trial_id)
        else:
            self._fail(f"adapter sent unknown event kind {ev.kind!r}")

    def _ignore_response(self, reason: str, response: str, t: float) -> None:
        if reason == "no_trial_waiting":
            self._counters.unsolicited_responses_ignored += 1
        elif reason == "duplicate":
            self._counters.duplicate_responses_ignored += 1
        else:
            self._counters.late_responses_ignored += 1
        self._emit("response_ignored", reason=reason, response=response, response_t_s=round(self._rel(t), 4))

    def _on_response(self, response: str, t: float, source: str, trial_id: str | None) -> None:
        if self.state != "testing":
            return  # session already over (e.g. a button press racing an abort): ignored
        cur = self._current
        closed = next((tr for tr in self.trials if tr.trial_id == trial_id), None)             if trial_id is not None else None
        # A second answer for a trial that already has one is a duplicate; for a trial that
        # timed out it is late.
        closed_reason = "duplicate" if closed is not None and closed.response_status == "received" else "late"
        if cur is None:
            self._ignore_response(closed_reason if closed else "no_trial_waiting", response, t)
            return
        if trial_id is not None and trial_id != cur.trial_id:
            self._ignore_response(closed_reason if closed else "stale_trial", response, t)
            return
        if t < self._cmd_t:
            self._ignore_response("stale_trial", response, t)
            return
        if t >= self._deadline:  # the window is half-open: [command, command + window)
            # Count first: closing the trial may end the session and build the result.
            self._ignore_response("late", response, t)
            self._close_missing()
            return
        cur.response = response
        cur.response_status = "received"
        cur.response_source = source
        cur.response_t_s = self._rel(t)
        cur.latency_ms = (t - self._cmd_t) * 1000.0
        felt = response == "felt"
        if cur.kind == "catch":
            cur.catch_outcome = "fail" if felt else "pass"
        else:
            step = self.stair.record(felt)
            cur.staircase = {"direction": step.direction, "reversal": step.reversal,
                             "reversal_level": step.reversal_level, "next_setting": step.next_setting}
            if step.reversal:
                self._emit("reversal", trial_id=cur.trial_id, level=step.reversal_level,
                           count=self.stair.reversals, required=self.cfg.reversals_to_stop)
        self._consecutive_missing = 0
        self._close_current()

    def _close_missing(self) -> None:
        self._consecutive_missing += 1
        self._close_current(status="missing", note="no response within the window")

    def _close_current(self, status: str | None = None, note: str | None = None) -> None:
        """Finish the in-flight trial, then apply the stop rules or schedule the next one."""
        trial = self._current
        if status is not None:
            trial.response_status = status
        if note is not None:
            trial.note = note
        stimulus_end = self._cmd_t + self.cfg.stimulus_ms / 1000.0  # the motor may still be running
        self._current = None
        self._deadline = None
        self._cmd_t = None
        self.trials.append(trial)
        self._emit("trial_completed", trial=trial.to_dict())
        if trial.response_status in ("aborted", "error"):
            return  # the caller ends the session
        reason = self.stair.stop_reason()
        if reason is None and self._consecutive_missing >= self.cfg.max_consecutive_missing:
            reason = "too_many_missing_responses"
        if reason is None and len(self.trials) >= self.cfg.max_trials:
            reason = "max_trials_reached"
        if reason is not None:
            self._finish("completed", reason)
        else:
            # Never start the next stimulus while the previous one is still running (the controller
            # would reject it as busy), however quickly the person answered.
            # A small margin covers link latency between our clock and the controller's timer.
            self._pending_at = max(self._clock() + self.cfg.inter_trial_s, stimulus_end + STIMULUS_MARGIN_S)

    def _fail(self, error: str) -> None:
        """Adapter failure: try to stop the motor, close the trial, end in ``error``."""
        try:
            self.adapter.stop_stimulus()
        except Exception:
            self._counters.stop_not_confirmed = True
        if self._current is not None:
            self._close_current(status="error", note=f"adapter error: {error}")
        self._finish("error", "adapter_error", error=error)

    def _finish(self, state: str, outcome: str, abort_reason: str | None = None,
                error: str | None = None) -> None:
        if self.result is not None:
            return
        previous = self.state
        self._current = None
        self._deadline = None
        self._pending_at = None
        if state == "completed":
            try:  # the last answer may have come mid-stimulus: make sure the motor is off
                self.adapter.stop_stimulus()
            except Exception:
                self._counters.stop_not_confirmed = True
        self.state = state
        duration = self._clock() - self._t0
        self.result = build_result(
            session_id=self.session_id, cfg=self.cfg, state=state, outcome=outcome,
            provenance=copy.deepcopy(self.provenance), trials=self.trials, stair=self.stair,
            started_at=self._started_at, ended_at=self._wall(), duration_s=duration,
            counters=self._counters, abort_reason=abort_reason, error=error)
        self._emit("state_changed", previous=previous, current=state, outcome=outcome)
        self._emit("session_result", result=copy.deepcopy(self.result))
        self._save()

    def _save(self) -> None:
        if self._sink is None or self.saved:
            return
        try:
            self._sink(copy.deepcopy(self.result))
        except AlreadySavedError:
            self.saved = True
            self.already_saved = True
            self.save_error = None
            self._emit("result_already_saved", session_id=self.session_id)
            return
        except Exception as exc:  # the sink is caller code; never lose the result over it
            self.save_error = f"{type(exc).__name__}: {exc}"
            self._emit("result_save_failed", error=self.save_error)
            return
        self.saved = True
        self.save_error = None
        self._emit("result_saved", session_id=self.session_id)
