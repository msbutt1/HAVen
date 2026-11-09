"""Soak / resource regression tests for a long-running backend (fast: whole file < 60 s).

These are miniature versions of the multi-minute soak runs done against a real
``vg-backend serve`` process. They check that repeating the things a dashboard does
all day (sessions started and stopped, replays started and removed, clients that
never read) does not leak threads or file descriptors, does not make acquisition
stall, and does not grow without bound.

Tests marked ``xfail(strict=True)`` reproduce real defects found by the soak run; each
reason carries the measured numbers. When the defect is fixed the strict xfail turns
into a failure, which is the cue to remove the marker.

Deterministic and offline: simulated data only, no network.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import resource
from pathlib import Path

import pytest

from conftest import ScriptedAdapter, SimAdapter, sim_frames, wait_until
from haven_backend.hub import Hub
from haven_backend.runtime import RuntimeGone
from haven_backend.service import Backend, processing_config_for
from haven_backend.store import Store

# registered informally: `pytest -m fast` selects these
pytestmark = pytest.mark.fast


def open_fds() -> int:
    """Open file descriptors of this process (the listing's own fd is a constant offset)."""
    return len(os.listdir("/dev/fd"))


def sim_runtime(b: Backend, name: str = "wearable", **kw):
    kw = {"duration_s": 5, "speed": 30, **kw}
    rt = b.add_runtime(name, SimAdapter(**kw), processing_config_for("sim"))
    assert wait_until(lambda: rt.host_state == "ready", 10), rt.status()
    return rt


# ---------------------------------------------------------------- threads / fds / directories

def test_session_and_replay_cycles_leak_no_threads_fds_or_dirs(backend: Backend):
    """Start/stop sessions and start/remove replays repeatedly (including removing a
    replay mid-stream): thread count, open fds and runtime table return to baseline."""
    rt = sim_runtime(backend)

    def cycle() -> str:
        s = backend.start_session("wearable", "soak")
        assert wait_until(lambda: rt.session is not None and rt.session.rec.lines > 30, 10)
        sid = backend.stop_session(s["session_id"])["session_id"]
        fast = backend.start_replay(sid, fast=True)["runtime"]
        assert wait_until(lambda: backend.runtimes[fast].finished, 30)
        backend.remove_runtime(fast)
        slow = backend.start_replay(sid, speed=1.0)["runtime"]  # paced: still streaming when removed
        assert wait_until(lambda: backend.runtimes[slow].counters["frames"] > 5, 10)
        backend.remove_runtime(slow)
        return sid

    import gc

    cycle()  # warm-up: lazily created pools, sqlite journal files, imports
    gc.collect()  # sockets/loops left by earlier tests are closed lazily and must not count as ours
    threads0, fds0 = threading.active_count(), open_fds()
    for _ in range(8):
        cycle()
    gc.collect()
    assert threading.active_count() <= threads0, (threads0, threading.active_count())
    assert open_fds() <= fds0 + 2, (fds0, open_fds())
    assert list(backend.runtimes) == ["wearable"]
    dirs = [p for p in (backend.data_dir / "sessions").iterdir()]
    assert len(dirs) == 9  # exactly one directory per session, no strays from replays


def test_runtime_crash_closes_recorder_files_and_thread(backend: Backend):
    """An unexpected adapter failure while recording: files are closed, the session row is
    final, the thread is gone and commands answer 409 instead of hanging."""
    class Boom(ScriptedAdapter):
        armed = threading.Event()

        def read_line(self, timeout):
            if self.armed.is_set():
                raise RuntimeError("boom")
            return super().read_line(timeout)

    fds0 = open_fds()
    rt = backend.add_runtime("wearable", Boom(sim_frames(duration_s=10), hold_open=True),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["frames"] > 20, 10)
    s = backend.start_session("wearable", "crash")
    assert wait_until(lambda: rt.session.rec.lines > 5, 10)
    Boom.armed.set()
    assert wait_until(lambda: not rt.alive, 10)
    row = backend.session(s["session_id"])
    assert row["state"] == "interrupted" and row["end_reason"] == "runtime_error"
    assert rt.host_state == "error"
    assert open_fds() <= fds0, (fds0, open_fds())  # recorder files closed with the thread
    with pytest.raises(RuntimeGone):
        rt.start_session("again")


# ---------------------------------------------------------------- slow / never-reading clients

class _Loop:
    """An event loop on its own thread, standing in for the server's loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def test_never_reading_subscriber_stays_bounded_and_does_not_stall_acquisition(tmp_path: Path):
    """A WebSocket client that connects and never reads keeps a bounded queue, loses the
    oldest messages, and never slows the device thread."""
    hub = Hub(client_queue_size=300)
    b = Backend(tmp_path / "data", hub=hub)
    lp = _Loop()
    try:
        async def sub():
            return hub.subscribe(frozenset({"derived", "telemetry", "runtime", "device", "session"}))
        stuck = lp.run(sub())
        rt = sim_runtime(b, speed=20)
        t0, f0 = time.monotonic(), rt.counters["frames"]
        time.sleep(3.0)
        frames = rt.counters["frames"] - f0
        rate = frames / (time.monotonic() - t0)
        assert rate > 300, f"acquisition slowed to {rate:.0f} frames/s with a stuck client"
        assert hub.published > 5 * 300
        assert stuck.queue.qsize() <= 300
        assert stuck.dropped_total > 0
        assert rt.counters["frame_errors"] == 0
        lp.run(asyncio.sleep(0.05))
        hub.unsubscribe(stuck)
        assert hub.subscriber_count == 0
    finally:
        lp.close()
        b.shutdown()


def test_replay_burst_does_not_evict_live_messages_from_a_shared_client_queue():
    live_total, burst = 100, 10_000
    hub = Hub(client_queue_size=2000)
    lp = _Loop()
    try:
        async def sub():
            return hub.subscribe()
        s = lp.run(sub())
        for i in range(burst):
            hub.publish("derived", "replay-x", {"i": i})
            if i % (burst // live_total) == 0:
                hub.publish("derived", "wearable", {"live": i})
        lp.run(asyncio.sleep(0.2))
        got = []
        while not s.queue.empty():
            got.append(s.queue.get_nowait())
        live = [m for m in got if m["runtime"] == "wearable"]
        assert len(live) == live_total, f"{live_total - len(live)} of {live_total} live messages evicted"
    finally:
        lp.close()


# ---------------------------------------------------------------- acquisition under heavy HTTP use

def _seed_rows(store: Store, n: int) -> None:
    """Many realistic history rows, so list queries have real work to do."""
    import json
    score = json.dumps({"label": "x" * 80, "explanation": "y" * 200, "excluded_by_reason": {"k": 1.0}})
    files = json.dumps({f: {"path": "/x/" + f, "bytes": 1, "sha256": "0" * 64}
                        for f in ("telemetry.ndjson", "rx_times.csv", "derived.ndjson", "commands.ndjson")})
    rows = [(f"ws_{i:06d}", "wearable", "dev1", "completed", "l", "simulated", 1,
             f"2026-01-{1 + i % 28:02d}T00:00:{i % 60:02d}.{i % 1000:03d}Z", "[]", score,
             json.dumps({"frames": 5000}), files, "/nonexistent") for i in range(n)]
    with store._lock:
        store._db.executemany(
            "INSERT INTO wearable_sessions (session_id,runtime,device_id,state,label,source,simulated_history,"
            "started_at,boot_ids_json,score_json,counters_json,files_json,dir) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)


def test_acquisition_gap_stays_small_under_parallel_history_and_file_reads(backend: Backend, tmp_path: Path):
    """Parallel GET /api/history, /api/sessions and file downloads (service calls from 6 threads)
    must not make the acquisition thread stall. Real-server numbers (20000 history rows, 8 clients,
    2827 requests, 5.9 GB served in 25 s): max gap between frames 90.8 ms loaded vs 81.8 ms idle."""
    _seed_rows(backend.store, 6000)
    rt = sim_runtime(backend, speed=20)
    stamps: list[float] = []
    orig = rt._on_line

    def on_line(line, now):
        stamps.append(now)
        return orig(line, now)
    rt._on_line = on_line  # instance attribute: the acquisition loop looks it up per frame

    s = backend.start_session("wearable", "gap")
    assert wait_until(lambda: rt.session.rec.lines > 200, 10)
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(1 << 20) * 16)
    stop = threading.Event()
    served = [0]

    def hammer(i: int) -> None:
        while not stop.is_set():
            if i % 3 == 0:
                served[0] += len(backend.history(limit=1000)["items"])
            elif i % 3 == 1:
                served[0] += len(backend.store.wearable_sessions(limit=1000))
            else:
                served[0] += len(big.read_bytes())

    ths = [threading.Thread(target=hammer, args=(i,), daemon=True) for i in range(6)]
    mark = len(stamps)
    for t in ths:
        t.start()
    time.sleep(3.0)
    stop.set()
    for t in ths:
        t.join(10)
    loaded = stamps[mark:]
    backend.stop_session(s["session_id"])
    gaps = sorted(b - a for a, b in zip(loaded, loaded[1:]))
    assert len(loaded) > 300 and served[0] > 0
    assert gaps[-1] < 1.0, f"acquisition stalled {gaps[-1] * 1e3:.0f} ms under HTTP load"
    assert gaps[int(len(gaps) * 0.99)] < 0.25


def test_stop_session_does_not_hold_acquisition_long_for_a_big_session(backend: Backend):
    """Stopping hashes every session file on the acquisition thread. Hashing ~64 MB must
    stay well under a second (a serial device's OS buffer is small)."""
    rt = sim_runtime(backend, speed=20)
    s = backend.start_session("wearable", "big")
    assert wait_until(lambda: rt.session.rec.lines > 50, 10)
    pad = rt.session.rec.dir / "telemetry.ndjson"
    with pad.open("ab") as fh:  # grow the recorded file outside the recorder's own handle
        fh.write(b"\n".join([b"{}"] * 2000) + b"\n")
        fh.write((b"x" * 1023 + b"\n") * (64 * 1024))
    t = time.monotonic()
    backend.stop_session(s["session_id"])
    assert time.monotonic() - t < 2.0


# ---------------------------------------------------------------- unbounded growth

def test_peak_memory_does_not_grow_with_more_frames_processed(backend: Backend):
    """Processing three times more frames (many simulated reboots) must not raise the peak
    RSS: no per-frame or per-boot accumulation in processor buffers, latest values, counters."""
    rt = backend.add_runtime("wearable", SimAdapter(duration_s=5, speed=1000, fast=True, loop=True),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["frames"] > 4000, 30)
    scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes on macOS, KiB on Linux
    rss = lambda: resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale / 1e6  # noqa: E731
    f0 = rt.counters["frames"]
    r0 = rss()
    assert wait_until(lambda: rt.counters["frames"] > 4 * f0, 60), rt.counters["frames"]
    assert rss() - r0 < 25, f"peak RSS grew {rss() - r0:.1f} MB over {rt.counters['frames'] - f0} more frames"
    assert len(rt.events) < 20 and len(rt.counters) < 30
    assert rt._cmds.qsize() == 0


def test_sqlite_wal_stays_bounded_under_sustained_writes(tmp_path: Path):
    """Real-server run: WAL plateaued at about 4 MB over 160 sessions and 645 idempotency rows."""
    store = Store(tmp_path / "v.sqlite3")
    try:
        payload = {"blob": "z" * 4000}
        for i in range(3000):
            store.idem_put(f"req-{i}", "start_session:wearable", payload)  # ~12 MB of rows
        wal = tmp_path / "v.sqlite3-wal"
        assert (wal.stat().st_size if wal.exists() else 0) < 8 * 1024 * 1024
    finally:
        store.close()


def test_key_locks_do_not_grow_with_every_request_id(backend: Backend):
    for i in range(1500):
        backend.start_station_session(label="k", request_id=f"rid-{i}")
    assert len(backend._key_locks) < 100, len(backend._key_locks)


# ---------------------------------------------------------------- shutdown

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sigterm_with_a_stuck_websocket_client_still_shuts_down(tmp_path: Path):
    import base64
    port = _free_port()
    data = tmp_path / "data"
    proc = subprocess.Popen(
        [sys.executable, "-m", "haven_backend.cli", "--data-dir", str(data), "serve", "--http-port",
         str(port), "--speed", "20", "--log-level", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stuck = None
    try:
        import httpx
        base = f"http://127.0.0.1:{port}"
        assert wait_until(lambda: _up(httpx, base), 30)
        stuck = socket.socket()
        stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        stuck.connect(("127.0.0.1", port))
        key = base64.b64encode(os.urandom(16)).decode()
        stuck.sendall((f"GET /ws/live?channels=derived,telemetry HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                       f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        assert wait_until(lambda: httpx.get(base + "/api/health").json()["ws_clients"] == 1, 10)
        sid = httpx.post(base + "/api/sessions", json={}, timeout=10).json()["session_id"]
        time.sleep(1.0)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            pytest.fail("server did not exit within 10 s of SIGTERM with a stuck WebSocket client")
        db = sqlite3.connect(data / "vibeguard.sqlite3")
        assert db.execute("select state from wearable_sessions where session_id=?", (sid,)).fetchone()[0] \
            == "interrupted"
    finally:
        if stuck is not None:
            stuck.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


def _up(httpx, base: str) -> bool:
    try:
        return httpx.get(base + "/api/health", timeout=1).status_code == 200
    except Exception:  # noqa: BLE001 - server not listening yet
        return False
