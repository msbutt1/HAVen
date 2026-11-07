"""Review: what REAL boards do that the simulated firmware does not, against the serial path.

The serial path (SerialAdapter, serial_check, find_baud, the runtime's reconnect loop) is driven by the
real pyserial over pseudo-terminals with CRAFTED byte streams: boot banners, debug prints, CRLF, hello
re-sends, NUL padding, a board that resets on every open, two boards on one glob, a host-first handshake.
Everything is SIMULATED bytes. Each test either verifies something works (plain test) or reproduces a
defect (xfail strict: when the defect is fixed the test flips to XPASS and strict makes it fail loudly,
so the marker must be removed).

macOS note: a pty only accepts some baud rates (115200, 230400).
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import pty
import threading
import time
import tty
from pathlib import Path

import pytest

from conftest import sim_frames, wait_until
from vibeguard_signals import contract
from vibeguard_backend.adapters import SerialAdapter
from vibeguard_backend.hardware import find_baud, serial_check
from vibeguard_backend.service import Backend, processing_config_for

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs pseudo-terminals")

BAUD = 115200
SECS = 1.7          # serial_check window
DT = 0.005          # seconds between frames: ~245 frames take ~1.3 s


# ------------------------------------------------------------------------------------ helpers

def _live(frames: list[bytes]) -> list[bytes]:
    out = []
    for f in frames:
        rec = json.loads(f)
        rec["provenance"] = {"source": "live"}
        out.append(contract.serialize_record(rec).encode().rstrip(b"\n"))
    return out


FRAMES = _live(sim_frames(scenario="happy_path", duration_s=6))   # no trailing newline; [0] is the hello
HELLO = FRAMES[0]


def nl(frame: bytes) -> bytes:
    return frame + b"\n"


def paced(items: list[bytes], dt: float = DT) -> list:
    out: list = []
    for x in items:
        out += [x, dt]
    return out


class Board:
    """A pty 'board'. ``script(open_number)`` returns what the firmware does after each open of the port:
    bytes to send, a number (seconds to sleep) or ("wait_rx", seconds) to wait for the host to send a byte.
    A new open restarts the script and cancels the previous one, the way a DTR auto-reset does."""

    def __init__(self, script):
        import fcntl
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        # never block in write(): closing a pty master while another thread is stuck writing to it hangs the
        # process in the kernel (uninterruptible), so the firmware thread polls instead
        fcntl.fcntl(self.master, fcntl.F_SETFL, fcntl.fcntl(self.master, fcntl.F_GETFL) | os.O_NONBLOCK)
        self.path = os.ttyname(self.slave)
        self.script = script
        self.opens = 0
        self.host_bytes = b""
        self._gen = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def play(self) -> None:
        with self._lock:
            self.opens += 1
            self._gen += 1
            gen, items = self._gen, self.script(self.opens)
        t = threading.Thread(target=self._run, args=(gen, items), daemon=True)
        self._threads.append(t)
        t.start()

    def _alive(self, gen: int) -> bool:
        return not self._stop.is_set() and gen == self._gen

    def _run(self, gen: int, items: list) -> None:
        for it in items:
            if not self._alive(gen):
                return
            if isinstance(it, (int, float)):
                time.sleep(it)
            elif isinstance(it, tuple) and it[0] == "wait_rx":
                end = time.monotonic() + it[1]
                while self._alive(gen) and time.monotonic() < end and not self._drain_host():
                    time.sleep(0.01)
            else:
                view = memoryview(it)
                while view and self._alive(gen):
                    try:
                        n = os.write(self.master, view)
                    except BlockingIOError:
                        time.sleep(0.002)
                        continue
                    except OSError:
                        return
                    view = view[n:]

    def _drain_host(self) -> bool:
        """Anything the host wrote to the port (read from the master side, non-blocking)."""
        try:
            data = os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            return bool(self.host_bytes)
        self.host_bytes += data
        return bool(self.host_bytes)

    def host_wrote(self) -> bytes:
        self._drain_host()
        return self.host_bytes

    def close(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(2)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


class BoardAdapter(SerialAdapter):
    """The real SerialAdapter; the board 'boots' (plays its script) right after every successful open."""

    def __init__(self, board: Board, port: str | None = None, baud: int = BAUD):
        super().__init__(port or board.path, baud)
        self.board = board

    def open(self) -> None:
        super().open()
        self.board.play()


@pytest.fixture
def boards():
    made: list[Board] = []

    def make(script) -> Board:
        b = Board(script)
        made.append(b)
        return b

    yield make
    for b in made:
        b.close()


@pytest.fixture
def be(tmp_path: Path):
    b = Backend(tmp_path / "data")
    yield b
    b.shutdown()


def check_with(board: Board, secs: float = SECS, port: str | None = None):
    return serial_check(port or board.path, BAUD, secs,
                        adapter_factory=lambda p, bd: BoardAdapter(board, p, bd))


def named(report, name):
    found = [c for c in report.checks if c.name == name]
    return found[0] if found else None


def text(report) -> str:
    return report.render()


def collect(ad: SerialAdapter, secs: float) -> list[bytes]:
    lines, end = [], time.monotonic() + secs
    while time.monotonic() < end:
        line = ad.read_line(0.1)
        if line is not None:
            lines.append(line)
    return lines


def is_hello(line: bytes) -> bool:
    rec, issues = contract.parse_line(line)
    return rec is not None and not issues and rec["type"] == "hello"


def run_runtime(be: Backend, board: Board, until, timeout: float = 8.0, session: bool = False):
    ad = BoardAdapter(board)
    rt = be.add_runtime("ser", ad, processing_config_for("serial"))
    assert wait_until(lambda: rt.connection == "connected", 5)
    if session:
        rt.start_session("review")
    assert wait_until(lambda: until(rt), timeout), dict(rt.counters)
    return rt, ad


def baseline_losses() -> int:
    """seq gaps already present in the simulated stream itself (the simulator injects some loss)."""
    last, missing = None, 0
    for f in FRAMES:
        s = json.loads(f)["seq"]
        if last is not None and s > last + 1:
            missing += s - last - 1
        last = s
    return missing


# ------------------------------------------------------------------------------------ baseline

def test_baseline_clean_stream_passes_so_the_cases_below_are_comparable(boards):
    b = boards(lambda i: paced([nl(f) for f in FRAMES]))
    res = check_with(b)
    assert named(res.report, "contract frames").level == "PASS", text(res.report)
    assert res.stats["invalid"] == {}
    assert res.stats["seq_duplicates"] == 0 and res.stats["seq_missing"] == baseline_losses()


# ------------------------------------------------------------------------------------ (1) boot banner

BANNER = (b"ets Jun  8 2016 00:22:57\r\n\r\nrst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
          b"configsip: 0, SPIWP:0xee\r\nBooting...\r\n")


def test_banner_before_hello_is_survivable_in_serial_check(boards):
    b = boards(lambda i: [BANNER, nl(HELLO)] + paced([nl(f) for f in FRAMES[1:]]))
    res = check_with(b)
    rep = res.report
    assert named(rep, "contract frames").level in ("PASS", "WARN"), text(rep)
    assert named(rep, "hello received") is None, text(rep)            # the hello was found after the banner
    assert named(rep, "hardware facts known").level == "PASS"
    assert named(rep, "emg sample rate").level == "PASS"
    assert sum(res.stats["invalid"].values()) <= 4                     # banner lines, counted not hidden


def test_banner_before_hello_runtime_keeps_hello_and_counts_the_banner_visibly(be, boards):
    b = boards(lambda i: [BANNER, nl(HELLO)] + paced([nl(f) for f in FRAMES[1:]]))
    rt, ad = run_runtime(be, b, lambda r: r.counters["records"] >= len(FRAMES) - 1)
    assert rt.hello is not None
    assert rt.counters["frame_errors"] == 0
    assert 1 <= rt.counters["invalid_frames"] <= 4                     # banner lines, not silently swallowed
    assert ad.partial_discarded == 1                                   # the first banner line (it is not a frame)


def test_banner_warn_hint_quotes_the_first_rejected_banner_line(boards):
    banner = b"\r\n" + BANNER
    b = boards(lambda i: [banner, nl(HELLO)] + paced([nl(f) for f in FRAMES[1:]]))
    rep = check_with(b).report
    assert named(rep, "contract frames").level == "WARN", text(rep)
    assert "ets Jun" in text(rep), text(rep)


def test_hello_glued_behind_unterminated_boot_garbage_is_not_lost(boards):
    # ESP32 ROM output at 74880 baud is garbage at 115200 and may end without a newline; the app's hello follows.
    b = boards(lambda i: [b"\xe0\x80\xfe\x1b\x9c\x00" + nl(HELLO)] + paced([nl(f) for f in FRAMES[1:]]))
    ad = BoardAdapter(b)
    ad.open()
    try:
        lines = collect(ad, 1.0)
    finally:
        ad.close()
    assert any(is_hello(line) for line in lines), f"{len(lines)} lines, none is the hello; partial_discarded={ad.partial_discarded}"


def test_a_hello_behind_garbage_on_the_first_line_is_recovered_not_blamed_on_the_firmware(boards):
    b = boards(lambda i: [b"\xe0\x80\xfe\x1b\x9c\x00" + nl(HELLO)] + paced([nl(f) for f in FRAMES[1:]]))
    rep = check_with(b, secs=3.0).report
    assert named(rep, "hello received") is None          # no 'firmware sent no hello' complaint
    assert named(rep, "hardware facts known") is not None  # the hello was recovered and read


# ------------------------------------------------------------------------------------ (2) debug prints

def test_occasional_debug_line_is_a_warn_that_quotes_the_line(boards):
    items = [nl(HELLO)]
    for i, f in enumerate(FRAMES[1:]):
        items.append(nl(f))
        if i % 30 == 0:
            items.append(b"DBG loop ok\r\n")
    b = boards(lambda i: paced(items))
    rep = check_with(b).report
    c = named(rep, "contract frames")
    assert c.level == "WARN", text(rep)
    assert "DBG" in text(rep)


def test_heavy_debug_output_shows_the_offending_line(boards):
    items = [nl(HELLO)]
    for f in FRAMES[1:]:
        items += [nl(f), b"DBG loop ok\r\n"]
    b = boards(lambda i: paced(items, 0.004))
    rep = check_with(b).report
    assert named(rep, "contract frames").level == "FAIL"
    assert "DBG" in text(rep), text(rep)


def test_debug_text_interleaved_does_not_disturb_valid_records(be, boards):
    items = [nl(HELLO)]
    for f in FRAMES[1:60]:
        items += [nl(f), b"DBG loop ok\r\n"]
    b = boards(lambda i: paced(items, 0.002))
    rt, _ = run_runtime(be, b, lambda r: r.counters["frames"] >= 119)
    assert rt.counters["records"] == 60 and rt.counters["invalid_frames"] == 59
    assert rt.counters["frame_errors"] == 0 and rt.hello is not None


# ------------------------------------------------------------------------------------ (3) CRLF, trailing space

def test_crlf_and_trailing_space_frames_are_valid(boards):
    for name, ending in (("crlf", b"\r\n"), ("space", b" \n"), ("space-crlf", b" \r\n")):
        b = boards(lambda i, e=ending: paced([f + e for f in FRAMES]))
        res = check_with(b, secs=3.5)  # 247 frames at 5 ms spacing need ~1.5 s; leave margin for the analysis
        assert res.stats["invalid"] == {}, (name, res.stats["invalid"])
        assert res.stats["valid"] == len(FRAMES) and res.stats["seq_duplicates"] == 0, name


# ------------------------------------------------------------------------------------ (4) mid-line at connect

def test_frame_split_mid_line_at_connect_is_one_counted_discard(be, boards):
    tail = nl(FRAMES[3])[200:]                       # the board was mid-frame when we opened the port
    b = boards(lambda i: [tail] + paced([nl(f) for f in FRAMES[4:60]]))
    rt, ad = run_runtime(be, b, lambda r: r.counters["records"] >= 56)
    assert ad.partial_discarded == 1
    assert rt.counters["invalid_frames"] == 0 and rt.counters["frame_errors"] == 0


def test_serial_check_after_a_mid_line_connect_says_no_hello_correctly(boards):
    tail = nl(FRAMES[0])[100:]                       # the tail of a hello
    b = boards(lambda i: [tail] + paced([nl(f) for f in FRAMES[1:]]))
    rep = check_with(b).report
    assert named(rep, "hello received").level == "WARN"
    assert named(rep, "contract frames").level in ("PASS", "WARN")


# ------------------------------------------------------------------------------------ (5) hello re-sent

def _with_resent_hello(at: int) -> list[bytes]:
    """The hello is emitted again at position ``at`` with the next seq; every later record shifts by one
    (the contract: seq increments by exactly one per record emitted)."""
    recs = [json.loads(f) for f in FRAMES]
    out = []
    for i, r in enumerate(recs):
        r = copy.deepcopy(r)
        if i == at:
            h = copy.deepcopy(recs[0])
            h["seq"] = r["seq"]
            out.append(h)
        if i >= at:
            r["seq"] += 1
        out.append(r)
    for r in out:
        assert contract.validate_record(r) == [], contract.validate_record(r)[:2]
    return [contract.serialize_record(r).encode().rstrip(b"\n") for r in out]


def test_hello_resent_with_the_next_seq_is_not_a_gap_a_duplicate_or_a_reboot(boards):
    lines = _with_resent_hello(100)
    b = boards(lambda i: paced([nl(x) for x in lines]))
    res = check_with(b)
    assert res.stats["seq_duplicates"] == 0 and res.stats["reboots"] == 0
    assert res.stats["seq_missing"] == baseline_losses()
    assert named(res.report, "contract frames").level == "PASS"


def test_hello_resent_verbatim_is_reported_as_a_duplicate_not_a_reboot(boards):
    lines = [nl(f) for f in FRAMES[:100]] + [nl(HELLO)] + [nl(f) for f in FRAMES[100:]]
    b = boards(lambda i: paced(lines))
    res = check_with(b)
    assert res.stats["seq_duplicates"] == 1 and res.stats["reboots"] == 0
    assert named(res.report, "duplicate records").level == "WARN"


def test_runtime_survives_a_hello_resend_with_the_next_seq(be, boards):
    lines = _with_resent_hello(40)[:100]
    b = boards(lambda i: paced([nl(x) for x in lines], 0.002))
    rt, _ = run_runtime(be, b, lambda r: r.counters["frames"] >= len(lines))
    assert rt.counters["frame_errors"] == 0 and rt.counters["invalid_frames"] == 0
    assert rt.boot_id == json.loads(HELLO)["boot_id"]
    assert rt.events.get("boot_change", 0) <= 1


# ------------------------------------------------------------------------------------ (6) reset on open

class _RecordingSerial:
    """Stands in for serial.Serial: records how the adapter opens the port."""
    calls: list = []

    def __init__(self, *args, **kw):
        type(self).calls.append((args, kw, time.monotonic()))
        self.in_waiting = 0

    def read(self, n=1):
        return b""

    def close(self):
        pass


def test_open_uses_pyserial_defaults_which_assert_dtr_and_rts_at_open(monkeypatch):
    """Documents the behaviour: serial.Serial(port, baud, timeout=0) opens immediately and pyserial drives DTR and
    RTS high, which auto-resets Arduino-style boards (and ESP32 dev boards) on EVERY open."""
    import serial
    _RecordingSerial.calls = []
    monkeypatch.setattr(serial, "Serial", _RecordingSerial)
    ad = SerialAdapter("/dev/ttyFAKE0", BAUD)
    ad.open()
    args, kw, _ = _RecordingSerial.calls[0]
    assert args == ("/dev/ttyFAKE0", BAUD) and "dtr" not in kw and "rts" not in kw


def test_adapter_can_be_told_not_to_toggle_dtr():
    params = " ".join(inspect.signature(SerialAdapter.__init__).parameters)
    assert "dtr" in params or "reset" in params, params


def test_each_baud_probe_and_each_check_is_one_open_hence_one_board_reset(boards):
    b = boards(lambda i: paced([nl(f) for f in FRAMES[:40]], 0.002))
    results = find_baud(b.path, [115200, 230400], probe_s=0.6,
                        adapter_factory=lambda p, bd: BoardAdapter(b, p, bd))
    assert b.opens == 2 and [r["baud"] for r in results] == [115200, 230400]
    check_with(b, secs=1.0)
    assert b.opens == 3          # N probes + 1 check = N+1 resets of an auto-reset board


def test_find_baud_gives_a_resetting_board_time_to_boot(boards):
    b = boards(lambda i: [3.4, nl(HELLO)] + paced([nl(f) for f in FRAMES[1:60]], 0.002))
    res = find_baud(b.path, [115200], adapter_factory=lambda p, bd: BoardAdapter(b, p, bd))   # default probe_s
    assert res[0]["valid"] > 0, res


def test_reconnect_after_an_open_that_works_but_a_read_that_fails_is_rate_limited(be, monkeypatch):
    import serial
    opens: list[float] = []

    class Flaky:
        def __init__(self, port, baud, timeout=0, **kw):
            opens.append(time.monotonic())
            if len(opens) > 400:      # bound the damage of the defect: after this the port 'disappears' for good
                raise serial.SerialException("could not open port")

        @property
        def in_waiting(self):
            raise serial.SerialException("device reports readiness to read but returned no data")

        def read(self, n=1):
            raise serial.SerialException("device reports readiness to read but returned no data")

        def close(self):
            pass

    monkeypatch.setattr(serial, "Serial", Flaky)
    be.add_runtime("ser", SerialAdapter("/dev/ttyFAKE0", BAUD), processing_config_for("serial"))
    time.sleep(1.0)
    first_second = [t for t in opens if t - opens[0] <= 1.0]
    assert len(first_second) <= 20, f"{len(first_second)} opens in 1 s (capped at 400)"


def test_missing_port_is_retried_with_backoff_not_a_storm(be):
    """Contrast to the storm: when open() itself fails the loop backs off (0.25 s doubling to 2 s)."""
    ad = SerialAdapter("/dev/ttyDOES_NOT_EXIST", BAUD)
    calls = []
    orig = ad.open

    def counting_open():
        calls.append(1)
        return orig()

    ad.open = counting_open
    be.add_runtime("ser", ad, processing_config_for("serial"))
    time.sleep(1.2)
    assert 2 <= len(calls) <= 8


# ------------------------------------------------------------------------------------ (7) port naming

def _two_boards(tmp_path, boards):
    links = tmp_path / "dev"
    links.mkdir()
    wearable = boards(lambda i: [nl(HELLO)] + paced([nl(f) for f in FRAMES[1:]]))
    other = boards(lambda i: [b"hello from the other board\r\n"])
    (links / "ttyACM10").symlink_to(wearable.path)     # lexically before ttyACM2
    (links / "ttyACM2").symlink_to(other.path)         # the one an operator would call 'the first'
    return links, wearable, other


def test_glob_with_two_boards_picks_the_naturally_first_name_and_serial_check_names_it(tmp_path, boards):
    links, wearable, other = _two_boards(tmp_path, boards)
    ad = BoardAdapter(wearable, str(links / "ttyACM*"))
    ad.open()
    ad.close()
    assert ad.port.endswith("ttyACM2")                 # natural order: ACM2 before ACM10 (not lexical)
    assert len(ad.candidates) == 2
    res = check_with(wearable, port=str(links / "ttyACM*"))
    assert "ttyACM2" in named(res.report, "port opens").detail


def test_ambiguous_glob_is_reported(tmp_path, boards):
    links, wearable, other = _two_boards(tmp_path, boards)
    res = check_with(wearable, port=str(links / "ttyACM*"))
    assert "ttyACM2" in text(res.report), text(res.report)


# ------------------------------------------------------------------------------------ (8) tiny-line flood

def test_flood_of_tiny_lines_does_not_starve_or_lose_valid_frames(be, boards):
    spam = b"x\n" * 60000                              # ~120 kB of one-byte debug lines
    b = boards(lambda i: [nl(HELLO), spam] + [nl(f) for f in FRAMES[1:60]])
    t0 = time.monotonic()
    rt, ad = run_runtime(be, b, lambda r: r.counters["frames"] >= 60060, timeout=20, session=True)
    assert time.monotonic() - t0 < 15
    assert rt.counters["records"] == 60 and rt.counters["invalid_frames"] == 60000
    assert rt.counters["frame_errors"] == 0 and rt.connection == "connected"


# ------------------------------------------------------------------------------------ (9) NUL bytes

def test_a_leading_nul_line_at_connect_is_discarded_like_any_partial_line(boards):
    b = boards(lambda i: [b"\x00\x00\x00\x00\n"] + paced([nl(f) for f in FRAMES]))
    res = check_with(b, secs=3.5)
    assert res.stats["invalid"] == {} and res.stats["valid"] == len(FRAMES)


def test_nul_bytes_inside_a_line_are_rejected_visibly_not_repaired(be, boards):
    b = boards(lambda i: paced([b"\x00" + nl(f) for f in FRAMES[:60]], 0.002))
    rt, _ = run_runtime(be, b, lambda r: r.counters["frames"] >= 59)
    # the first line (NUL + the hello) is recovered by the hello resync; every later NUL-prefixed frame is
    # rejected visibly, never repaired
    assert rt.counters["records"] <= 1 and rt.counters["invalid_frames"] >= 58
    assert rt.counters["frame_errors"] == 0


def test_nul_prefixed_json_is_not_diagnosed_as_wrong_baud(boards):
    b = boards(lambda i: paced([b"\x00" + nl(f) for f in FRAMES]))
    rep = check_with(b).report
    assert named(rep, "contract frames").level == "FAIL"
    assert "WRONG BAUD" not in text(rep), text(rep)


def test_nul_padded_stream_is_not_called_a_hang(boards):
    b = boards(lambda i: paced([nl(f) + b"\x00\x00" for f in FRAMES]))
    rep = check_with(b, secs=3.2).report      # the stall rule needs > 2 s without a valid frame
    assert named(rep, "data arrives").level == "PASS"
    assert "stopped sending" not in text(rep), text(rep)


# ------------------------------------------------------------------------------------ (10) host speaks first

def test_nothing_is_ever_written_to_the_device(boards):
    b = boards(lambda i: paced([nl(f) for f in FRAMES[:60]], 0.002))
    check_with(b, secs=1.0)
    assert b.host_wrote() == b""
    assert not hasattr(SerialAdapter, "write")


def test_a_board_that_answers_only_after_a_host_byte_is_reported_as_silent(boards):
    b = boards(lambda i: [("wait_rx", 3.0), nl(HELLO)] + paced([nl(f) for f in FRAMES[1:]]))
    res = check_with(b, secs=1.5)
    assert named(res.report, "data arrives").level == "FAIL"
    assert b.host_wrote() == b""


def test_silent_board_message_mentions_that_the_host_sends_nothing(boards):
    b = boards(lambda i: [("wait_rx", 3.0), nl(HELLO)])
    res = check_with(b, secs=1.5)
    out = text(res.report)
    assert "DTR" in out or "command" in out or "host sends" in out, out
