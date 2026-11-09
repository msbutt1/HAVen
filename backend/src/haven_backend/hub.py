"""Live delivery hub: fan-out from acquisition threads to WebSocket clients.

Acquisition never waits for a client. Each subscriber has a bounded queue; when
a slow client falls behind, its oldest messages are dropped and the next message
it receives carries ``dropped_before`` so the UI can say it missed data.

Every message is an envelope:

    {"v": 1, "hub_seq": 812, "channel": "derived", "runtime": "wearable",
     "rx_wall": "2026-11-07T18:02:11.532Z", "payload": {...}}

Channels:
- ``derived``   processor outputs (stream_features / stream_event / experimental_score)
- ``device``    device ``hello`` and ``status`` records as received
- ``telemetry`` raw ``emg_batch`` / ``imu_batch`` records (high volume; opt-in)
- ``runtime``   backend-level state: connection, host state, counters
- ``session``   wearable session started / stopped / interrupted
- ``station``   station session and trial records
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Any

#: runtimes started by POST /api/sessions/{id}/replay are named replay-<id>; a client that did not
#: ask for a runtime by name does not receive them (a fast replay would otherwise flood its queue
#: and push out the live device's messages)
REPLAY_PREFIX = "replay-"
CHANNELS = ("derived", "device", "telemetry", "runtime", "session", "station")
DEFAULT_CHANNELS = frozenset(c for c in CHANNELS if c != "telemetry")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Subscription:
    def __init__(self, loop: asyncio.AbstractEventLoop, channels: frozenset[str],
                 runtimes: frozenset[str] | None, maxsize: int, include_replays: bool = True):
        self.loop = loop
        self.channels = channels
        self.runtimes = runtimes
        self.include_replays = include_replays
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.dropped_total = 0

    def wants(self, msg: dict) -> bool:
        rt = msg["runtime"]
        if not self.include_replays and rt is not None and rt.startswith(REPLAY_PREFIX):
            return False  # replay streams go only to clients that ask for them by name
        return msg["channel"] in self.channels and (
            self.runtimes is None or rt is None or rt in self.runtimes)

    def _put(self, msg: dict) -> None:  # runs in the event loop thread
        """Enqueue; when full, drop the oldest and tell the client where the gap is.

        The count of dropped messages rides on the message right AFTER the gap (the
        new head of the queue), and accumulates if that head is itself dropped later,
        so the sum of ``dropped_before`` a client sees equals what it missed.
        """
        carry = self.dropped
        self.dropped = 0
        if self.queue.full():
            try:
                evicted = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                evicted = None
            if evicted is not None:
                self.dropped_total += 1
                carry += 1 + evicted.get("dropped_before", 0)
        if carry:
            pending = self.queue._queue  # type: ignore[attr-defined]  # the deque behind the queue
            if pending:
                head = pending[0]
                pending[0] = {**head, "dropped_before": head.get("dropped_before", 0) + carry}
            else:
                msg = {**msg, "dropped_before": msg.get("dropped_before", 0) + carry}
        self.queue.put_nowait(msg)


class Hub:
    def __init__(self, client_queue_size: int = 2000):
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._seq = 0
        self.client_queue_size = client_queue_size
        self.published = 0

    def publish(self, channel: str, runtime: str | None, payload: Any) -> dict:
        """Thread-safe; never blocks on clients.

        Delivery is scheduled while the lock is held, so concurrent publishers cannot
        reorder ``hub_seq`` (``call_soon_threadsafe`` does not block).
        """
        dead: list[Subscription] = []
        with self._lock:
            self._seq += 1
            msg = {"v": 1, "hub_seq": self._seq, "channel": channel, "runtime": runtime,
                   "rx_wall": utc_now_iso(), "payload": payload}
            self.published += 1
            for sub in self._subs:
                if sub.wants(msg):
                    try:
                        sub.loop.call_soon_threadsafe(sub._put, msg)
                    except RuntimeError:  # loop closed: the client is gone
                        dead.append(sub)
            for sub in dead:
                self._subs.remove(sub)
        return msg

    @property
    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self, channels: frozenset[str] = DEFAULT_CHANNELS,
                  runtimes: frozenset[str] | None = None, include_replays: bool = False) -> Subscription:
        sub = Subscription(asyncio.get_running_loop(), channels, runtimes, self.client_queue_size,
                           include_replays)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
