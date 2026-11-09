"""Run a station session against the simulated adapter on virtual time (instant, deterministic)."""

from __future__ import annotations

from typing import Callable

from .adapters import Responder, SimulatedStationAdapter, VirtualClock
from .config import StationConfig
from .engine import StationEngine

# Fixed wall-clock stamp so simulated runs are byte-reproducible.
SIM_WALL_CLOCK = "2026-11-07T12:00:00+00:00"


def drive(engine: StationEngine, adapter, clock: VirtualClock,
          *, abort_after_trials: int | None = None, max_steps: int = 100_000) -> StationEngine:
    """Start ``engine`` and advance virtual time until it reaches a terminal state.

    ``adapter`` is anything with ``next_event_time()``: the simulated adapter, or the
    ``SimulatedController`` behind a ``LinkStationAdapter``. ``abort_after_trials``
    aborts as soon as that many trials have completed.
    """
    engine.start()
    for _ in range(max_steps):
        engine.tick()
        if engine.state != "testing":
            return engine
        if abort_after_trials is not None and len(engine.trials) >= abort_after_trials:
            engine.abort("scripted_abort")
            return engine
        candidates = [t for t in (engine.next_deadline(), adapter.next_event_time()) if t is not None]
        if not candidates:
            raise RuntimeError("simulation stalled: nothing left to wait for")
        clock.advance_to(max(min(candidates), clock()))
    raise RuntimeError("simulation did not finish within max_steps")


def run_simulated(responder: Responder, config: StationConfig | None = None, *, seed: int = 0,
                  session_id: str | None = None, sink: Callable[[dict], None] | None = None,
                  abort_after_trials: int | None = None, detail: dict | None = None,
                  ) -> tuple[StationEngine, SimulatedStationAdapter]:
    clock = VirtualClock()
    adapter = SimulatedStationAdapter(clock, responder, detail=detail)
    engine = StationEngine(adapter, config, clock=clock, wall_clock=lambda: SIM_WALL_CLOCK,
                           session_id=session_id, seed=seed, sink=sink)
    drive(engine, adapter, clock, abort_after_trials=abort_after_trials)
    return engine, adapter
