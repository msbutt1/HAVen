"""A faithful emulator of the HAVen ESP32 wearable firmware, behind a pseudo-terminal. SIMULATED bytes only.

Built from the firmware README (Salaar), not from the backend's own simulator. It behaves like the board:

* a host OPEN of the port is a board RESET (DTR/RTS wired to EN/IO0): the ROM banner is printed (garbage, as
  seen at the wrong baud rate, plus text, possibly without a trailing newline so it is glued to the first
  hello), then a NEW boot starts: ``boot_id`` = "b-" + 16 hex, ``seq`` 0 hello, device time and sample
  indices restart;
* one ``seq`` counter per boot shared by all record types, assigned when a record is SUCCESSFULLY encoded
  (an encode error consumes no seq);
* sample ring (1024 EMG / 512 IMU) -> batcher (<= 256 rows, 50 ms target) -> encoder (8192-byte buffer)
  -> 32 KiB frame queue (2 KiB reserved for hello/status; when full the WHOLE frame is dropped, its seq was
  already assigned, so the host sees a seq gap and an UNFLAGGED sample-index jump) -> UART TX at wire_baud;
* ``first_sample_index`` counts acquisition attempts per stream since boot: a delivered sample, a ring-full
  drop, a failed read or a disconnected null row each consume one;
* ring full: the new sample is dropped, its index consumed, and the next batch reports
  ``dropped_samples_before`` with flag ``overrun`` (status counters ``emg_dropped_samples`` ...);
* hello re-sent every ``hello_period_s`` (5 s) and status every ``status_period_s`` (1 s, and at once when a
  sensor state changes), always with the NEXT seq;
* uniform timing when every step is within 25 % of nominal, otherwise explicit timestamps + ``timing_irregular``;
* provenance ``simulated``; hello.hardware = {board, acquisition "FAKE ... (SIMULATED)", config_problems, link}.

Fault knobs: ``drop_frames``, ``stall_serializer`` (ring overrun), ``disconnect_sensor``, ``fail_reads``,
``irregular``, ``fail_encode``, ``reset``, ``set_tx_rate``; ``reset_on_open=False`` + ``midline_on_connect``
give a board that was already running (a connect in the middle of a frame).

Every record handed to the encoder is checked with ``contract.validate_record``; problems land in
``contract_errors`` (the tests assert it stays empty). Ground truth for the assertions: ``emitted`` (every
record with a seq), ``dropped`` (records lost in the TX queue), ``boots``.
"""

from __future__ import annotations

import os
import pty
import random
import termios
import threading
import itertools
import time
import tty
from collections import deque
from pathlib import Path

from haven_signals import contract

_SEEDS = itertools.count(7)   # each emulator instance draws different boot ids (a hardware RNG never repeats)
DEVICE_ID_W = "vg-w-2805a5075554"
RING = {"emg": 1024, "imu": 512}
MAX_ROWS = 256
ENCODE_BUFFER = 8192
FRAME_QUEUE = 32 * 1024
RESERVED = 2 * 1024

ROM_GARBAGE = bytes([0xE0, 0x9C, 0x00, 0xF8, 0x1E, 0x86, 0x80, 0x9E, 0x98, 0x7E, 0x00, 0x66, 0xFE, 0xF0, 0x80])
ROM_TEXT = (b"ets Jun  8 2016 00:22:57\r\n\r\nrst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
            b"configsip: 0, SPIWP:0xee\r\nclk_drv:0x00,q_drv:0x00,d_drv:0x00,cs0_drv:0x00,hd_drv:0x00,wp_drv:0x00\r\n"
            b"mode:DIO, clock div:2\r\nload:0x3fff0030,len:1184\r\nentry 0x40078000")


class _Stream:
    def __init__(self, name: str, rate: float, channels: list[str], units: dict, ring: int):
        self.name, self.rate, self.channels, self.units, self.ring = name, rate, channels, units, ring
        self.reset()

    def reset(self) -> None:
        self.slot = 0                # acquisition attempts since boot (= first_sample_index of the next row)
        self.rows: deque[dict] = deque()
        self.drop_run = 0            # ring drops immediately before the next delivered row
        self.dropped_total = 0
        self.state = "unknown"
        self.disconnected = False
        self.fail_reads = 0
        self.irregular = 0
        self.last_row_index = None


class FirmwareEmulator:
    def __init__(self, link: Path | str | None = None, device_id: str = DEVICE_ID_W, *, emg_hz: float = 1000.0,
                 imu_hz: float = 400.0, batch_ms: float = 50.0, hello_period_s: float = 5.0,
                 status_period_s: float = 1.0, wire_baud: int = 921600, reset_on_open: bool = True,
                 banner: str = "glued", seed: int | None = None, acquisition: str = "FAKE sources: synthetic EMG/IMU (SIMULATED)",
                 config_problems: str | None = "none", board: str = "esp32-wroom-32 (board W)",
                 midline_on_connect: bool = False, hello_streams_known: bool = True):
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        os.set_blocking(self.master, False)
        self.path = os.ttyname(slave)
        os.close(slave)  # only the host holds the slave: a write succeeding means a host has the port open
        self.link = Path(link) if link else None
        if self.link is not None:
            self.link.symlink_to(self.path)
        self.device_id = device_id
        self.hello_period_s, self.status_period_s = hello_period_s, status_period_s
        self.batch_s = batch_ms / 1000.0
        self.wire_baud = wire_baud
        self.tx_rate = wire_baud / 10.0           # bytes per second the UART can move
        self.reset_on_open = reset_on_open
        self.banner = banner                      # "glued" | "glued_single" | "newline" | "none"
        self.midline_on_connect = midline_on_connect
        self.acquisition, self.config_problems, self.board = acquisition, config_problems, board
        self.hello_streams_known = hello_streams_known
        self.boot_delay_s = 0.25                  # reset -> first byte (also lets the host finish configuring the tty)
        self._cut_frame = False
        self._marked = False
        self.boots_pending = False
        self.boots_pending_at = 0.0
        self.emg_sigma = 30.0                     # test knob: the EMG noise amplitude (a 'squeeze' is a larger sigma)
        self._rng = random.Random(next(_SEEDS) if seed is None else seed)
        self.streams = {
            "emg": _Stream("emg", emg_hz, ["emg0"], {"emg0": "adc_counts"}, RING["emg"]),
            "imu": _Stream("imu", imu_hz, ["ax", "ay", "az"],
                           {"ax": "raw_counts", "ay": "raw_counts", "az": "raw_counts"}, RING["imu"]),
        }
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.connected = False
        self.opens = 0
        self.boots: list[str] = []
        self.boot_n = 0
        self.boot_id = ""
        self.emitted: list[dict] = []     # every record that got a seq
        self.dropped: list[dict] = []     # records lost in the TX queue
        self.contract_errors: list[str] = []
        self.counters: dict[str, int] = {}
        self.seq = 0
        self._q: deque[tuple[str, bytes]] = deque()
        self._qbytes = 0
        self._inflight = b""
        self._budget = 0.0
        self._last_tick = time.monotonic()
        self._force_drop = 0
        self._stall_until = 0.0
        self._fail_encode = 0
        self._reset_requested = False
        self._boot_mono = time.monotonic()
        self._next_hello = self._next_status = 0.0
        self._status_dirty = False
        self.bytes_written = 0
        self.running_since_start = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="firmware-emulator")

    # ------------------------------------------------------------------ public

    def start(self) -> "FirmwareEmulator":
        if not self.reset_on_open:
            with self._lock:
                self._boot(banner=False)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(3)
        try:
            os.close(self.master)
        except OSError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.close()

    # fault knobs --------------------------------------------------------------------

    def drop_frames(self, n: int) -> None:
        """The frame queue is 'full' for the next n batch frames: each is dropped whole (seq already assigned)."""
        with self._lock:
            self._force_drop += n

    def stall_serializer(self, seconds: float) -> None:
        """The batcher/encoder stops draining the sample rings: they fill and new samples are dropped."""
        with self._lock:
            self._stall_until = time.monotonic() + seconds

    def disconnect_sensor(self, stream: str, on: bool = True) -> None:
        with self._lock:
            self.streams[stream].disconnected = on

    def fail_reads(self, stream: str, n: int) -> None:
        with self._lock:
            self.streams[stream].fail_reads += n

    def irregular(self, stream: str, n: int = 1) -> None:
        with self._lock:
            self.streams[stream].irregular += n

    def fail_encode(self, n: int = 1) -> None:
        with self._lock:
            self._fail_encode += n

    def reset(self, mid_frame: bool = False) -> None:
        """A reset in the middle of a session (watchdog, brown-out): banner + a new boot. ``mid_frame``: the
        UART was half way through a frame, so its first half reaches the host before the ROM banner."""
        with self._lock:
            self._reset_requested = True
            self._cut_frame = mid_frame

    def set_tx_rate(self, bytes_per_s: float) -> None:
        with self._lock:
            self.tx_rate = bytes_per_s

    def wait_for(self, pred, timeout: float = 10.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if pred(self):
                    return True
            time.sleep(0.01)
        with self._lock:
            return pred(self)

    # ------------------------------------------------------------------ the board

    def _now(self) -> float:
        return time.monotonic() - self._boot_mono

    def _t_us(self) -> int:
        return int(self._now() * 1e6)

    def _boot(self, banner: bool) -> None:
        self.boot_n += 1
        self.boot_id = "b-" + "".join(self._rng.choice("0123456789abcdef") for _ in range(16))
        self.boots.append(self.boot_id)
        self._boot_mono = time.monotonic()
        self.seq = 0
        self.counters = {"emg_dropped_samples": 0, "imu_dropped_samples": 0, "tx_dropped_records": 0,
                         "tx_dropped_bytes": 0, "encode_errors": 0}
        for s in self.streams.values():
            s.reset()
        self._q.clear()
        self._qbytes = 0
        self._inflight = b""
        self._stall_until = 0.0
        self._status_dirty = False
        if banner and self.banner != "none":
            if self.banner == "glued_single":     # garbage with no newline at all, glued to the hello
                raw = ROM_GARBAGE
            else:                                  # "glued": several lines, the last one has no newline
                raw = ROM_GARBAGE + ROM_TEXT + (b"\r\n" if self.banner == "newline" else b"")
            self._q.append(("raw", raw))
        self._send_hello()
        t = self._now()
        self._next_hello = t + self.hello_period_s
        self._next_status = t + self.status_period_s

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                alive = self._probe_host()
                if alive and self.connected and self._marked and not self._marker_present():
                    self.connected = self._marked = False   # a quick close + open between two polls: a new host, a reset
                if alive and not self.connected:
                    self.connected = True
                    self.opens += 1
                    if self.reset_on_open:
                        self.boots_pending_at = now + self.boot_delay_s   # the ROM + bootloader take a moment
                        self.boots_pending = True
                    elif self.midline_on_connect:
                        self._midline = True
                elif not alive and self.connected:
                    self.connected = False
                    self.boots_pending = False
                if self.boots_pending and now >= self.boots_pending_at:
                    self.boots_pending = False
                    self._boot(banner=True)
                    self._set_marker()
                if self._reset_requested and self.boots:
                    self._reset_requested = False
                    if self._cut_frame:
                        self._cut_frame = False
                        frame = self._inflight or next((d for k, d in self._q if k != "raw"), b"")
                        if frame:
                            try:
                                os.write(self.master, frame[: max(1, len(frame) // 2)])
                            except OSError:
                                pass
                        self._inflight = b""
                    self._boot(banner=True)
                if self.boots:
                    self._step()
                    self._drain(now)
            time.sleep(0.002)

    # macOS resets a pty's termios when the slave is opened again: a marker in an unused control character
    # (set well after the host configured the port) shows that the host closed and re-opened between two polls.
    def _set_marker(self) -> None:
        try:
            a = termios.tcgetattr(self.master)
            a[6][termios.VREPRINT] = b"\x7e"
            termios.tcsetattr(self.master, termios.TCSANOW, a)
            self._marked = True
        except (termios.error, OSError):
            self._marked = False

    def _marker_present(self) -> bool:
        try:
            return termios.tcgetattr(self.master)[6][termios.VREPRINT] == b"\x7e"
        except (termios.error, OSError):
            return True

    def _probe_host(self) -> bool:
        try:
            os.write(self.master, b"")
            return True
        except OSError:
            return False

    # -- acquisition -----------------------------------------------------------------

    def _step(self) -> None:
        t = self._now()
        for s in self.streams.values():
            dt = 1.0 / s.rate
            while 0.005 + s.slot * dt <= t:
                self._acquire(s, dt)
            self._batch(s, t)
        if t >= self._next_status or self._status_dirty:
            self._send_status()
            self._next_status = t + self.status_period_s
        if t >= self._next_hello:
            self._send_hello()
            self._next_hello = t + self.hello_period_s

    def _value(self, s: _Stream, slot: int) -> list[int]:
        r = self._rng
        if s.name == "emg":
            return [max(0, min(4095, int(2048 + r.gauss(0, self.emg_sigma))))]
        return [int(r.gauss(0, 20)), int(r.gauss(0, 20)), int(4096 + r.gauss(0, 20))]

    def _set_state(self, s: _Stream, state: str) -> None:
        if s.state != state:
            s.state = state
            self._status_dirty = True

    def _acquire(self, s: _Stream, dt: float) -> None:
        slot = s.slot
        s.slot += 1
        ts = 0.005 + slot * dt
        ts_us = int(ts * 1e6) + self._rng.randint(-int(0.03 * dt * 1e6), int(0.03 * dt * 1e6))
        if s.irregular and slot > 0 and slot % 7 == 3:  # one sample 40 % late (beyond the 25 % limit)
            s.irregular -= 1
            ts_us += int(0.4 * dt * 1e6)
        if s.disconnected:
            row = {"index": slot, "t": ts_us, "values": [None] * len(s.channels), "flags": {"sensor_disconnected", "missing_values"}}
            self._set_state(s, "disconnected")
        elif s.fail_reads:
            s.fail_reads -= 1
            row = {"index": slot, "t": ts_us, "values": [None] * len(s.channels), "flags": {"missing_values"}}
        else:
            row = {"index": slot, "t": ts_us, "values": self._value(s, slot), "flags": set()}
            self._set_state(s, "ok")
        if len(s.rows) >= s.ring:  # ring full: the NEW sample is dropped, its index consumed
            s.drop_run += 1
            s.dropped_total += 1
            self.counters[f"{s.name}_dropped_samples"] += 1
            return
        row["dropped_before"] = s.drop_run
        s.drop_run = 0
        s.rows.append(row)

    # -- batcher + encoder ---------------------------------------------------------

    def _batch(self, s: _Stream, t: float) -> None:
        if time.monotonic() < self._stall_until:
            return
        while s.rows and (len(s.rows) >= MAX_ROWS or t - s.rows[0]["t"] / 1e6 >= self.batch_s):
            first = s.rows[0]
            rows = [s.rows.popleft()]
            while (s.rows and len(rows) < MAX_ROWS and s.rows[0]["dropped_before"] == 0
                   and s.rows[0]["index"] == rows[-1]["index"] + 1
                   and ("sensor_disconnected" in s.rows[0]["flags"]) == ("sensor_disconnected" in first["flags"])):
                rows.append(s.rows.popleft())
            self._encode_batch(s, rows)

    def _encode_batch(self, s: _Stream, rows: list[dict]) -> None:
        dt_us = 1e6 / s.rate
        ts = [r["t"] for r in rows]
        for i in range(1, len(ts)):  # strictly increasing
            if ts[i] <= ts[i - 1]:
                ts[i] = ts[i - 1] + 1
        steps = [b - a for a, b in zip(ts, ts[1:])]
        flags: list[str] = []
        irregular = any(abs(st - dt_us) > 0.25 * dt_us for st in steps)
        if irregular:
            timing = {"mode": "explicit", "t_us": ts}
            flags.append("timing_irregular")
        else:
            interval = (ts[-1] - ts[0]) / (len(ts) - 1) if len(ts) > 1 else dt_us
            timing = {"mode": "uniform", "t0_us": ts[0], "interval_us": round(interval, 3)}
        allflags = set().union(*(r["flags"] for r in rows))
        flags += sorted(allflags)
        quality: dict = {"flags": flags}
        dropped = rows[0]["dropped_before"]
        if dropped:
            quality["dropped_samples_before"] = dropped
            quality["flags"].append("overrun")
        rec = {"type": f"{s.name}_batch", "sample_rate_hz": s.rate, "first_sample_index": rows[0]["index"],
               "timing": timing, "channels": s.channels, "units": s.units,
               "samples": [r["values"] for r in rows], "quality": quality}
        self._encode(rec, "batch")

    def _send_hello(self) -> None:
        known = self.hello_streams_known
        rec = {"type": "hello", "t_us": self._t_us(), "firmware": {"name": "vibeguard-wearable", "version": "0.1.0"},
               "streams": {
                   "emg": {"nominal_rate_hz": self.streams["emg"].rate, "channels": ["emg0"],
                           "units": {"emg0": "adc_counts"}, "raw_limits": {"min": 0, "max": 4095} if known else None,
                           "sensor_model": "ESP32 ADC1" if known else None},
                   "imu": {"nominal_rate_hz": self.streams["imu"].rate, "channels": ["ax", "ay", "az"],
                           "units": {"ax": "raw_counts", "ay": "raw_counts", "az": "raw_counts"},
                           "raw_limits": {"min": -32768, "max": 32767} if known else None,
                           "sensor_model": "synthetic" if known else None}},
               "hardware": {"board": self.board, "acquisition": self.acquisition,
                            "config_problems": self.config_problems, "link": f"uart0 {self.wire_baud} 8N1"}}
        self._encode(rec, "hello")

    def _send_status(self) -> None:
        self._status_dirty = False
        rec = {"type": "status", "t_us": self._t_us(), "state": "streaming",
               "sensors": {"emg": self.streams["emg"].state, "imu": self.streams["imu"].state},
               "counters": dict(self.counters)}
        self._encode(rec, "status")

    def _encode(self, body: dict, kind: str) -> None:
        if kind == "batch" and self._fail_encode:
            self._fail_encode -= 1
            self.counters["encode_errors"] += 1   # no seq consumed
            return
        rec = {"schema": contract.SCHEMA, "version": 1, "type": body["type"], "device_id": self.device_id,
               "boot_id": self.boot_id, "seq": self.seq, "provenance": {"source": "simulated"}}
        rec.update({k: v for k, v in body.items() if k != "type"})
        issues = contract.validate_record(rec)
        if issues:
            self.contract_errors.append(f"{rec['type']} seq {rec['seq']}: {issues[0]}")
        line = contract.serialize_record(rec).encode()
        if len(line) > ENCODE_BUFFER:
            self.counters["encode_errors"] += 1
            return
        self.seq += 1
        self.emitted.append(rec)
        limit = FRAME_QUEUE if kind in ("hello", "status") else FRAME_QUEUE - RESERVED
        forced = kind == "batch" and self._force_drop > 0
        if forced or self._qbytes + len(line) > limit:
            if forced:
                self._force_drop -= 1
            self.counters["tx_dropped_records"] += 1
            self.counters["tx_dropped_bytes"] += len(line)
            self.dropped.append(rec)
            return
        self._q.append((kind, line))
        self._qbytes += len(line)

    # -- UART TX --------------------------------------------------------------------

    def _drain(self, now: float) -> None:
        dt = now - self._last_tick
        self._last_tick = now
        self._budget = min(self._budget + dt * self.tx_rate, 4096.0)
        while self._budget >= 1 or (self.tx_rate > 1e9):
            if not self._inflight:
                if not self._q:
                    return
                kind, data = self._q.popleft()
                if kind != "raw":
                    self._qbytes -= len(data)
                if getattr(self, "_midline", False) and kind != "raw":
                    self._midline = False
                    data = data[len(data) // 2:]  # the board was already mid-frame when the host connected
                self._inflight = data
            n = min(len(self._inflight), int(self._budget))
            if n <= 0:
                return
            try:
                w = os.write(self.master, self._inflight[:n])
            except BlockingIOError:
                return
            except OSError:
                self._inflight = b""  # no host reading: UART bytes go nowhere
                self._budget = 0.0
                self.connected = False   # the host closed the port; the next open is a reset again
                return
            self.bytes_written += w
            self._inflight = self._inflight[w:]
            self._budget -= w
