"""Read device telemetry from a file or a pipe, exactly as it was written (no relabelling).

For the firmware's ``vg-fake-device``, which prints the byte stream the board will send:

    build/host/vg-fake-device --seconds 30 > fake.ndjson
    uv run vg-backend serve --device stream --stream fake.ndjson
    build/host/vg-fake-device --seconds 30 | uv run vg-backend serve --device stream --stream -

The records keep the provenance the producer gave them (``simulated`` for the fake device). Unlike a
replay of a recorded session, nothing is relabelled. By default time is the device's own clock (the stream
is read as fast as it arrives and the source ends with an end-of-stream); ``paced`` plays it back at the
speed the device timestamps imply, so a dashboard sees it live.
"""

from __future__ import annotations

import os
import select
import sys
import time
from pathlib import Path
from typing import BinaryIO

from vibeguard_signals import contract
from vibeguard_signals.replay import device_time_us

from .adapters import AdapterDisconnected, DeviceAdapter, EndOfStream


class StreamAdapter(DeviceAdapter):
    kind = "stream"

    def __init__(self, source: str = "-", paced: bool = False, speed: float = 1.0):
        if not speed > 0:
            raise ValueError("speed must be above 0")
        self.source = source
        self.paced = paced
        self.speed = speed
        self.clock = "host" if paced else "device"
        self._fh: BinaryIO | None = None
        self._buf = bytearray()
        self._eof = False
        self._t0: float | None = None
        self._boot: str | None = None
        self._boot_t0 = 0.0
        self._boot_last = 0.0
        self._offset = 0.0
        self._next: bytes | None = None

    def open(self) -> None:
        if self._fh is not None:
            return
        if self.source == "-":
            self._fh = sys.stdin.buffer
        else:
            p = Path(self.source)
            if not p.is_file():
                raise AdapterDisconnected(f"stream file not found: {p}")
            self._fh = p.open("rb")
        self._t0 = time.monotonic()

    def _next_line(self, timeout: float) -> bytes | None:
        """One newline-terminated line from the source, or None if nothing arrived within ``timeout``."""
        assert self._fh is not None
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl + 1])
                del self._buf[:nl + 1]
                return line
            if self._eof:
                if self._buf:
                    line, self._buf = bytes(self._buf), bytearray()
                    return line
                raise EndOfStream
            try:
                fd = self._fh.fileno()
                if self.source == "-":  # a pipe: wait for data, but never block past the timeout
                    ready, _, _ = select.select([fd], [], [], timeout)
                    if not ready:
                        return None
                chunk = os.read(fd, 65536)
            except (OSError, ValueError) as exc:
                raise AdapterDisconnected(f"cannot read {self.source}: {exc}") from exc
            if not chunk:
                self._eof = True
                continue
            self._buf.extend(chunk)

    def read_line(self, timeout: float) -> bytes | None:
        if self._fh is None:
            raise AdapterDisconnected("stream not open")
        if self._next is None:
            self._next = self._next_line(timeout)
            if self._next is None:
                return None
        line = self._next
        if self.paced:
            due = self._due_s(line)
            if due is not None:
                wait = self._t0 + due / self.speed - time.monotonic()  # type: ignore[operator]
                if wait > 0:
                    time.sleep(min(wait, timeout))
                    if wait > timeout:
                        return None
        self._next = None
        return line

    def _due_s(self, line: bytes) -> float | None:
        """Playback time (seconds since the first record) for a line, from its device timestamp."""
        rec, issues = contract.parse_line(line)
        if rec is None or issues:
            return None
        t = device_time_us(rec)
        if t is None:
            return None
        if rec["boot_id"] != self._boot:  # device time restarts at every boot: append the new one after the old
            if self._boot is not None:
                self._offset += (self._boot_last - self._boot_t0) / 1e6
            self._boot, self._boot_t0 = rec["boot_id"], t
        self._boot_last = t
        return self._offset + (t - self._boot_t0) / 1e6

    def close(self) -> None:
        if self._fh is not None and self._fh is not sys.stdin.buffer:
            self._fh.close()
        self._fh = None

    def describe(self) -> dict:
        return {**super().describe(), "source": self.source, "paced": self.paced, "speed": self.speed,
                "note": "records keep the provenance the producer gave them"}
