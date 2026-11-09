"""USB serial transport for the controller link (pyserial).

Implements ``LineTransport``: non-blocking ``read_lines`` and ``write_line``. Works with
any pyserial URL, including ``loop://`` for tests. NOT yet exercised against the real
controller. ``SerialException`` is an ``OSError``, which the link adapter turns into an
``AdapterError`` (and the engine into a visible ``error`` state).
"""

from __future__ import annotations

import time

import serial
from serial.tools import list_ports

MAX_PARTIAL_BYTES = 65_536  # a line longer than this is garbage; drop it rather than grow forever


class SerialTransport:
    def __init__(self, port: str, baud: int = 115200, *, open_wait_s: float = 0.0):
        # timeout=0: reads never block. write_timeout: a wedged port must not hang the engine.
        self._ser = serial.serial_for_url(port, baudrate=baud, timeout=0, write_timeout=1.0)
        self.port = port
        self.baud = baud
        self._buf = bytearray()
        self.overflows = 0
        self._discarding = False  # after an overflow: drop everything up to the next newline
        try:
            if open_wait_s:
                time.sleep(open_wait_s)  # many ESP32 boards reset when the port opens
            self._ser.reset_input_buffer()
        except BaseException:
            self._ser.close()  # never leave the port open if setup fails
            raise

    @property
    def is_simulated(self) -> bool:
        """Only a plain device name (COM5, /dev/ttyUSB0) is real hardware. A pyserial URL such as
        ``loop://`` or ``spy://`` is a software port and is labelled simulated."""
        return "://" in self.port

    def write_line(self, line: str) -> None:
        data = line.encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        self._ser.write(data)  # honours write_timeout; flush() is omitted (it can spin on Windows)

    def read_lines(self) -> list[str]:
        waiting = self._ser.in_waiting
        if waiting:
            self._buf += self._ser.read(waiting)
        lines = []
        while True:
            i = self._buf.find(b"\n")
            if i < 0:
                break
            raw = bytes(self._buf[:i]).rstrip(b"\r")
            del self._buf[:i + 1]
            if self._discarding:  # the tail of a runaway line: resynchronise on its newline
                self._discarding = False
                continue
            if raw:
                lines.append(raw.decode("utf-8", errors="replace"))
        if len(self._buf) > MAX_PARTIAL_BYTES:
            self._buf.clear()
            self._discarding = True
            self.overflows += 1
        elif self._discarding and self._buf:
            self._buf.clear()  # still inside the runaway line
        return lines

    def close(self) -> None:
        self._ser.close()

    def __enter__(self) -> "SerialTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def list_serial_ports() -> list[tuple[str, str]]:
    """(device, description) for every serial port the OS reports."""
    return [(p.device, p.description) for p in sorted(list_ports.comports())]
