"""Live delivery (hub + WebSocket) and acquisition independence.

The hub fans out from acquisition threads to WebSocket clients. Two promises are
checked here:

1. A dashboard gets an honest stream: the channels and runtimes it asked for, in
   envelopes it can reason about, and an exact account of anything it missed.
2. Acquisition never waits for a client: a slow or stuck client costs that
   client data, never the device stream.

Hub unit tests run inside ``asyncio.run`` (no pytest plugin needed).
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from conftest import ScriptedAdapter, SimAdapter, sim_frames, wait_until
from vibeguard_backend.app import create_app
from vibeguard_backend.hub import CHANNELS, DEFAULT_CHANNELS, Hub
from vibeguard_backend.service import Backend


# ---------------------------------------------------------------------------- helpers

async def _settle(pred, rounds: int = 200) -> bool:
    """Let the loop run the callbacks publishers scheduled with call_soon_threadsafe."""
    for _ in range(rounds):
        if pred():
            return True
        await asyncio.sleep(0)
    return pred()


def _drain(sub) -> list[dict]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


WATCHDOG = "_test_watchdog"


def read_until(ws, hub: Hub, pred, channel: str, timeout: float = 10.0) -> list[dict]:
    """Receive until ``pred(msg)``; a timer publishes a marker so a receive never hangs."""
    timer = threading.Timer(timeout, lambda: hub.publish(channel, None, {WATCHDOG: True}))
    timer.daemon = True
    timer.start()
    msgs: list[dict] = []
    try:
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if isinstance(m.get("payload"), dict) and m["payload"].get(WATCHDOG):
                raise AssertionError(f"timed out after {timeout}s; got channels "
                                     f"{sorted({x['channel'] for x in msgs})}")
            if pred(m):
                return msgs
    finally:
        timer.cancel()


@pytest.fixture
def sim_rt(backend, sim_config):
    """A simulated wearable at 5x speed, looping, so live data keeps flowing."""
    rt = backend.add_runtime("wearable", SimAdapter(speed=5.0, loop=True, duration_s=30.0), sim_config)
    assert wait_until(lambda: rt.connection == "connected" and rt.counters["records"] > 0)
    return rt


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend)) as c:
        yield c


# ---------------------------------------------------------------------------- hub: envelopes & filters

def test_envelope_fields_and_increasing_hub_seq():
    hub = Hub()

    async def main():
        sub = hub.subscribe(frozenset(CHANNELS))
        payloads = [{"i": i} for i in range(5)]
        returned = [hub.publish("derived", "wearable", p) for p in payloads]
        returned.append(hub.publish("station", None, {"event": "x"}))
        assert await _settle(lambda: sub.queue.qsize() == 6)
        return returned, _drain(sub)

    returned, got = asyncio.run(main())
    assert [m["hub_seq"] for m in got] == [m["hub_seq"] for m in returned]
    seqs = [m["hub_seq"] for m in got]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(b - a == 1 for a, b in zip(seqs, seqs[1:]))
    for m, p in zip(got[:5], [{"i": i} for i in range(5)]):
        assert m["v"] == 1
        assert m["channel"] == "derived"
        assert m["runtime"] == "wearable"
        assert m["payload"] == p
        assert "dropped_before" not in m
        assert m["rx_wall"].endswith("Z")
        datetime.fromisoformat(m["rx_wall"].replace("Z", "+00:00"))
    assert got[5]["channel"] == "station" and got[5]["runtime"] is None
    assert hub.published == 6


def test_default_channels_exclude_telemetry():
    assert "telemetry" not in DEFAULT_CHANNELS
    assert set(DEFAULT_CHANNELS) == set(CHANNELS) - {"telemetry"}
    hub = Hub()

    async def main():
        sub = hub.subscribe()  # defaults
        for ch in CHANNELS:
            hub.publish(ch, "wearable", {"ch": ch})
        await _settle(lambda: sub.queue.qsize() >= len(CHANNELS) - 1)
        await asyncio.sleep(0)
        return _drain(sub)

    got = asyncio.run(main())
    assert sorted(m["channel"] for m in got) == sorted(set(CHANNELS) - {"telemetry"})


def test_channel_and_runtime_filters():
    hub = Hub()

    async def main():
        only_session = hub.subscribe(frozenset({"session"}))
        only_a = hub.subscribe(frozenset(CHANNELS), frozenset({"a"}))
        telemetry = hub.subscribe(frozenset({"telemetry"}))
        hub.publish("derived", "a", 1)
        hub.publish("session", "a", 2)
        hub.publish("session", "b", 3)
        hub.publish("derived", "b", 4)
        hub.publish("station", None, 5)  # backend-level: not tied to any runtime
        hub.publish("telemetry", "a", 6)
        await _settle(lambda: only_a.queue.qsize() == 4)
        for _ in range(5):
            await asyncio.sleep(0)
        return _drain(only_session), _drain(only_a), _drain(telemetry)

    s, a, t = asyncio.run(main())
    assert [m["payload"] for m in s] == [2, 3]
    assert [m["payload"] for m in a] == [1, 2, 5, 6]
    assert [m["payload"] for m in t] == [6]


def test_unsubscribed_client_receives_nothing_more():
    hub = Hub()

    async def main():
        sub = hub.subscribe()
        hub.publish("derived", "a", 1)
        await _settle(lambda: sub.queue.qsize() == 1)
        hub.unsubscribe(sub)
        hub.unsubscribe(sub)  # twice is harmless
        hub.publish("derived", "a", 2)
        for _ in range(5):
            await asyncio.sleep(0)
        return _drain(sub)

    assert [m["payload"] for m in asyncio.run(main())] == [1]
    assert hub.subscriber_count == 0


# ---------------------------------------------------------------------------- hub: bounded queues

def test_single_overflow_drops_oldest_and_marks_next_message():
    hub = Hub(client_queue_size=1)

    async def main():
        sub = hub.subscribe()
        hub.publish("derived", "a", "first")
        hub.publish("derived", "a", "second")
        await _settle(lambda: sub.dropped_total == 1)
        for _ in range(5):
            await asyncio.sleep(0)
        return sub, _drain(sub)

    sub, got = asyncio.run(main())
    assert [m["payload"] for m in got] == ["second"]
    assert got[0]["dropped_before"] == 1
    assert sub.dropped_total == 1


def test_queue_never_exceeds_bound_and_keeps_newest():
    hub = Hub(client_queue_size=5)

    async def main():
        sub = hub.subscribe()
        for i in range(50):
            hub.publish("derived", "a", i)
        await _settle(lambda: sub.dropped_total == 45)
        return sub, _drain(sub)

    sub, got = asyncio.run(main())
    assert sub.dropped_total == 45
    assert [m["payload"] for m in got] == list(range(45, 50))


def test_dropped_before_total_is_exact_after_sustained_overflow():
    """A dashboard sums dropped_before to say how much it missed; that sum must be exact."""
    hub = Hub(client_queue_size=3)

    async def main():
        sub = hub.subscribe()
        for i in range(10):
            hub.publish("derived", "a", i)
        await _settle(lambda: sub.dropped_total == 7)
        return sub, _drain(sub)

    sub, got = asyncio.run(main())
    assert sub.dropped_total == 7
    assert [m["payload"] for m in got] == [7, 8, 9]
    reported = sum(m.get("dropped_before", 0) for m in got)
    assert reported == 7, f"client was told it missed {reported} messages, it missed 7: {got}"


def test_dropped_before_marks_the_actual_gap():
    """dropped_before on a message must equal the hub_seq gap right before it."""
    hub = Hub(client_queue_size=2)

    async def main():
        sub = hub.subscribe()
        first = hub.publish("derived", "a", 0)["hub_seq"]
        for i in range(1, 3):
            hub.publish("derived", "a", i)
        await _settle(lambda: sub.dropped_total == 1)
        return first, _drain(sub)

    first, got = asyncio.run(main())
    prev = first - 1
    for m in got:
        assert m.get("dropped_before", 0) == m["hub_seq"] - prev - 1, (
            f"message hub_seq={m['hub_seq']} says dropped_before={m.get('dropped_before', 0)} "
            f"but the gap before it is {m['hub_seq'] - prev - 1}: {got}")
        prev = m["hub_seq"]


def test_publish_from_other_thread_never_blocks_on_a_stalled_loop():
    """The loop is blocked (joining the publisher), so nothing drains: publish must still return."""
    hub = Hub(client_queue_size=4)
    n = 5000

    async def main():
        sub = hub.subscribe(frozenset(CHANNELS))
        errors: list[BaseException] = []

        def publisher():
            try:
                for i in range(n):
                    hub.publish("telemetry", "a", i)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=publisher)
        t0 = time.monotonic()
        t.start()
        t.join(10.0)  # deliberately blocks the event loop
        elapsed = time.monotonic() - t0
        alive = t.is_alive()
        await _settle(lambda: sub.dropped_total + sub.queue.qsize() == n, rounds=50)
        return sub, errors, elapsed, alive, _drain(sub)

    sub, errors, elapsed, alive, got = asyncio.run(main())
    assert not alive and not errors
    assert elapsed < 5.0
    assert hub.published == n
    assert sub.dropped_total == n - 4
    assert [m["payload"] for m in got] == list(range(n - 4, n))


def test_publish_with_zero_subscribers_never_raises():
    hub = Hub()
    msg = hub.publish("derived", "wearable", {"x": 1})
    assert msg["hub_seq"] == 1 and hub.published == 1

    errors: list[BaseException] = []

    def worker():
        try:
            for i in range(100):
                hub.publish("runtime", None, i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    assert not errors and hub.published == 101 and hub.subscriber_count == 0


def test_subscriber_on_closed_loop_is_removed_and_others_still_receive():
    hub = Hub()

    async def make_dead():
        return hub.subscribe()

    dead = asyncio.run(make_dead())  # asyncio.run closes that loop
    assert hub.subscriber_count == 1

    async def main():
        live = hub.subscribe()
        assert hub.subscriber_count == 2
        hub.publish("derived", "a", "after-close")  # must not raise
        assert await _settle(lambda: live.queue.qsize() == 1)
        return live, _drain(live)

    live, got = asyncio.run(main())
    assert [m["payload"] for m in got] == ["after-close"]
    assert hub.subscriber_count == 1  # only the closed-loop one was removed
    hub.unsubscribe(live)
    assert dead.queue.qsize() == 0


class _PreemptedLoop:
    """Delegates to a real loop; the first call_soon_threadsafe waits for ``release``.

    Models a publishing thread being preempted between taking its hub_seq and
    scheduling delivery (an ordinary GIL switch)."""

    def __init__(self, loop, release: threading.Event, entered: threading.Event):
        self._loop, self._release, self._entered = loop, release, entered
        self._first = True
        self._mu = threading.Lock()

    def call_soon_threadsafe(self, *args):
        with self._mu:
            first, self._first = self._first, False
        if first:
            self._entered.set()
            self._release.wait(5)
        return self._loop.call_soon_threadsafe(*args)


def test_hub_seq_delivered_in_order_with_concurrent_publishers():
    hub = Hub()

    async def main():
        sub = hub.subscribe(frozenset(CHANNELS))
        release, entered = threading.Event(), threading.Event()
        sub.loop = _PreemptedLoop(sub.loop, release, entered)
        a = threading.Thread(target=hub.publish, args=("derived", "rt-a", "A"))
        a.start()
        assert entered.wait(5)  # thread A holds hub_seq 1, not yet scheduled
        b = threading.Thread(target=hub.publish, args=("derived", "rt-b", "B"))
        b.start()
        b.join(5)
        release.set()
        a.join(5)
        await _settle(lambda: sub.queue.qsize() == 2)
        return _drain(sub)

    got = asyncio.run(main())
    seqs = [m["hub_seq"] for m in got]
    assert seqs == sorted(seqs), f"client received hub_seq in order {seqs}"


# ---------------------------------------------------------------------------- WebSocket

def test_ws_first_message_is_snapshot_of_runtimes(backend, sim_rt, client):
    with client.websocket_connect("/ws/live") as ws:
        first = ws.receive_json()
        assert first["channel"] == "snapshot"
        assert first["v"] == 1
        runtimes = first["payload"]["runtimes"]
        assert [r["runtime"] for r in runtimes] == ["wearable"]
        snap = runtimes[0]
        assert snap["state"] in ("ready", "calibrating", "recording")
        assert snap["adapter"]["kind"] == "sim"
        assert "latest" in snap and "hello" in snap
        assert snap["provenance"]["source"] == "simulated"
        assert first["payload"]["channels"] == sorted(DEFAULT_CHANNELS)
        # the live stream follows, as envelopes, never telemetry by default
        msgs = read_until(ws, backend.hub, lambda m: m["channel"] == "derived", "derived")
        assert all(m["channel"] != "telemetry" for m in msgs)
        live = msgs[-1]
        assert {"v", "hub_seq", "channel", "runtime", "rx_wall", "payload"} <= set(live)
        assert live["runtime"] == "wearable"


def test_ws_default_channels_carry_no_telemetry(backend, sim_rt, client):
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()  # snapshot
        start = sim_rt.counters["records"]
        assert wait_until(lambda: sim_rt.counters["records"] > start + 40)  # telemetry was published
        backend.start_station_session("simulated", "sentinel")
        msgs = read_until(ws, backend.hub, lambda m: m["channel"] == "station", "station")
    chans = {m["channel"] for m in msgs}
    assert "telemetry" not in chans
    assert "derived" in chans
    seqs = [m["hub_seq"] for m in msgs]
    assert seqs == sorted(seqs)


def test_ws_telemetry_is_opt_in(backend, sim_rt, client):
    with client.websocket_connect("/ws/live?channels=telemetry,station") as ws:
        snap = ws.receive_json()
        assert snap["payload"]["channels"] == ["station", "telemetry"]
        msgs = read_until(ws, backend.hub, lambda m: m["channel"] == "telemetry", "station")
    rec = msgs[-1]["payload"]
    assert rec["type"] in ("emg_batch", "imu_batch")
    assert rec["provenance"]["source"] == "simulated"


def test_ws_channel_filter_delivers_only_requested(backend, sim_rt, client):
    with client.websocket_connect("/ws/live?channels=runtime,session") as ws:
        snap = ws.receive_json()
        assert snap["payload"]["channels"] == ["runtime", "session"]
        before = backend.hub.published
        assert wait_until(lambda: backend.hub.published > before + 50)  # derived/device flowed meanwhile
        s = backend.start_session("wearable", "filter")
        backend.stop_session(s["session_id"])
        msgs = read_until(ws, backend.hub,
                          lambda m: m["channel"] == "session" and m["payload"]["event"] == "session_completed",
                          "session")
    assert {m["channel"] for m in msgs} <= {"runtime", "session"}
    events = [m["payload"]["event"] for m in msgs if m["channel"] == "session"]
    assert events == ["session_started", "session_completed"]
    assert msgs[-1]["payload"]["session"]["source"] == "simulated"
    assert any(m["channel"] == "runtime" and m["payload"].get("state") == "recording" for m in msgs)


def test_ws_unknown_runtime_filter_gets_no_derived(backend, sim_rt, client):
    with client.websocket_connect("/ws/live?runtime=nope") as ws:
        snap = ws.receive_json()
        assert snap["channel"] == "snapshot"
        assert snap["payload"]["runtimes"] == []
        start = sim_rt.counters["records"]
        assert wait_until(lambda: sim_rt.counters["records"] > start + 40)
        backend.start_station_session("simulated", "sentinel")  # runtime=None: reaches every client
        msgs = read_until(ws, backend.hub, lambda m: m["channel"] == "station", "station")
    assert [m["channel"] for m in msgs] == ["station"]
    assert all(m["runtime"] in (None, "nope") for m in msgs)


def test_ws_simulated_disconnect_is_visible_live(backend, sim_rt, client):
    with client.websocket_connect("/ws/live?channels=runtime") as ws:
        ws.receive_json()
        r = client.post("/api/runtimes/wearable/simulate-disconnect", json={"seconds": 0.5})
        assert r.status_code == 200
        msgs = read_until(ws, backend.hub,
                          lambda m: m["payload"].get("event") == "device_connection"
                          and m["payload"]["connection"] == "disconnected", "runtime")
        back = read_until(ws, backend.hub,
                          lambda m: m["payload"].get("event") == "device_connection"
                          and m["payload"]["connection"] == "connected", "runtime")
    states = [m["payload"].get("state") for m in msgs + back if "state" in m["payload"]]
    assert "disconnected" in states
    assert sim_rt.counters["connection_disconnected"] >= 1


def test_ws_close_unsubscribes(backend, client):
    with client.websocket_connect("/ws/live") as ws:
        ws.receive_json()
        assert wait_until(lambda: backend.hub.subscriber_count == 1)
        assert client.get("/api/health").json()["ws_clients"] == 1
    assert wait_until(lambda: backend.hub.subscriber_count == 0)


# ---------------------------------------------------------------------------- acquisition independence

def _run_fast(backend: Backend, name: str, frames: list[bytes], sim_config) -> tuple[float, object]:
    rt = backend.add_runtime(name, ScriptedAdapter(list(frames), clock="device"), sim_config)
    t0 = time.monotonic()
    assert wait_until(lambda: rt.finished, timeout=30)
    return time.monotonic() - t0, rt


def test_stuck_clients_do_not_slow_acquisition(backend, sim_config):
    frames = sim_frames(duration_s=25)
    assert len(frames) > 900

    base_time, base = _run_fast(backend, "base", frames, sim_config)
    assert base.counters["frames"] == len(frames)
    assert base.host_state == "completed" and base.error is None

    # Hub-level clients on an event loop that is completely stalled ...
    stall = threading.Event()
    ready = threading.Event()
    stalled_loop = asyncio.new_event_loop()

    def run_stalled():
        asyncio.set_event_loop(stalled_loop)

        async def subs():
            for _ in range(3):
                backend.hub.subscribe(frozenset(CHANNELS))
            ready.set()
            stall.wait(60)  # blocks the loop: these clients never read anything

        stalled_loop.run_until_complete(subs())

    th = threading.Thread(target=run_stalled, daemon=True)
    th.start()
    assert ready.wait(5)

    # ... plus three real WebSocket clients that subscribe to everything and never read.
    app = create_app(backend)
    try:
        with TestClient(app) as c, \
                c.websocket_connect("/ws/live?channels=" + ",".join(CHANNELS)) as w1, \
                c.websocket_connect("/ws/live?channels=" + ",".join(CHANNELS)) as w2, \
                c.websocket_connect("/ws/live?channels=" + ",".join(CHANNELS)) as w3:
            assert wait_until(lambda: backend.hub.subscriber_count == 6)
            loaded_time, loaded = _run_fast(backend, "loaded", frames, sim_config)
            assert loaded.counters["frames"] == len(frames)
            assert loaded.counters["records"] == base.counters["records"]
            assert loaded.host_state == "completed" and loaded.error is None
            del w1, w2, w3
    finally:
        stall.set()
        th.join(5)
        stalled_loop.close()

    floor = 0.2  # avoid comparing against a near-zero baseline
    assert loaded_time < 3 * max(base_time, floor), (base_time, loaded_time)
