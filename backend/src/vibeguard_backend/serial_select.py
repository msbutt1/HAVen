"""Pick the wearable among several serial ports by the ``device_id`` in its telemetry.

Both wearable boards use the same CP2102 USB-UART bridge and report the SAME USB serial number, so the OS
cannot tell them apart. The firmware's ``device_id`` (``vg-w-<base MAC>``) is in every record, so the right
port is the one whose stream carries it. This adapter probes each candidate port (the glob, in natural
order), keeps the one that matches, and hands over the lines it already read so nothing is lost.

DTR/RTS should be off while probing an ESP32 dev board (``dtr=False, rts=False``): opening a port with them
asserted can reset the board through the EN/IO0 auto-reset circuit.
"""

from __future__ import annotations

import glob
import re
import threading
import time
from collections import deque

from vibeguard_signals import contract

from .adapters import AdapterDisconnected, DeviceAdapter, SerialAdapter


def _natural(name: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


_DEVICE_ID = re.compile(rb'"device_id"\s*:\s*"([^"]{1,64})"')


def _parse(line: bytes):
    """contract.parse_line that never raises: a hostile line (deep nesting) is just invalid."""
    try:
        return contract.parse_line(line)
    except (RecursionError, ValueError, TypeError):
        return None, [contract.Issue("malformed_json", "", "undecodable")]


class DeviceIdSerialAdapter(DeviceAdapter):
    """A serial adapter that only accepts the port whose stream says ``device_id == expect_device_id``."""

    kind = "serial"
    clock = "host"

    def __init__(self, port_pattern: str, baud: int, expect_device_id: str, dtr: bool | None = None,
                 rts: bool | None = None, probe_s: float = 7.0):
        if not expect_device_id or not isinstance(expect_device_id, str):
            raise ValueError("expect_device_id must be a non-empty string")
        if not probe_s > 0:
            raise ValueError("probe_s must be above 0")
        self.port_pattern = port_pattern
        self.baud = baud
        self.expect_device_id = expect_device_id
        # Probing opens every matching port, including the OTHER board's: with DTR/RTS asserted that would reset
        # it (auto-reset circuit). They default to off; pass True to override (native USB waiting for DTR).
        self.dtr = False if dtr is None else dtr
        self.rts = False if rts is None else rts
        self._stop = threading.Event()
        self.probe_s = probe_s  # a hello is re-sent every 5 s and status every 1 s, so a live board answers fast
        self.port: str | None = None
        self.seen: dict[str, str | None] = {}   # port -> device_id found there (None: nothing valid in time)
        self.candidates: list[str] = []
        self._inner: SerialAdapter | None = None
        self._pending: deque[bytes] = deque()
        self._last_port: str | None = None

    partial_discarded_last = 0
    hello_recovered_last = 0

    @property
    def hello_recovered(self) -> int:
        inner = self._inner
        return inner.hello_recovered if inner else self.hello_recovered_last

    @property
    def partial_discarded(self) -> int:
        inner = self._inner
        return inner.partial_discarded if inner else self.partial_discarded_last

    def _candidates(self) -> list[str]:
        if any(c in self.port_pattern for c in "*?["):
            found = sorted(glob.glob(self.port_pattern), key=_natural)
        else:
            found = [self.port_pattern]
        if self._last_port in found:  # the port that matched last time is tried first: a quick reconnect
            found.remove(self._last_port)
            found.insert(0, self._last_port)
        return found

    def interrupt(self) -> None:
        self._stop.set()

    def open(self) -> None:
        self._stop.clear()
        self.candidates = self._candidates()
        self.seen = {p: d for p, d in self.seen.items() if p in self.candidates}  # forget ports that vanished
        if not self.candidates:
            raise AdapterDisconnected(f"no port matches {self.port_pattern}")
        errors = []
        for cand in self.candidates:
            if self._stop.is_set():
                raise AdapterDisconnected("open interrupted")
            ad = SerialAdapter(cand, self.baud, dtr=self.dtr, rts=self.rts)
            try:
                ad.open()
            except AdapterDisconnected as exc:
                errors.append(str(exc))
                self.seen[cand] = None
                continue
            adopted = False
            try:
                lines: list[bytes] = []
                found: str | None = None
                end = time.monotonic() + self.probe_s
                while time.monotonic() < end and found is None and not self._stop.is_set():
                    try:
                        line = ad.read_line(0.2)
                    except AdapterDisconnected as exc:
                        errors.append(f"{cand}: {exc}")
                        break
                    if line is None:
                        continue
                    lines.append(line)
                    rec, issues = _parse(line)
                    if rec is not None and not issues:
                        found = rec["device_id"]
                self.seen[cand] = found
                if found == self.expect_device_id:
                    self._inner, self.port, self._last_port = ad, ad.port, cand
                    self._pending = deque(lines)  # what was read while identifying must still reach the pipeline
                    adopted = True
                    return
            finally:  # any exception, an interrupt or a mismatch: never leave a probe port open
                if not adopted:
                    ad.close()
        if self._stop.is_set():
            raise AdapterDisconnected("open interrupted")
        seen = {p: d for p, d in self.seen.items() if p in self.candidates}
        detail = ", ".join(f"{p}: {d or 'no valid data'}" for p, d in seen.items())
        raise AdapterDisconnected(f"no port carries device {self.expect_device_id} ({detail}"
                                  + (f"; errors: {'; '.join(errors)}" if errors else "") + ")")

    def read_line(self, timeout: float) -> bytes | None:
        inner = self._inner
        if self._pending:
            line = self._pending.popleft()
        elif inner is None:
            raise AdapterDisconnected("port not open")
        else:
            line = inner.read_line(timeout)
        if line is not None:
            m = _DEVICE_ID.search(line[:300])
            if m and m.group(1).decode("ascii", "replace") != self.expect_device_id:
                # the board on this port was replaced (or the names swapped): re-identify instead of adopting it
                found = m.group(1).decode("ascii", "replace")
                self.close()
                raise AdapterDisconnected(f"the board on this port is now {found}, expected {self.expect_device_id}")
        return line

    def close(self) -> None:
        inner, self._inner = self._inner, None
        if inner is not None:
            self.partial_discarded_last = inner.partial_discarded
            self.hello_recovered_last = inner.hello_recovered
            inner.close()
        self._pending.clear()
        self.port = None

    def describe(self) -> dict:
        inner = self._inner  # one read: open()/close() run on another thread
        d = {**super().describe(), "port": self.port, "port_pattern": self.port_pattern, "baud": self.baud,
             "expect_device_id": self.expect_device_id, "ports_seen": dict(self.seen),
             "port_candidates": list(self.candidates), "dtr": self.dtr, "rts": self.rts}
        if inner is not None:
            d.update({"partial_discarded": inner.partial_discarded, "oversize_resyncs": inner.oversize_resyncs,
                      "hello_recovered": inner.hello_recovered})
        return d
