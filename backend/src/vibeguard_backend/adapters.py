"""Device adapters: every source of device telemetry exposes the same interface.

An adapter delivers raw NDJSON frames (bytes, one contract-v1 record each) to a
DeviceRuntime. It never decodes, repairs or reorders them: validation and
processing happen downstream, identically for every adapter.

    adapter.open()               -> raises AdapterDisconnected if the device is not there
    adapter.read_line(timeout)   -> bytes, or None when nothing arrived within timeout
                                    raises AdapterDisconnected (link lost) or
                                    EndOfStream (finite source exhausted)
    adapter.close()

Adapters:
- SimAdapter:    in-process simulated wearable (``vibeguard_signals.simulator``).
- SerialAdapter: real firmware over USB serial, with reconnect after unplug.
- ReplayAdapter: a recorded session, re-emitted with replay provenance.
"""

from __future__ import annotations

import glob
import os
import re
import itertools
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Iterator

from vibeguard_signals import contract
from vibeguard_signals.replay import replay_lines
from vibeguard_signals.simulator import MAX_RUN_ID_LEN, SimConfig, Simulation

MAX_LINE_BYTES = contract.MAX_LINE_BYTES


class AdapterDisconnected(Exception):
    """The link to the device is gone (unplugged, port vanished, read failed)."""


class EndOfStream(Exception):
    """A finite source (replay, finite simulation) has no more frames."""


class DeviceAdapter(ABC):
    #: "sim" | "serial" | "replay" | ...
    kind: str = "abstract"
    #: "host": frames arrive in (paced) real time, staleness judged by host clock.
    #: "device": frames arrive as fast as possible, staleness judged by device time.
    clock: str = "host"

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def read_line(self, timeout: float) -> bytes | None: ...

    def close(self) -> None:
        pass

    def interrupt(self) -> None:
        """Ask a blocking open()/read_line() to return soon (the runtime is stopping). Default: nothing to do."""

    def describe(self) -> dict:
        return {"kind": self.kind, "clock": self.clock}


class _PacedFrames(DeviceAdapter):
    """Shared pacing for adapters that emit (due_s, frame) pairs from an iterator."""

    def __init__(self, speed: float = 1.0, fast: bool = False):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.speed = speed
        self.fast = fast
        self.clock = "device" if fast else "host"
        self._it: Iterator[tuple[float, bytes]] | None = None
        self._pending: tuple[float, bytes] | None = None
        self._t0: float | None = None

    @abstractmethod
    def _frames(self) -> Iterator[tuple[float, bytes]]:
        """Yield (source_time_s, frame) in order; source time drives pacing."""

    def open(self) -> None:
        if self._it is None:
            self._it = self._frames()
            self._t0 = time.monotonic()

    def _next(self) -> tuple[float, bytes]:
        if self._pending is None:
            assert self._it is not None
            try:
                self._pending = next(self._it)
            except StopIteration:
                raise EndOfStream from None
        return self._pending

    def read_line(self, timeout: float) -> bytes | None:
        if self._it is None:
            raise AdapterDisconnected("adapter not open")
        due_s, frame = self._next()
        if not self.fast:
            wait = self._t0 + due_s / self.speed - time.monotonic()  # type: ignore[operator]
            if wait > 0:
                time.sleep(min(wait, timeout))
                if wait > timeout:
                    return None
        self._pending = None
        return frame


class SimAdapter(_PacedFrames):
    """Simulated wearable. ``loop=True`` restarts the run when it ends, with a new
    boot identity, the way a real device would after a power cycle.

    ``inject_disconnect(seconds)`` imitates pulling the USB cable: frames generated
    during the window are lost and ``read_line``/``open`` raise AdapterDisconnected.
    """

    kind = "sim"

    def __init__(self, scenario: str = "demo", seed: int = 1, duration_s: float = 30.0,
                 speed: float = 1.0, fast: bool = False, loop: bool = True,
                 device_id: str | None = None, run_id: str | None = None):
        super().__init__(speed, fast)
        kwargs = {"scenario": scenario, "seed": seed, "duration_s": duration_s}
        if device_id:
            kwargs["device_id"] = device_id
        self.config = SimConfig(**kwargs)
        self.config.validate()
        if run_id is not None and loop and len(run_id) + len("-r9999") > MAX_RUN_ID_LEN:
            raise ValueError(f"run_id must be at most {MAX_RUN_ID_LEN - 6} characters when looping")
        self.loop = loop
        self.run_id = run_id
        self.recording_ids: list[str] = []
        self._unplugged_until = 0.0
        self.frames_lost_unplugged = 0

    def _frames(self) -> Iterator[tuple[float, bytes]]:
        offset = 0.0
        for n in itertools.count():
            run_id = self.run_id if (self.run_id and n == 0) else None
            if self.run_id and n > 0:
                run_id = f"{self.run_id}-r{n}"
            sim = Simulation(self.config, run_id=run_id)
            self.recording_ids.append(sim.recording_id)
            last = 0.0
            for r in sim.records():
                last = r.sim_time_us / 1e6
                if r.lost:  # lost in simulated transport: the host never sees it
                    continue
                yield offset + last, contract.serialize_record(r.record).encode()
            if not self.loop:
                return
            offset += last + 0.3  # the device is silent briefly while it "reboots"

    def inject_disconnect(self, seconds: float) -> None:
        """Pretend the cable is pulled for ``seconds`` of wall time.

        Frames that come due during the window are lost, as on a real link (in
        fast mode, frames covering ``seconds × speed`` of simulated time are lost).
        """
        self._unplugged_until = time.monotonic() + seconds
        self._unplug_sim_s = seconds * self.speed

    def _unplugged(self) -> bool:
        return time.monotonic() < self._unplugged_until

    def _drop_unplugged_frames(self) -> None:
        if self._it is None or not self._unplugged_until:
            return
        try:
            if self.fast:
                start, _ = self._next()
                while self._next()[0] < start + self._unplug_sim_s:
                    self._pending = None
                    self.frames_lost_unplugged += 1
            else:
                until = self._unplugged_until
                while self._t0 + self._next()[0] / self.speed < until:  # type: ignore[operator]
                    self._pending = None
                    self.frames_lost_unplugged += 1
        except EndOfStream:
            pass
        self._unplugged_until = 0.0

    def open(self) -> None:
        if self._unplugged():
            raise AdapterDisconnected("simulated cable unplugged")
        super().open()
        self._drop_unplugged_frames()

    def read_line(self, timeout: float) -> bytes | None:
        if self._unplugged():
            raise AdapterDisconnected("simulated cable unplugged")
        return super().read_line(timeout)

    def describe(self) -> dict:
        return {**super().describe(), "scenario": self.config.scenario, "seed": self.config.seed,
                "duration_s": self.config.duration_s, "speed": self.speed, "fast": self.fast,
                "loop": self.loop, "recording_ids": self.recording_ids[-5:],
                "frames_lost_unplugged": self.frames_lost_unplugged,
                "note": "SIMULATED device; never presented as live"}


class ReplayAdapter(_PacedFrames):
    """Re-emits a recorded telemetry file with ``provenance.source = "replay"``.

    device_id, boot_id, seq and device timestamps are unchanged; ``speed`` only
    changes wall-clock pacing. Invalid frames in the recording are skipped and
    counted in ``skipped``.
    """

    kind = "replay"

    def __init__(self, path: str | Path, recording_id: str | None = None,
                 speed: float = 1.0, fast: bool = False):
        super().__init__(speed, fast)
        # Pacing only decides when frames are delivered. Staleness is always judged in
        # recorded device time, so a replay reproduces the recording at any speed.
        self.clock = "device"
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.recording_id = recording_id
        self.skipped: list = []

    def _frames(self) -> Iterator[tuple[float, bytes]]:
        with self.path.open("rb") as fh:
            for clock_s, rec in replay_lines(fh, self.recording_id, self.skipped):
                yield clock_s, contract.serialize_record(rec).encode()

    def describe(self) -> dict:
        return {**super().describe(), "path": str(self.path), "recording_id": self.recording_id,
                "speed": self.speed, "fast": self.fast, "skipped_frames": len(self.skipped)}


_KEEP_INPUT_CLASS = None


def _keep_input_serial_class():
    """A pyserial Serial that does not discard pending input while opening (real pyserial only; a test
    double installed as ``serial.Serial`` is left alone)."""
    global _KEEP_INPUT_CLASS
    import serial

    base = serial.Serial
    if not str(getattr(base, "__module__", "")).startswith("serial"):
        return None
    if _KEEP_INPUT_CLASS is None or _KEEP_INPUT_CLASS.__base__ is not base:
        class _KeepInputSerial(base):  # type: ignore[misc, valid-type]
            _opening = False

            def reset_input_buffer(self):
                if self._opening:
                    return  # keep whatever the firmware already sent
                return super().reset_input_buffer()
        _KEEP_INPUT_CLASS = _KeepInputSerial
    return _KEEP_INPUT_CLASS


class SerialAdapter(DeviceAdapter):
    """Real firmware over USB serial (NDJSON framing).

    ``port`` may be a glob such as ``/dev/cu.usbmodem*`` so the device is found
    again if it re-enumerates under a new name after an unplug. An over-long
    line is passed on truncated (the validator rejects it as ``line_too_long``)
    and the reader resynchronises at the next newline.
    """

    kind = "serial"
    clock = "host"

    def __init__(self, port: str, baud: int = 921600, dtr: bool | None = None, rts: bool | None = None):
        """``dtr`` / ``rts``: leave None for the driver default (asserted at open). Auto-reset boards
        (Arduino-style, ESP32 dev kits) restart their firmware whenever DTR is asserted, i.e. on every
        open; pass False to avoid that. Native-USB boards that wait for the host (``while(!Serial)``)
        need DTR, so the default stays on."""
        self.port_pattern = port
        self.baud = baud
        self.dtr, self.rts = dtr, rts
        self.candidates: list[str] = []  # every port the pattern matched at the last open
        self.port: str | None = None
        self._ser = None
        self._buf = bytearray()
        self._ready: deque[bytes] = deque()
        self._discarding = False
        self.oversize_resyncs = 0  # visible in describe(): newline-free runs longer than one frame
        self.partial_discarded = 0  # first lines after (re)connect that could not be a frame
        self.hello_recovered = 0    # a hello found glued behind boot text on that first line
        self._check_first = False
        self._first_discard = False
        self._banner_phase = False  # until the first frame-like line: boot text may carry a glued hello

    @staticmethod
    def _natural(name: str):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]

    def _resolve(self) -> str:
        if any(c in self.port_pattern for c in "*?["):
            matches = sorted(glob.glob(self.port_pattern), key=self._natural)  # ttyACM2 before ttyACM10
            self.candidates = matches
            if not matches:
                raise AdapterDisconnected(f"no port matches {self.port_pattern}")
            return matches[0]
        self.candidates = [self.port_pattern]
        return self.port_pattern

    def open(self) -> None:
        import serial  # pyserial; imported lazily so tests need no serial stack

        port = self._resolve()
        try:
            kw: dict = {"timeout": 0}
            if os.name == "posix":
                kw["exclusive"] = True  # a second program (serial monitor, another vg-backend) is refused, not interleaved
            keep = _keep_input_serial_class()
            if keep is not None:
                # pyserial flushes the input buffer inside open(). A firmware that sends its hello the instant
                # DTR rises (native USB, `while(!Serial)`) would lose it, so the flush is skipped on open.
                ser = keep()
                ser.port, ser.baudrate = port, self.baud
                ser.timeout = 0
                if "exclusive" in kw:
                    ser.exclusive = True
                if self.dtr is not None:
                    ser.dtr = self.dtr
                if self.rts is not None:
                    ser.rts = self.rts
                ser._opening = True
                try:
                    ser.open()
                finally:
                    ser._opening = False
            elif self.dtr is None and self.rts is None:
                ser = serial.Serial(port, self.baud, **kw)
            else:  # set the modem lines BEFORE opening, so an auto-reset board is not reset by the open
                ser = serial.Serial(**kw)
                ser.port, ser.baudrate = port, self.baud
                if self.dtr is not None:
                    ser.dtr = self.dtr
                if self.rts is not None:
                    ser.rts = self.rts
                ser.open()
            self._ser = ser
        except (serial.SerialException, OSError, ValueError) as exc:
            raise AdapterDisconnected(f"cannot open {port}: {exc}") from exc
        self.port = port
        self._buf.clear()
        self._ready.clear()
        # Connecting mid-stream, the first line may be the tail of a frame. If it does not start with
        # '{' it cannot be a frame and is dropped (and counted); if it does (for example the
        # firmware's hello sent on connect) it is kept, and a damaged one is rejected by the
        # validator as a visible invalid_record.
        self._discarding = False
        self._check_first = True
        self._first_discard = False
        self._banner_phase = True

    def _split(self) -> None:
        if self._check_first and self._buf:
            self._check_first = False
            if self._buf[:1] != b"{":
                self._discarding = True
                self._first_discard = True  # boot text may end without a newline, glued to the hello
                self.partial_discarded += 1
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                if len(self._buf) > MAX_LINE_BYTES and not self._discarding:
                    self._ready.append(bytes(self._buf[:MAX_LINE_BYTES + 1]))
                    self._discarding = True
                    self.oversize_resyncs += 1
                    self._buf.clear()
                elif self._discarding and not (self._first_discard and len(self._buf) <= MAX_LINE_BYTES):
                    self._buf.clear()
                    self._first_discard = False
                return
            line = bytes(self._buf[:nl + 1])
            del self._buf[:nl + 1]
            if self._discarding:
                self._discarding = False
                if self._first_discard:
                    self._first_discard = False
                    k = line.rfind(b'{"schema"')  # a hello right behind unterminated boot text is kept
                    if k >= 0:
                        self.hello_recovered += 1
                        self._ready.append(line[k:])
                continue
            if line[:1] != b"{":
                k = line.rfind(b'{"schema"')  # a hello glued behind boot text (a ROM banner at any reset)
                if k > 0 and re.search(rb'"type"\s*:\s*"hello"', line[k:k + 400]):
                    self.hello_recovered += 1
                    line = line[k:]
            # an over-long line is passed on truncated either way; the validator rejects it
            self._ready.append(line if len(line) <= MAX_LINE_BYTES else line[:MAX_LINE_BYTES + 1])

    def read_line(self, timeout: float) -> bytes | None:
        import serial

        if self._ser is None:
            raise AdapterDisconnected("port not open")
        deadline = time.monotonic() + timeout
        while not self._ready:
            try:
                waiting = self._ser.in_waiting
                chunk = self._ser.read(waiting or 1)
            except (serial.SerialException, OSError, TypeError) as exc:
                self.close()
                raise AdapterDisconnected(f"read failed: {exc}") from exc
            if chunk:
                self._buf.extend(chunk)
                self._split()
                # a newline-free stream (wrong baud rate, garbage) must not hold the caller forever
                if not self._ready and time.monotonic() >= deadline:
                    return None
            elif time.monotonic() >= deadline:
                return None
            else:
                time.sleep(0.002)
        return self._ready.popleft()

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001 - closing a vanished port may fail
                pass
        self._ser = None

    def describe(self) -> dict:
        return {**super().describe(), "port": self.port, "port_pattern": self.port_pattern,
                "baud": self.baud, "oversize_resyncs": self.oversize_resyncs,
                "partial_discarded": self.partial_discarded, "hello_recovered": self.hello_recovered,
                "port_candidates": self.candidates, "dtr": self.dtr, "rts": self.rts}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
