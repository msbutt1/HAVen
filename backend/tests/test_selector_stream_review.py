"""Adversarial review of DeviceIdSerialAdapter and StreamAdapter. SIMULATED data only, offline.

xfail(strict=True): a reproduced defect (the test states the behaviour that SHOULD hold). Plain tests: verified behaviour.
"""

from __future__ import annotations

import json
import os
import select
import sys
import threading
import time
import types

import pytest
from conftest import sim_frames, wait_until

from vibeguard_signals import contract
from vibeguard_signals.replay import replay_lines
from vibeguard_backend import serial_select
from vibeguard_backend.adapters import AdapterDisconnected, EndOfStream, MAX_LINE_BYTES
from vibeguard_backend.serial_select import DeviceIdSerialAdapter
from vibeguard_backend.service import processing_config_for
from vibeguard_backend.stream import StreamAdapter

A, B = "vg-w-2805a5075554", "vg-w-aaaaaaaaaaaa"


def dev_frames(device_id, duration_s=3, run_id="fw"):
    out = []
    for f in sim_frames(duration_s=duration_s, run_id=run_id):
        r = json.loads(f)
        r["device_id"] = device_id
        r["boot_id"] = "b-" + device_id[-8:].ljust(8, "0") + "00000000"
        out.append((json.dumps(r, separators=(",", ":")) + "\n").encode())
    return out


class FakeSer:
    """Stands in for SerialAdapter inside serial_select. script[port] = list of bytes / Exception / None (silence)."""
    scripts: dict = {}
    instances: list = []

    def __init__(self, port, baud, dtr=None, rts=None):
        self.port, self.baud, self.dtr, self.rts = port, baud, dtr, rts
        self.closed = self.is_open = False
        self.partial_discarded = self.oversize_resyncs = self.hello_recovered = 0
        self.items = list(FakeSer.scripts.get(port, []))
        FakeSer.instances.append(self)

    def open(self):
        if self.port not in FakeSer.scripts:
            raise AdapterDisconnected(f"cannot open {self.port}")
        self.is_open = True

    def read_line(self, timeout):
        if self.items:
            it = self.items.pop(0)
            if isinstance(it, BaseException):
                raise it
            if it is not None:
                time.sleep(0.001)
                return it
        time.sleep(min(timeout, 0.02))
        return None

    def close(self):
        self.closed, self.is_open = True, False


@pytest.fixture
def fake(monkeypatch, tmp_path):
    FakeSer.scripts, FakeSer.instances = {}, []
    monkeypatch.setattr(serial_select, "SerialAdapter", FakeSer)

    def make(spec: dict):
        for name, lines in spec.items():
            (tmp_path / name).touch()
            FakeSer.scripts[str(tmp_path / name)] = lines
        return str(tmp_path / "ttyUSB*")
    return make


# ------------------------------------------------------------------------------ DeviceIdSerialAdapter

def test_handover_keeps_order_and_the_inner_adapters_own_buffer(fake, tmp_path):
    fr = dev_frames(A)
    pat = fake({"ttyUSB0": dev_frames(B)[:3], "ttyUSB1": fr[:10]})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=2)
    ad.open()
    got = []
    while len(got) < 10:
        line = ad.read_line(0.1)
        if line:
            got.append(line)
    assert got == fr[:10]                       # probing consumed some, the inner adapter the rest: same order, none lost
    assert [i.closed for i in FakeSer.instances] == [True, False]   # the wrong board's port is released at once
    ad.close()


def test_a_reconnect_tries_the_last_matching_port_first(fake, tmp_path):
    pat = fake({"ttyUSB0": dev_frames(B), "ttyUSB1": dev_frames(A)})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=2)
    ad.open(); ad.close()
    FakeSer.scripts[str(tmp_path / "ttyUSB0")], FakeSer.scripts[str(tmp_path / "ttyUSB1")] = dev_frames(A), dev_frames(B)
    ad.open()                                   # the boards swapped names: the old name is probed first, found wrong, then the other
    assert ad.port.endswith("ttyUSB0")
    ad.close()


def test_an_unexpected_error_while_probing_does_not_leak_the_port(fake):
    pat = fake({"ttyUSB0": [KeyboardInterrupt()]})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=2)
    with pytest.raises(KeyboardInterrupt):
        ad.open()
    assert all(i.closed for i in FakeSer.instances)


def test_a_port_streaming_deeply_nested_json_is_a_visible_disconnect_not_a_crash(fake):
    pat = fake({"ttyUSB0": [b"[" * 30000 + b"]" * 30000 + b"\n"]})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=1)
    with pytest.raises(AdapterDisconnected):
        ad.open()
    assert all(i.closed for i in FakeSer.instances)


def test_the_runtime_survives_the_nested_json_line_when_there_is_no_selector(backend):
    """Control for the test above: the runtime's own parse guard handles the same line."""
    from conftest import ScriptedAdapter
    rt = backend.add_runtime("w", ScriptedAdapter([b"[" * 30000 + b"]" * 30000 + b"\n"] + dev_frames(A)[:5]),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.finished, 10) and rt.error is None


def test_describe_does_not_claim_a_port_after_close_or_a_failed_reopen(fake, tmp_path):
    pat = fake({"ttyUSB1": dev_frames(A)})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=2)
    ad.open(); ad.close()
    os.remove(tmp_path / "ttyUSB1")
    with pytest.raises(AdapterDisconnected):
        ad.open()
    assert ad.describe()["port"] is None


def test_ports_seen_forgets_vanished_ports(fake, tmp_path):
    pat = fake({"ttyUSB0": dev_frames(B), "ttyUSB1": dev_frames(A)})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=2)
    ad.open(); ad.close()
    os.remove(tmp_path / "ttyUSB0")
    FakeSer.scripts.pop(str(tmp_path / "ttyUSB0"))
    ad.open()
    assert str(tmp_path / "ttyUSB0") not in ad.describe()["ports_seen"]
    ad.close()


def test_stop_is_prompt_while_probing_silent_ports(backend, fake):
    pat = fake({f"ttyUSB{i}": [] for i in range(4)})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=1.5)       # 6 s of probing before the runtime can see _stop
    rt = backend.add_runtime("w", ad, processing_config_for("sim"))
    time.sleep(0.3)
    t0 = time.monotonic()
    assert rt.stop(timeout=1.0) is True, f"still probing after {time.monotonic() - t0:.1f}s"


def test_a_silent_port_costs_the_whole_probe_window_per_port(fake):
    pat = fake({"ttyUSB0": [], "ttyUSB1": [], "ttyUSB2": dev_frames(A)})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=0.5)
    t0 = time.monotonic()
    ad.open()
    took = time.monotonic() - t0
    assert 0.95 < took < 2.0                    # documents the cost: 2 silent ports x probe_s before the board is even tried
    ad.close()


def test_describe_is_safe_against_a_concurrent_close(fake):
    pat = fake({"ttyUSB0": dev_frames(A)})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=2)
    ad.open()
    inner = ad._inner

    class Racy(type(inner)):
        @property
        def partial_discarded(self):
            ad.close()                          # the runtime thread closes the adapter between the reads
            return 0
    inner.__class__ = Racy
    ad.describe()


def test_a_pinned_adapter_whose_port_starts_carrying_the_other_board_is_not_followed(backend, fake):
    pat = fake({"ttyUSB0": dev_frames(A)[:25] + dev_frames(B, run_id="other")})
    ad = DeviceIdSerialAdapter(pat, 115200, A, probe_s=2)
    rt = backend.add_runtime("w", ad, processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] > 40, 10)
    assert rt.device_id == A


def test_default_dtr_rts_in_the_cli_do_not_reset_the_unwanted_board(monkeypatch, tmp_path):
    """serve --device-id without --no-dtr probes EVERY candidate with the driver default (DTR/RTS asserted)."""
    from vibeguard_backend import cli
    seen = {}

    class Spy:
        def __init__(self, port, baud, expect, dtr=None, rts=None, **kw):
            seen.update(dtr=dtr, rts=rts)
            raise ValueError("stop here")
    monkeypatch.setattr(serial_select, "DeviceIdSerialAdapter", Spy)
    cli.main(["serve", "--device", "serial", "--port", "/dev/cu.usbserial*", "--baud", "921600", "--device-id", A, "--data-dir", str(tmp_path / "d")])
    assert seen, "adapter was not built"
    assert seen["dtr"] is False and seen["rts"] is False


# ------------------------------------------------------------------------------ StreamAdapter

def _read_all(ad, timeout=0.2):
    out = []
    while True:
        try:
            line = ad.read_line(timeout)
        except EndOfStream:
            return out
        if line is not None:
            out.append(line)


def test_eof_without_trailing_newline_still_delivers_the_last_line(tmp_path):
    f = tmp_path / "a.ndjson"
    fr = dev_frames(A)
    f.write_bytes(b"".join(fr).rstrip(b"\n"))
    ad = StreamAdapter(str(f)); ad.open()
    got = _read_all(ad)
    assert len(got) == len(fr) and got[-1] == fr[-1].rstrip(b"\n")
    assert contract.parse_line(got[-1])[0] is not None


def test_crlf_lines_are_accepted_by_the_validator(tmp_path):
    f = tmp_path / "c.ndjson"
    fr = dev_frames(A)
    f.write_bytes(b"".join(x.rstrip(b"\n") + b"\r\n" for x in fr))
    ad = StreamAdapter(str(f)); ad.open()
    got = _read_all(ad)
    assert len(got) == len(fr) and all(contract.parse_line(g)[1] == [] for g in got)


def test_binary_garbage_and_empty_file(tmp_path):
    f = tmp_path / "b.bin"
    f.write_bytes(bytes(range(256)) * 10)
    ad = StreamAdapter(str(f)); ad.open()
    assert b"".join(_read_all(ad)) == bytes(range(256)) * 10
    e = tmp_path / "e"; e.write_bytes(b"")
    ad2 = StreamAdapter(str(e)); ad2.open()
    with pytest.raises(EndOfStream):
        ad2.read_line(0.1)


def _stdin_pipe(monkeypatch):
    r, w = os.pipe()
    rf = os.fdopen(r, "rb", 0)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=rf))
    return rf, w


def test_a_partial_line_on_a_pipe_waits_for_its_newline(monkeypatch):
    rf, w = _stdin_pipe(monkeypatch)
    ad = StreamAdapter("-"); ad.open()
    os.write(w, b'{"schema"')
    t0 = time.monotonic()
    assert ad.read_line(0.2) is None and time.monotonic() - t0 < 0.6
    os.write(w, b':1}\n')
    assert ad.read_line(0.5) == b'{"schema":1}\n'
    os.close(w)
    with pytest.raises(EndOfStream):
        ad.read_line(0.5)
    rf.close()


def test_a_newline_free_flood_on_stdin_cannot_hold_read_line_past_its_timeout(monkeypatch):
    rf, w = _stdin_pipe(monkeypatch)
    stop = threading.Event()

    def flood():
        chunk = b"x" * 16384
        end = time.monotonic() + 3.5                  # a steady newline-free stream for 3.5 s (tens of MB at most)
        while not stop.is_set() and time.monotonic() < end:
            try:
                os.write(w, chunk)
            except OSError:
                return
            time.sleep(0.001)
    t = threading.Thread(target=flood, daemon=True); t.start()
    ad = StreamAdapter("-"); ad.open()
    res = {}

    def call():
        try:
            res["v"] = ad.read_line(0.1)
        except BaseException as exc:  # noqa: BLE001
            res["v"] = exc
    c = threading.Thread(target=call, daemon=True); c.start()
    c.join(2.0)
    returned = not c.is_alive()
    buffered = len(ad._buf)
    stop.set(); t.join(2); os.close(w); c.join(5); rf.close()
    assert returned, f"read_line(0.1) still blocked after 2 s with {buffered} bytes buffered"


def test_an_overlong_line_is_truncated_like_the_serial_adapter(tmp_path):
    f = tmp_path / "huge"
    f.write_bytes(b"x" * (3 * 1024 * 1024))
    ad = StreamAdapter(str(f)); ad.open()
    assert len(ad.read_line(0.1)) <= MAX_LINE_BYTES + 1


def test_a_fifo_path_is_a_valid_stream_source(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    fr = dev_frames(A)[:5]

    def writer():
        with open(fifo, "wb") as fh:
            fh.write(b"".join(fr))
    threading.Thread(target=writer, daemon=True).start()
    ad = StreamAdapter(str(fifo)); ad.open()
    assert _read_all(ad) == fr


def test_an_unreadable_stream_file_is_a_clean_disconnect(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    f = tmp_path / "locked"; f.write_bytes(b"{}\n"); f.chmod(0)
    try:
        with pytest.raises(AdapterDisconnected):
            StreamAdapter(str(f)).open()
    finally:
        f.chmod(0o600)


def test_reopen_after_close_reads_the_file_again(tmp_path):
    f = tmp_path / "r.ndjson"; f.write_bytes(b"".join(dev_frames(A)[:4]))
    ad = StreamAdapter(str(f)); ad.open()
    assert len(_read_all(ad)) == 4
    ad.close(); ad.open()
    assert len(_read_all(ad)) == 4


def _status(t_us, boot, seq):
    fr = dev_frames(A)
    r = json.loads(fr[1]); assert r["type"] == "status"
    r.update(t_us=t_us, boot_id=boot, seq=seq)
    return (json.dumps(r, separators=(",", ":")) + "\n").encode()


def _due_sequence(lines):
    ad = StreamAdapter("-", paced=True)
    return [ad._due_s(l) for l in lines]


def test_paced_timeline_appends_each_boot_after_the_previous_one():
    lines = [_status(1_000_000, "b-aaaaaaaaaaaaaaaa", 0), _status(3_000_000, "b-aaaaaaaaaaaaaaaa", 1),
             _status(500_000, "b-bbbbbbbbbbbbbbbb", 0), _status(1_500_000, "b-bbbbbbbbbbbbbbbb", 1)]
    assert _due_sequence(lines) == pytest.approx([0.0, 2.0, 2.0, 3.0])


def test_paced_timeline_matches_replay_when_a_record_arrives_late():
    lines = [_status(1_000_000, "b-aaaaaaaaaaaaaaaa", 0), _status(10_000_000, "b-aaaaaaaaaaaaaaaa", 1),
             _status(2_000_000, "b-aaaaaaaaaaaaaaaa", 2),            # late (reordered) record of the same boot
             _status(1_000_000, "b-bbbbbbbbbbbbbbbb", 0), _status(2_000_000, "b-bbbbbbbbbbbbbbbb", 1)]
    expect = [c for c, _ in replay_lines(lines)]
    assert _due_sequence(lines) == pytest.approx(expect)


def test_paced_speed_edges(tmp_path):
    for bad in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            StreamAdapter("-", speed=bad)
    f = tmp_path / "s.ndjson"; f.write_bytes(_status(1_000_000, "b-aaaaaaaaaaaaaaaa", 0) + _status(60_000_000, "b-aaaaaaaaaaaaaaaa", 1))
    ad = StreamAdapter(str(f), paced=True, speed=1e-300); ad.open()
    assert ad.read_line(0.05) is not None           # first record is due at 0
    t0 = time.monotonic()
    assert ad.read_line(0.05) is None and time.monotonic() - t0 < 0.5   # frozen far in the future, but the call returns
    ad2 = StreamAdapter(str(f), paced=True, speed=float("inf")); ad2.open()
    assert len(_read_all(ad2)) == 2


def test_clock_choice():
    assert StreamAdapter("-").clock == "device" and StreamAdapter("-", paced=True).clock == "host"


def test_a_stalled_live_pipe_is_not_reported_as_fresh(monkeypatch, backend):
    rf, w = _stdin_pipe(monkeypatch)
    rt = backend.add_runtime("w", StreamAdapter("-"), processing_config_for("sim"))
    for line in dev_frames(A)[:12]:
        os.write(w, line)
    assert wait_until(lambda: rt.counters["records"] >= 12, 10)
    time.sleep(2.5)                                     # producer hung: no bytes, no EOF
    try:
        assert rt.status()["data_fresh"] is False, "still 'fresh' after 2.5 s of silence"
    finally:
        os.close(w)
        wait_until(lambda: rt.finished, 5)
        rf.close()


def test_a_finished_stream_is_not_fresh_and_ends_the_runtime(backend, tmp_path):
    f = tmp_path / "f.ndjson"; f.write_bytes(b"".join(dev_frames(A)))
    rt = backend.add_runtime("w", StreamAdapter(str(f)), processing_config_for("sim"))
    assert wait_until(lambda: rt.finished, 10)
    st = rt.status()
    assert st["data_fresh"] is False and rt.error is None


def test_the_same_file_read_twice_does_not_break_the_second_runtime(backend, tmp_path):
    f = tmp_path / "t.ndjson"; f.write_bytes(b"".join(dev_frames(A)))
    r1 = backend.add_runtime("w1", StreamAdapter(str(f)), processing_config_for("sim"))
    assert wait_until(lambda: r1.finished, 10)
    r2 = backend.add_runtime("w2", StreamAdapter(str(f)), processing_config_for("sim"))
    assert wait_until(lambda: r2.finished, 10) and r2.error is None
    assert r2.counters["records"] == r1.counters["records"]
