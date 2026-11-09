"""Hardware self-test for the station controller (run this first after wiring).

Checks, in order: the controller answers ``hello`` (and reports its motor-setting range),
a ``stop_stimulus`` is confirmed, the motor accepts and completes pulses at low / medium /
high settings, and both buttons are seen. Whether the motor is actually felt is a human
check: the program says so rather than pretending to know.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .adapters import AdapterError, AdapterEvent
from .link import LinkStationAdapter


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _wait_for(adapter: LinkStationAdapter, clock, sleep, timeout_s: float, want) -> AdapterEvent | None:
    """Poll until ``want(event)`` matches or a fault arrives. Returns that event, or None on timeout."""
    deadline = clock() + timeout_s
    while True:
        for ev in adapter.poll():
            if ev.kind == "fault" or want(ev):
                return ev
        if clock() >= deadline:
            return None
        sleep(0.01)


def selftest(adapter: LinkStationAdapter, clock: Callable[[], float], sleep: Callable[[float], None],
             say: Callable[[str], None] = print, *, motor: bool = True, button_wait_s: float = 15.0,
             pulse_ms: int = 700, prompt: Callable[[str], None] | None = None) -> list[Check]:
    checks: list[Check] = []

    def record(name, ok, detail=""):
        checks.append(Check(name, ok, detail))
        say(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    try:
        info = adapter.request_hello()
        rng, fw = info["setting_range"], info["firmware"]
        record("controller answers hello", True,
               f"{fw['name']} {fw['version']}, settings {rng['min']}-{rng['max']}")
    except AdapterError as exc:
        record("controller answers hello", False, str(exc))
        return checks  # nothing else can work

    try:
        adapter.stop_stimulus()
        record("stop_stimulus is confirmed", True)
    except AdapterError as exc:
        record("stop_stimulus is confirmed", False, str(exc))

    if motor:
        lo, hi = rng["min"], rng["max"]
        for label, frac in (("low", 0.25), ("medium", 0.5), ("high", 0.75)):
            setting = lo + round(frac * (hi - lo))
            say(f"  ... pulsing the motor at the {label} setting ({setting}): touch it and confirm you feel it")
            try:
                adapter.deliver_stimulus(f"selftest-{label}", setting, pulse_ms)
                started = _wait_for(adapter, clock, sleep, 2.0, lambda e: e.kind == "stimulus_started")
                if started is None or started.kind == "fault":
                    record(f"motor pulse ({label}) starts", False,
                           (started.detail if started else None) or "no stimulus_started")
                else:
                    ended = _wait_for(adapter, clock, sleep, pulse_ms / 1000 + 2.0,
                                      lambda e: e.kind == "stimulus_ended")
                    ok = ended is not None and ended.kind == "stimulus_ended"
                    record(f"motor pulse ({label}) starts and ends", ok, "" if ok else "no stimulus_ended")
                adapter.stop_stimulus()
            except AdapterError as exc:
                record(f"motor pulse ({label})", False, str(exc))
        say("  (whether the vibration is actually felt is a human check: this test cannot verify it)")

    for resp in ("felt", "not_felt"):
        say(f"  ... press the {resp.upper().replace('_', ' ')} button now (within {button_wait_s:.0f} s)")
        if prompt:
            prompt(resp)
        ev = _wait_for(adapter, clock, sleep, button_wait_s, lambda e: e.kind == "response")
        if ev is not None and ev.kind == "response":
            record(f"{resp} button seen", ev.response == resp,
                   f"controller reported {ev.response!r}" + ("" if ev.response == resp else " (buttons swapped?)"))
        else:
            record(f"{resp} button seen", False, (ev.detail if ev else None) or "no press detected")
    return checks
