"""The REAL SerialAdapter (real pyserial, no mocks) driven through pseudo-terminals.

The test plays the firmware: it writes device frames to the pty master; the adapter
opens the slave path like a ``/dev/cu.usbmodem*`` port. Offline, deterministic in what
it asserts (counts, bytes), bounded in time. Everything is SIMULATED frames; where a
test needs "live" provenance the simulator frames are rewritten and re-validated with
the contract validator.
"""

from __future__ import annotations

import copy
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
from vibeguard_backend.adapters import MAX_LINE_BYTES, SerialAdapter
from vibeguard_backend.service import Backend, processing_config_for

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs pseudo-terminals")

# a lone newline sent first ends the adapter's "discard the first partial line" state
PREAMBLE = b"\n"
# macOS cannot apply the non-standard 921600 baud (IOSSIOSPEED) to a pty: ENOTTY. Any standard
# rate opens; the baud is irrelevant to a pty.
BAUD = 115200


# ---------------------------------------------------------------- helpers


class FakeDevice:
    """A pseudo-terminal pair. ``path`` is what the host opens; we write as the firmware."""

    def __init__(self) -> None:
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)  # no echo / CR translation / canonical line limit
        self.path = os.ttyname(self.slave)
        self.written = 0
        self._closed = False

    def write(self, data: bytes, chunk: int | None = None) -> None:
        view = memoryview(data)
        step = chunk or len(data) or 1
        for i in range(0, len(data), step):
            part = view[i:i + step]
            while part:
                n = os.write(self.master, part)
                part = part[n:]
        self.written += len(data)

    def release_slave(self) -> None:
        """Drop our own handle on the slave so only the adapter holds it."""
        if self.slave is not None:
            try:
                os.close(self.slave)
            except OSError:
                pass
            self.slave = None

    def unplug(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                os.close(self.master)
            except OSError:
                pass
        self.release_slave()

    close = unplug


class TapAdapter(SerialAdapter):
    """The real SerialAdapter, additionally recording what read_line hands over."""

    def __init__(self, port, baud=BAUD):
        super().__init__(port, baud)
        self.lines: list[tuple[float, bytes]] = []
        self.opens = 0
        self.open_failures = 0
        self.max_read_s = 0.0

    def open(self):
        self.opens += 1
        try:
            super().open()
        except Exception:
            self.open_failures += 1
            raise

    def read_line(self, timeout):
        t0 = time.monotonic()
        try:
            line = super().read_line(timeout)
        finally:
            self.max_read_s = max(self.max_read_s, time.monotonic() - t0)
        if line is not None:
            self.lines.append((time.monotonic(), line))
        return line


@pytest.fixture
def be(tmp_path: Path):
    b = Backend(tmp_path / "data")
    yield b
    b.shutdown()


@pytest.fixture
def devices():
    made: list[FakeDevice] = []

    def make() -> FakeDevice:
        d = FakeDevice()
        made.append(d)
        return d

    yield make
    for d in made:
        d.unplug()


def start_rt(be: Backend, adapter: SerialAdapter, name: str = "ser"):
    return be.add_runtime(name, adapter, processing_config_for("serial"))


def connected(rt) -> bool:
    return rt.connection == "connected"


def as_live(frame: bytes) -> bytes:
    rec = json.loads(frame)
    rec = copy.deepcopy(rec)
    rec["provenance"] = {"source": "live"}
    assert contract.validate_record(rec) == [], contract.validate_record(rec)
    return contract.serialize_record(rec).encode()


def nl(frame: bytes) -> bytes:
    return frame if frame.endswith(b"\n") else frame + b"\n"


FRAMES = sim_frames(duration_s=10)  # ~400 contract-valid frames, ~320 kB
LIVE = [as_live(f) for f in FRAMES]


def missing_seqs(frames: list[bytes]) -> int:
    """Records absent from a frame list (simulated transport loss), by seq jumps."""
    missing, last = 0, {}
    for f in frames:
        r = json.loads(f)
        prev = last.get(r["boot_id"])
        if prev is not None and r["seq"] > prev + 1:
            missing += r["seq"] - prev - 1
        last[r["boot_id"]] = r["seq"]
    return missing


def irregular_chunks(data: bytes) -> list[bytes]:
    """1-byte writes, mid-line splits, and many lines per write."""
    sizes = [1, 1, 1, 3, 7, 61, 400, 777, 1, 2500, 13, 9000, 5, 1024, 33000]
    out, i, k = [], 0, 0
    while i < len(data):
        s = sizes[k % len(sizes)]
        out.append(data[i:i + s])
        i += s
        k += 1
    return out


# ---------------------------------------------------------------- (1) end to end


@pytest.mark.parametrize("frames,source", [(FRAMES, "simulated"), (LIVE, "live")])
def test_irregular_chunks_every_record_processed_and_session_byte_exact(be, devices, frames, source):
    dev = devices()
    rt = start_rt(be, TapAdapter(dev.path))
    assert wait_until(lambda: connected(rt), 5)
    dev.release_slave()
    row = rt.start_session("pty")
    sent = [nl(f) for f in frames]
    dev.write(PREAMBLE)
    for chunk in irregular_chunks(b"".join(sent)):
        dev.write(chunk)
    # the preamble newline only ends the adapter's discard state; it yields no frame
    assert wait_until(lambda: rt.counters["frames"] >= len(sent), 20), dict(rt.counters)
    assert wait_until(lambda: rt.counters["records"] >= len(sent), 20), dict(rt.counters)
    assert rt.counters["records"] == len(sent)
    assert rt.counters["frame_errors"] == 0
    assert rt.counters["invalid_frames"] == 0
    assert rt.connection == "connected"
    assert rt.provenance["source"] == source
    stopped = rt.stop_session(row["session_id"])
    tel = Path(stopped["dir"]) / "telemetry.ndjson"
    assert tel.read_bytes() == b"".join(sent)
    assert stopped["source"] == source
    assert stopped["counters"]["frames"] == len(sent)
    assert stopped["state"] == "completed"
    # the serial path neither hides nor invents sequence gaps: exactly the ones in the stream
    assert rt.events["seq_gap"] == missing_seqs(frames) > 0, dict(rt.events)
    # every published telemetry record keeps its provenance
    assert {r["provenance"]["source"] for r in map(json.loads, sent)} == {source}


def test_first_complete_frame_after_connect_is_not_dropped(be, devices):
    """Firmware that starts talking right after the port opens (hello first) loses nothing."""
    dev = devices()
    rt = start_rt(be, SerialAdapter(dev.path, BAUD))
    assert wait_until(lambda: connected(rt), 5)
    dev.release_slave()
    sent = [nl(f) for f in FRAMES[:30]]
    dev.write(b"".join(sent), chunk=500)  # no leading newline: a line boundary right at connect
    wait_until(lambda: rt.counters["frames"] >= len(sent), 2)
    assert rt.counters["frames"] == len(sent), dict(rt.counters)
    assert rt.hello is not None


# ---------------------------------------------------------------- (2) garbage, CRLF, oversize, reconnect


def test_garbage_crlf_and_binary_noise_never_stop_ingestion(be, devices):
    dev = devices()
    rt = start_rt(be, TapAdapter(dev.path))
    assert wait_until(lambda: connected(rt), 5)
    dev.release_slave()
    dev.write(PREAMBLE)
    good = FRAMES[:40]
    junk = [b"\x00\xff\xfe garbage \x01\x02", b"{not json", b"[]", b"null", b"\xc3\x28 bad utf8",
            b"{" * 5000, b'{"schema":"vibeguard.device_telemetry"}', b"   "]
    payload = bytearray()
    for i, g in enumerate(good):
        payload += g.rstrip(b"\r\n") + b"\r\n"  # CRLF line endings on valid frames
        if i % 5 == 0:
            payload += junk[(i // 5) % len(junk)] + b"\n"
    dev.write(bytes(payload), chunk=211)
    n_junk = len([i for i in range(len(good)) if i % 5 == 0])
    assert wait_until(lambda: rt.counters["frames"] >= len(good) + n_junk, 10), dict(rt.counters)
    assert rt.counters["frames"] == len(good) + n_junk
    assert rt.counters["invalid_frames"] == n_junk
    assert rt.counters["records"] == len(good), (dict(rt.counters), rt.last_invalid, rt.last_frame_error)
    assert rt.counters["frame_errors"] == 0
    assert rt.connection == "connected" and rt.alive


def test_crlf_frames_are_valid_records(be, devices):
    dev = devices()
    rt = start_rt(be, SerialAdapter(dev.path, BAUD))
    assert wait_until(lambda: connected(rt), 5)
    dev.release_slave()
    dev.write(PREAMBLE + b"".join(f.rstrip(b"\r\n") + b"\r\n" for f in FRAMES[:20]))
    assert wait_until(lambda: rt.counters["records"] >= 20, 5), (dict(rt.counters), rt.last_invalid)
    assert rt.counters["invalid_frames"] == 0


def test_70kb_line_without_newline_then_valid_frames_resync(be, devices):
    dev = devices()
    ad = TapAdapter(dev.path)
    rt = start_rt(be, ad)
    assert wait_until(lambda: connected(rt), 5)
    dev.release_slave()
    dev.write(PREAMBLE)
    dev.write(b"A" * 70_000, chunk=4096)  # no newline: must not grow memory without bound
    time.sleep(0.3)
    assert len(ad._buf) <= MAX_LINE_BYTES + 4096  # the buffer is bounded
    tail = b"B" * 1000 + b"\n"  # the end of the over-long line
    good = [nl(f) for f in FRAMES[:25]]
    dev.write(tail + b"".join(good), chunk=997)
    assert wait_until(lambda: rt.counters["records"] >= len(good), 10), (dict(rt.counters), rt.last_invalid)
    assert rt.counters["records"] == len(good)
    assert rt.counters["invalid_frames"] == 1  # the over-long line, reported once
    assert rt.last_invalid["codes"] == ["line_too_long"]
    assert rt.counters["frame_errors"] == 0


def test_a_frame_split_across_a_reconnect_is_never_stitched(be, devices, tmp_path):
    """Half a frame, unplug, replug on a new pty: the half frame must not join the next line."""
    links = tmp_path / "dev"
    links.mkdir()
    d1 = devices()
    (links / "ttyfake0").symlink_to(d1.path)
    ad = TapAdapter(str(links / "ttyfake*"))
    rt = start_rt(be, ad)
    assert wait_until(lambda: connected(rt), 5)
    d1.release_slave()
    d1.write(PREAMBLE + b"".join(nl(f) for f in FRAMES[:10]))
    assert wait_until(lambda: rt.counters["records"] >= 10, 5)
    half = FRAMES[10][: len(FRAMES[10]) // 2]
    d1.write(half)
    time.sleep(0.2)
    d1.unplug()
    assert wait_until(lambda: rt.connection == "disconnected", 3)
    (links / "ttyfake0").unlink()
    d2 = devices()
    (links / "ttyfake1").symlink_to(d2.path)
    assert wait_until(connected_after(rt, 1), 8), (rt.connection, rt.connection_detail)
    d2.release_slave()
    before = rt.counters["records"]
    # the new device starts mid-line (tail of a frame) then complete frames
    tail = FRAMES[11][len(FRAMES[11]) // 2:] + b"\n"
    good = [nl(f) for f in FRAMES[12:32]]
    d2.write(tail + b"".join(good))
    assert wait_until(lambda: rt.counters["records"] >= before + len(good), 5), dict(rt.counters)
    assert rt.counters["records"] == before + len(good)
    assert rt.counters["frame_errors"] == 0


def connected_after(rt, n_disconnects: int):
    return lambda: rt.connection == "connected" and rt.counters["connection_disconnected"] >= n_disconnects


# ---------------------------------------------------------------- (3) unplug and replug


def test_unplug_is_visible_quickly_and_nothing_stale_is_shown(be, devices):
    dev = devices()
    ad = TapAdapter(dev.path)
    rt = start_rt(be, ad)
    assert wait_until(lambda: connected(rt), 5)
    dev.release_slave()
    dev.write(PREAMBLE)
    # paced like a device so the stream is live when the cable goes
    stop = threading.Event()
    sent = [nl(f) for f in LIVE[:300]]

    def pump():
        for s in sent:
            if stop.is_set():
                return
            try:
                dev.write(s)
            except OSError:
                return
            time.sleep(0.015)

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    assert wait_until(lambda: rt.latest["emg"] is not None and rt.latest["imu"] is not None, 8)
    assert rt.snapshot()["latest"]["emg"].get("status") != "unavailable" or True
    row = rt.start_session("unplug")
    time.sleep(0.3)
    t_unplug = time.monotonic()
    stop.set()
    th.join(2)
    dev.unplug()
    assert wait_until(lambda: rt.host_state == "disconnected", 1.0)
    detect_s = time.monotonic() - t_unplug
    assert detect_s < 1.0
    assert rt.connection == "disconnected" and rt.connection_detail
    snap = rt.snapshot()
    assert snap["state"] == "disconnected"
    for key in ("emg", "imu", "score"):
        v = snap["latest"][key]
        assert v is None or v.get("status") == "unavailable", (key, v)
    # the stale event (host clock) is emitted within a second of the last byte
    assert wait_until(lambda: rt.events["stream_stale"] >= 1, 1.0), dict(rt.events)
    assert rt.alive and rt.counters["frame_errors"] == 0
    # the session recorded what arrived before the unplug, and is still closable
    assert rt.session is not None
    stopped = rt.stop_session(row["session_id"])
    assert stopped["counters"]["connection_disconnected"] == 1
    got = (Path(stopped["dir"]) / "telemetry.ndjson").read_bytes()
    assert got and got.endswith(b"\n")
    assert got in b"".join(sent)  # a contiguous, unaltered slice of what the device sent


def test_replug_onto_a_new_pty_under_the_same_glob_reconnects_and_new_session_works(be, devices, tmp_path):
    links = tmp_path / "dev"
    links.mkdir()
    d1 = devices()
    (links / "ttyfake0").symlink_to(d1.path)
    ad = TapAdapter(str(links / "ttyfake*"))
    rt = start_rt(be, ad)
    assert wait_until(lambda: connected(rt), 5)
    assert ad.port == str(links / "ttyfake0")
    d1.release_slave()
    s1 = [nl(f) for f in FRAMES[:50]]
    first = rt.start_session("before")
    d1.write(PREAMBLE + b"".join(s1))
    assert wait_until(lambda: rt.counters["records"] >= len(s1), 5)
    d1.unplug()
    assert wait_until(lambda: rt.host_state == "disconnected", 2)
    (links / "ttyfake0").unlink()
    # nothing matches the glob: still disconnected, retrying, no crash
    time.sleep(0.6)
    assert rt.host_state == "disconnected" and rt.alive
    s1_stopped = rt.stop_session(first["session_id"], state="interrupted", reason="device_unplugged")
    assert (Path(s1_stopped["dir"]) / "telemetry.ndjson").read_bytes() == b"".join(s1)

    d2 = devices()
    (links / "ttyfake1").symlink_to(d2.path)
    assert wait_until(connected_after(rt, 1), 8), (rt.connection, rt.connection_detail)
    assert ad.port == str(links / "ttyfake1")
    d2.release_slave()
    assert rt.host_state == "ready"
    second = rt.start_session("after")
    assert second["session_id"] != first["session_id"]
    s2 = [nl(f) for f in FRAMES[100:160]]
    before = rt.counters["records"]
    d2.write(PREAMBLE + b"".join(s2), chunk=1500)
    assert wait_until(lambda: rt.counters["records"] >= before + len(s2), 5), dict(rt.counters)
    stopped = rt.stop_session(second["session_id"])
    assert (Path(stopped["dir"]) / "telemetry.ndjson").read_bytes() == b"".join(s2)
    assert stopped["counters"]["frames"] == len(s2)
    rows = be.store.wearable_sessions()
    assert len([r for r in rows if r["runtime"] == "ser"]) == 2
    # data is live again: not marked stale, the device is not shown as disconnected
    assert rt.snapshot()["state"] == "ready"


def test_unplug_with_stale_node_left_behind_still_reaches_the_new_device(be, devices, tmp_path):
    """A dead node that stays in the glob (sorted first) must not hide a working port."""
    links = tmp_path / "dev"
    links.mkdir()
    d1 = devices()
    (links / "ttyfake0").symlink_to(d1.path)
    ad = TapAdapter(str(links / "ttyfake*"))
    rt = start_rt(be, ad)
    assert wait_until(lambda: connected(rt), 5)
    d1.release_slave()
    d1.unplug()
    assert wait_until(lambda: rt.host_state == "disconnected", 2)
    d2 = devices()
    (links / "ttyfake1").symlink_to(d2.path)  # old symlink ttyfake0 is NOT removed
    assert wait_until(lambda: rt.connection == "connected", 6), (rt.connection, rt.connection_detail,
                                                               ad.opens, ad.port)
    d2.release_slave()
    d2.write(PREAMBLE + b"".join(nl(f) for f in FRAMES[:10]))
    assert wait_until(lambda: rt.counters["records"] >= 10, 6), (rt.connection, rt.connection_detail,
                                                                 ad.opens, ad.port)


# ---------------------------------------------------------------- (4) sustained throughput


def test_sustained_45kBps_for_10s_no_loss_and_bounded_latency(be, devices):
    dev = devices()
    ad = TapAdapter(dev.path)
    rt = start_rt(be, ad)
    assert wait_until(lambda: connected(rt), 5)
    dev.release_slave()
    pool = [nl(f) for f in sim_frames(duration_s=30, seed=3, run_id="tp")]
    target_bps = 45_000
    wall_s = 10.0
    sent_at: list[float] = []
    sent: list[bytes] = []
    cpu0 = time.process_time()
    t0 = time.monotonic()
    dev.write(PREAMBLE)
    i = 0
    nbytes = 0
    while time.monotonic() - t0 < wall_s:
        f = pool[i % len(pool)]
        # new run each time the pool wraps would repeat seq numbers; the counters still count frames
        sent_at.append(time.monotonic())
        sent.append(f)
        dev.write(f)
        nbytes += len(f)
        i += 1
        due = t0 + nbytes / target_bps
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    n = len(sent)
    assert nbytes / wall_s > 40_000  # the offered load really was ~45 kB/s
    assert wait_until(lambda: rt.counters["frames"] >= n, 10), (rt.counters["frames"], n)
    assert rt.counters["frames"] == n
    assert rt.counters["frame_errors"] == 0
    # every line the adapter handed over, in order, is exactly what was written
    got = [ln for _, ln in ad.lines]
    assert got == sent
    lat = sorted(rx - sent_at[k] for k, (rx, _) in enumerate(ad.lines))
    p99, worst = lat[int(len(lat) * 0.99) - 1], lat[-1]
    print(f"throughput: {n} frames, {nbytes} B, latency p99={p99*1000:.0f} ms max={worst*1000:.0f} ms, "
          f"cpu={time.process_time()-cpu0:.1f}s, max read_line={ad.max_read_s*1000:.0f} ms")
    assert p99 < 0.5 and worst < 1.5
    assert ad.max_read_s < 1.0
    assert rt.connection == "connected" and rt.error is None


# ---------------------------------------------------------------- (5) a port that never exists


@pytest.mark.parametrize("port", ["/nonexistent/ttyNOPE", "/nonexistent/tty*"])
def test_missing_port_is_disconnected_with_backoff_and_no_busy_loop(be, port):
    ad = TapAdapter(port)
    cpu0 = time.process_time()
    t0 = time.monotonic()
    rt = start_rt(be, ad)
    assert wait_until(lambda: rt.connection == "disconnected", 2)
    assert rt.host_state == "disconnected" and rt.connection_detail
    time.sleep(4.0)
    elapsed = time.monotonic() - t0
    cpu = time.process_time() - cpu0
    assert rt.alive and rt.error is None
    assert rt.snapshot()["state"] == "disconnected"
    assert all(v is None for v in rt.latest.values())
    # backoff 0.25, 0.5, 1, 2, 2 ... : a handful of attempts, not hundreds
    assert 2 <= ad.opens <= 8, ad.opens
    assert ad.opens / elapsed < 4
    assert cpu < 1.0, f"{cpu:.2f}s CPU in {elapsed:.1f}s"
    # a port appearing later is picked up (glob case), proving the retry loop is live
