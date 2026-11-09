"""Shared fixtures for backend tests. Everything runs on SIMULATED data, offline."""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterable

import pytest

from haven_signals import contract
from haven_signals.simulator import SimConfig, Simulation
from haven_backend.adapters import AdapterDisconnected, DeviceAdapter, EndOfStream, SimAdapter
from haven_backend.service import Backend, processing_config_for

FIXTURES = Path(__file__).resolve().parents[2] / "signals" / "contract" / "fixtures"


@pytest.fixture(autouse=True)
def _no_bonjour_on_the_network(monkeypatch):
    """`serve` with a LAN --host announces itself by multicast DNS (bonjour.py). Tests never do: the announcer is
    replaced by one that does nothing (test_app_discovery.py checks the wiring with its own stand-in)."""
    from haven_backend import bonjour

    class QuietAnnouncer:
        def __init__(self, http_port, password, name=None, addresses=None):
            self.name, self.addresses, self.error = "test", [], "disabled in tests"

        def start(self):
            return False

        def stop(self):
            pass

    monkeypatch.setattr(bonjour, "Announcer", QuietAnnouncer)


def wait_until(pred: Callable[[], bool], timeout: float = 10.0, every: float = 0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(every)
    return pred()


def sim_frames(scenario: str = "demo", duration_s: float = 30.0, seed: int = 1,
               run_id: str | None = "t1") -> list[bytes]:
    """Frames a host would receive from the simulator (lost records already absent)."""
    sim = Simulation(SimConfig(scenario=scenario, duration_s=duration_s, seed=seed), run_id=run_id)
    return [contract.serialize_record(r.record).encode() for r in sim.records() if not r.lost]


class ScriptedAdapter(DeviceAdapter):
    """Test adapter driven by a script of items:

    bytes             -> delivered by read_line
    "disconnect"      -> read_line raises AdapterDisconnected once; open() then fails
                         until ``reconnect()`` is called (or immediately if auto_reconnect)
    ("pause", secs)   -> read_line returns None for that long (silence)
    ("gate", event)   -> blocks delivery until the threading.Event is set
    end of script     -> EndOfStream (or silence forever if ``hold_open``)
    """

    kind = "scripted"

    def __init__(self, items: Iterable, clock: str = "host", auto_reconnect: bool = True,
                 hold_open: bool = False):
        self.items = deque(items)
        self.clock = clock
        self.auto_reconnect = auto_reconnect
        self.hold_open = hold_open
        self.connected = False
        self.open_calls = 0
        self._pause_until = 0.0

    def open(self) -> None:
        self.open_calls += 1
        if not self.connected and not self.auto_reconnect and self.open_calls > 1:
            raise AdapterDisconnected("scripted: still unplugged")
        self.connected = True

    def reconnect(self) -> None:
        self.auto_reconnect = True

    def read_line(self, timeout: float) -> bytes | None:
        if not self.connected:
            raise AdapterDisconnected("scripted: not open")
        now = time.monotonic()
        if now < self._pause_until:
            time.sleep(min(timeout, self._pause_until - now))
            return None
        if not self.items:
            if self.hold_open:
                time.sleep(timeout)
                return None
            raise EndOfStream
        item = self.items[0]
        if isinstance(item, (bytes, bytearray)):
            self.items.popleft()
            return bytes(item)
        if item == "disconnect":
            self.items.popleft()
            self.connected = False
            raise AdapterDisconnected("scripted unplug")
        if isinstance(item, tuple) and item[0] == "pause":
            self.items.popleft()
            self._pause_until = time.monotonic() + item[1]
            return None
        if isinstance(item, tuple) and item[0] == "gate":
            if item[1].wait(timeout):
                self.items.popleft()
            return None
        raise ValueError(f"bad script item {item!r}")


@pytest.fixture(autouse=True)
def _websocket_host_is_localhost(monkeypatch):
    """Starlette's test client hard-codes the host 'testserver' for WebSocket URLs; the backend (rightly)
    only accepts local Host headers, like a real browser would send."""
    from starlette.testclient import TestClient

    original = TestClient.websocket_connect

    def connect(self, url, *args, **kwargs):
        if url.startswith("/"):
            url = "ws://localhost" + url
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "websocket_connect", connect)


@pytest.fixture
def backend(tmp_path: Path):
    b = Backend(tmp_path / "data")
    yield b
    b.shutdown()


@pytest.fixture
def sim_config():
    return processing_config_for("sim")


__all__ = ["FIXTURES", "ScriptedAdapter", "SimAdapter", "sim_frames", "wait_until"]
