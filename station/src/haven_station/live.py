"""Real-time runner: drive an engine with the wall clock until the session ends.

The engine itself never sleeps; this is the thin loop a station program (or the
backend) runs. Ctrl+C aborts, which stops the motor first.
"""

from __future__ import annotations

import time
from typing import Callable

from .engine import StationEngine


def run_live(engine: StationEngine, *, poll_interval_s: float = 0.02,
             sleep: Callable[[float], None] = time.sleep,
             on_events: Callable[[list[dict]], None] | None = None,
             should_abort: Callable[[], bool] | None = None) -> dict:
    """Start the engine and tick it until it reaches a terminal state. Returns the result."""
    def flush(events: list[dict]) -> None:
        if events and on_events:
            on_events(events)

    engine.start()
    try:
        while True:
            flush(engine.tick())
            if engine.state != "testing":
                break
            if should_abort is not None and should_abort():
                engine.abort("operator_abort")
                break
            sleep(poll_interval_s)
    except KeyboardInterrupt:
        try:
            engine.abort("keyboard_interrupt")
        except KeyboardInterrupt:  # a second Ctrl+C while waiting for the stop confirmation
            pass
    if engine.result is None:      # never return with the session still open
        engine.abort("interrupted")
    flush(engine.drain_events())
    return engine.result
