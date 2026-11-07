"""Ingestion robustness: malformed input never crashes or corrupts ingestion.

Acceptance check (abduljabbar_05): "Malformed messages do not crash ingestion."
Every case here drives a real DeviceRuntime (or the real SerialAdapter framing
over a fake port) and checks what the dashboard and the session record would
show: the runtime keeps running, rejected frames are counted and surfaced as
``invalid_record`` events with their contract codes, and valid frames that
follow the garbage are still processed into features.

Everything is SIMULATED and offline. No test sleeps blindly: they wait on state.
"""

from __future__ import annotations

import json
import random
import threading
from collections import Counter, deque
from pathlib import Path

import pytest
import serial

from conftest import FIXTURES, ScriptedAdapter, sim_frames, wait_until
from vibeguard_signals import contract
from vibeguard_backend.adapters import MAX_LINE_BYTES, AdapterDisconnected, SerialAdapter
from vibeguard_backend.hub import Hub
from vibeguard_backend.service import Backend, processing_config_for

# --------------------------------------------------------------------------- helpers


class RecordingHub(Hub):
    """A Hub that also keeps every published envelope, so tests can see exactly
    what a dashboard subscriber would have been sent, in hub order."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict] = []
        self._mlock = threading.Lock()

    def publish(self, channel, runtime, payload):
        msg = super().publish(channel, runtime, payload)
        with self._mlock:
            self.messages.append(msg)
        return msg

    def snapshot(self) -> list[dict]:
        with self._mlock:
            return list(self.messages)

    def derived(self, rtype: str | None = None, event: str | None = None) -> list[dict]:
        out = []
        for m in self.snapshot():
            if m["channel"] != "derived":
                continue
            p = m["payload"]
            if rtype and p["type"] != rtype:
                continue
            if event and p.get("event") != event:
                continue
            out.append(m)
        return out


@pytest.fixture
def hub() -> RecordingHub:
    return RecordingHub()


@pytest.fixture
def rbackend(tmp_path: Path, hub: RecordingHub):
    b = Backend(tmp_path / "data", hub=hub)
    yield b
    b.shutdown()


@pytest.fixture(scope="module")
def frames() -> list[bytes]:
    return sim_frames(duration_s=3)


def compact(path: Path) -> bytes:
    """A pretty-printed JSON fixture as one NDJSON line, byte-preserving (keeps
    duplicate keys and NaN literals that a json round-trip would lose)."""
    return b" ".join(ln.strip() for ln in path.read_bytes().splitlines()) + b"\n"


def mutate(frame: bytes, **changes) -> bytes:
    """A decoded valid frame with fields changed (value None deletes the field)."""
    rec = json.loads(frame)
    for k, v in changes.items():
        if v is None:
            rec.pop(k, None)
        else:
            rec[k] = v
    return json.dumps(rec, separators=(",", ":")).encode() + b"\n"


def first_index(frames: list[bytes], rtype: str) -> int:
    return next(i for i, f in enumerate(frames) if json.loads(f)["type"] == rtype)


INVALID_FIXTURES = sorted((FIXTURES / "invalid").glob("*.json"))
MANIFEST = json.loads((FIXTURES / "invalid_manifest.json").read_text())


def run_to_end(backend: Backend, script: list, clock: str = "device"):
    rt = backend.add_runtime("wearable", ScriptedAdapter(script, clock=clock),
                             processing_config_for("sim"))
    wait_until(lambda: rt.finished or rt.error is not None or not rt.alive, timeout=15)
    return rt


def garbage_lines(frames: list[bytes]) -> list[tuple[str, bytes, str]]:
    """(name, frame, expected contract code) for frames that are not JSON objects."""
    truncated = frames[2][: len(frames[2]) // 2]  # half an emg_batch, no newline
    rng = random.Random(20261107)
    rand_bin = [b"\xff" + bytes(rng.randrange(256) for _ in range(rng.randrange(1, 300))).replace(b"\n", b"")
                for _ in range(25)]
    printable = b"abcdefghijklmnopqrstuvwxyz0123456789{}[]:,\" "
    rand_txt = [b"#" + bytes(rng.choice(printable) for _ in range(rng.randrange(1, 200)))
                for _ in range(25)]
    lines = [
        ("empty", b"", "empty_line"),
        ("newline_only", b"\n", "empty_line"),
        ("whitespace_crlf", b"  \t \r\n", "empty_line"),
        ("not_utf8", b"\xff\xfe\xfa{\"a\":1}\n", "not_utf8"),
        ("bad_continuation", b'{"schema":"\xc3\x28"}\n', "not_utf8"),
        ("truncated_json", truncated, "malformed_json"),
        ("truncated_object", b'{"schema":"vibeguard.device_telemetry","version":1,\n', "malformed_json"),
        ("embedded_cr", b'{"a":\r1}\n', "malformed_json"),
        ("array", b"[1,2,3]\n", "not_object"),
        ("string", b'"emg_batch"\n', "not_object"),
        ("null", b"null\n", "not_object"),
        ("number", b"42\n", "not_object"),
        ("bool", b"true\n", "not_object"),
        ("nan_literal", b'{"x":NaN}\n', "nonfinite_value"),
        ("duplicate_key", b'{"a":1,"a":2}\n', "duplicate_key"),
        ("way_too_long", b'{"pad":"' + b"x" * 70_000 + b'"}\n', "line_too_long"),
        ("one_byte_over", b"x" * MAX_LINE_BYTES + b"\n", "line_too_long"),
    ]
    lines += [(f"rand_bin_{i}", b, "not_utf8") for i, b in enumerate(rand_bin)]
    lines += [(f"rand_txt_{i}", b, "malformed_json") for i, b in enumerate(rand_txt)]
    return lines


def interleave(frames: list[bytes], garbage: list[bytes], upto: float = 0.6) -> list[bytes]:
    """Spread garbage between the valid frames of the first ``upto`` of the stream."""
    out: list[bytes] = []
    n = int(len(frames) * upto)
    g = deque(garbage)
    per = max(1, -(-len(garbage) // max(1, n - 1)))
    for i, f in enumerate(frames):
        out.append(f)
        if 0 < i < n:
            for _ in range(per):
                if g:
                    out.append(g.popleft())
    out[n:n] = list(g)  # anything left goes in before the clean tail
    return out


def without_wallclock(p: dict) -> dict:
    return {k: v for k, v in p.items() if k not in ("rx_wall",)}


# --------------------------------------------------------------------------- runtime: non-object garbage


def test_garbage_interleaved_with_valid_frames_is_counted_exactly_and_processing_continues(
        rbackend, hub, frames):
    garbage = garbage_lines(frames)
    script = interleave(frames, [g for _, g, _ in garbage])
    assert len(script) == len(frames) + len(garbage)
    rt = rbackend.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True),
                              processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["frames"] == len(script) or rt.error is not None)

    # still acquiring, not in an error state
    assert rt.error is None
    assert rt.alive
    st = rt.status()
    assert st["state"] == "ready" and st["connection"] == "connected"

    # exact accounting: every frame is either a record or an invalid frame
    assert st["counters"]["frames"] == len(script)
    assert st["counters"]["invalid_frames"] == len(garbage)
    assert st["counters"]["records"] == len(frames)
    assert st["events"]["invalid_record"] == len(garbage)

    # each rejected frame reached subscribers as an invalid_record event, with its code
    inv = hub.derived("stream_event", "invalid_record")
    assert len(inv) == len(garbage)
    got = Counter(c for m in inv for c in m["payload"]["detail"]["codes"])
    assert got == Counter(code for _, _, code in garbage)
    assert all(m["payload"]["detail"]["where"] == "scripted" for m in inv)

    # last_invalid_frame describes the last rejected frame
    last_name, last_bytes, last_code = garbage[-1]
    assert st["last_invalid_frame"]["codes"] == [last_code]
    assert st["last_invalid_frame"]["bytes"] == len(last_bytes)
    assert st["last_invalid_frame"]["first_issue"]

    # valid frames after the garbage are still turned into features
    after = inv[-1]["hub_seq"]
    later = [m for m in hub.derived("stream_features") if m["hub_seq"] > after]
    assert {m["payload"]["stream"] for m in later} == {"emg", "imu"}
    assert rt.latest["emg"] is not None and rt.latest["imu"] is not None


def test_garbage_does_not_perturb_the_derived_stream(tmp_path, frames):
    """Rejected frames carry no sequence number or samples: apart from the
    invalid_record events themselves, processing must be identical to a clean run."""
    garbage = [g for _, g, _ in garbage_lines(frames)]

    def derived(script, name):
        hub = RecordingHub()
        b = Backend(tmp_path / name, hub=hub)
        try:
            rt = run_to_end(b, script)
            assert rt.finished and rt.error is None
        finally:
            b.shutdown()
        return [without_wallclock(m["payload"]) for m in hub.derived()
                if m["payload"].get("event") != "invalid_record"]

    clean = derived(list(frames), "clean")
    dirty = derived(interleave(frames, garbage), "dirty")
    assert clean, "a clean run must produce derived records"
    assert dirty == clean


def test_long_garbage_burst_then_valid_frames(rbackend, hub, frames):
    """A noisy cable can deliver nothing but junk for a while. The runtime must
    neither give up nor drop into error, and must resume on the first good frame."""
    rng = random.Random(5)
    burst = [b"\xfe" + bytes(rng.randrange(256) for _ in range(64)) for _ in range(500)]
    script = [*frames[:10], *burst, *frames[10:]]
    rt = run_to_end(rbackend, script)
    assert rt.error is None and rt.finished
    assert rt.counters["invalid_frames"] == 500
    assert rt.counters["records"] == len(frames)
    assert rt.events["invalid_record"] == 500
    # junk carries no sequence number: only the simulator's own lost records show as gaps
    assert rt.events["seq_gap"] == _baseline_seq_gaps(frames)


def _baseline_seq_gaps(frames: list[bytes]) -> int:
    seqs = [json.loads(f)["seq"] for f in frames]
    return sum(b - a - 1 for a, b in zip(seqs, seqs[1:]) if b > a + 1)


def test_garbage_in_host_clock_mode_keeps_runtime_live(rbackend, hub, frames):
    """Default (host) clock, as with real hardware: polling and staleness checks
    run alongside garbage without crashing."""
    garbage = [g for _, g, _ in garbage_lines(frames)][:20]
    script = interleave(frames[:60], garbage)
    rt = rbackend.add_runtime("wearable", ScriptedAdapter(script, clock="host", hold_open=True),
                              processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["frames"] == len(script) or rt.error is not None)
    assert rt.error is None and rt.alive
    assert rt.counters["invalid_frames"] == 20
    assert rt.counters["records"] == 60
    # polling keeps going on the acquisition thread after the garbage: silence becomes stale
    assert wait_until(lambda: rt.events["stream_stale"] > 0, timeout=10)
    assert rt.error is None and rt.alive


def test_runtime_publishes_status_with_invalid_counters(rbackend, hub, frames):
    """The dashboard sees rejected frames through runtime status, not only logs."""
    script = [*frames[:5], b"\xff\xff\n", b"[]\n", *frames[5:20]]
    rt = run_to_end(rbackend, script)
    assert rt.error is None
    status = rbackend.runtime_statuses()[0]
    assert status["counters"]["invalid_frames"] == 2
    assert status["last_invalid_frame"]["codes"] == ["not_object"]
    assert status["last_invalid_frame"]["bytes"] == 3
    assert status["state"] == "completed"


def test_garbage_during_session_is_recorded_raw_and_counted(rbackend, hub, frames):
    """A session keeps every frame exactly as received, garbage included, and its
    stored counters say how many were rejected."""
    gate, release = threading.Event(), threading.Event()
    garbage = [b"\xff\x00\x01\n", b"[1]\n", b"{\"a\":1,\"a\":2}\n", b"   \n",
               b'{"pad":"' + b"y" * 70_000 + b'"}\n']
    body = interleave(frames[1:80], garbage)
    script = [frames[0], ("gate", gate), *body, ("gate", release)]
    rt = rbackend.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True),
                              processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["records"] == 1)
    sess = rbackend.start_session("wearable", "garbage")
    gate.set()
    assert wait_until(lambda: rt.counters["frames"] == 1 + len(body))
    row = rbackend.stop_session(sess["session_id"])
    release.set()

    assert rt.error is None and rt.alive
    assert row["state"] == "completed"
    assert row["source"] == "simulated"
    assert row["counters"]["invalid_frames"] == len(garbage)
    assert row["counters"]["frames"] == len(body)
    assert row["counters"]["events"]["invalid_record"] == len(garbage)

    tel = Path(row["files"]["telemetry.ndjson"]["path"]).read_bytes().splitlines(keepends=True)
    assert len(tel) == len(body)
    assert tel == [b if b.endswith(b"\n") else b + b"\n" for b in body]


# --------------------------------------------------------------------------- runtime: contract fixtures


def _fixture_param(path: Path):
    return pytest.param(path, id=path.stem)


@pytest.mark.parametrize("path", [_fixture_param(p) for p in INVALID_FIXTURES])
def test_each_contract_invalid_fixture_is_rejected_and_ingestion_continues(rbackend, hub, frames, path):
    line = compact(path)
    k = first_index(frames, "emg_batch") + 1
    script = [*frames[:k], line, *frames[k:]]
    rt = run_to_end(rbackend, script)

    assert rt.error is None, rt.error
    assert rt.finished
    inv = hub.derived("stream_event", "invalid_record")
    assert len(inv) == 1
    assert MANIFEST[path.name]["code"] in inv[0]["payload"]["detail"]["codes"]
    # every valid frame after the bad one was still processed
    assert rt.proc.summary.records_accepted == len(frames)
    later = [m for m in hub.derived("stream_features") if m["hub_seq"] > inv[0]["hub_seq"]]
    assert {m["payload"]["stream"] for m in later} >= {"emg", "imu"}


def test_schema_invalid_objects_are_counted_as_invalid_frames(rbackend, hub, frames):
    """Frames that decode to a JSON object but violate the contract are rejected by
    the processor; the runtime's counters and last_invalid_frame must agree."""
    bad = [compact(p) for p in INVALID_FIXTURES
           if p.name != "missing_provenance.json"]  # that one crashes: tested separately
    script = interleave(frames, bad)
    rt = run_to_end(rbackend, script)
    assert rt.error is None
    assert rt.events["invalid_record"] == len(bad)
    st = rt.status()
    assert st["counters"]["invalid_frames"] == len(bad)
    assert st["counters"]["records"] == len(frames)
    assert st["last_invalid_frame"] is not None and st["last_invalid_frame"]["codes"]


def test_rejected_object_does_not_change_device_identity_or_session(rbackend, hub, frames):
    """A frame the contract rejects must not be trusted: it may not move the
    runtime's boot_id/provenance, join the session's boot list, or reach the
    telemetry channel as if it were a valid record."""
    gate, release = threading.Event(), threading.Event()
    k = first_index(frames, "emg_batch")
    forged = mutate(frames[k + 1], version=2, boot_id="forged-boot",
                    provenance={"source": "live"})
    script = [*frames[:k], ("gate", gate), forged, *frames[k:k + 1], ("gate", release)]
    rt = rbackend.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True),
                              processing_config_for("sim"))
    assert wait_until(lambda: rt.counters["frames"] == k)
    real_boot = rt.boot_id
    sess = rbackend.start_session("wearable", "forged")
    gate.set()
    assert wait_until(lambda: rt.counters["frames"] == k + 2)
    row = rbackend.stop_session(sess["session_id"])
    release.set()

    assert rt.error is None
    assert rt.boot_id == real_boot
    assert rt.provenance["source"] == "simulated"
    assert row["boot_ids"] == [real_boot]
    assert row["source"] == "simulated"
    tel = [m["payload"] for m in hub.snapshot() if m["channel"] in ("telemetry", "device")]
    assert all(p.get("boot_id") != "forged-boot" for p in tel)
    assert [d["last_boot_id"] for d in rbackend.store.devices()] == [real_boot]


def _crash_cases(frames: list[bytes]) -> dict[str, bytes]:
    k = first_index(frames, "emg_batch")
    return {
        "empty_object": b"{}\n",
        "missing_provenance": compact(FIXTURES / "invalid" / "missing_provenance.json"),
        "missing_seq_same_boot": mutate(frames[k], seq=None),
        "provenance_is_string": mutate(frames[k], provenance="simulated"),
        "device_id_is_list": mutate(frames[k], device_id=["sim", "wearable"]),
        "boot_id_is_object": mutate(frames[k], boot_id={"x": 1}),
    }


CRASH_REASON = "Schema-invalid JSON object crashes the runtime (unvalidated fields read in _on_line)"
CRASH_PARAMS = [
    pytest.param(c, id=c)
    for c in ("empty_object", "missing_provenance", "missing_seq_same_boot",
              "device_id_is_list", "boot_id_is_object")
] + [pytest.param("provenance_is_string", id="provenance_is_string")]


@pytest.mark.parametrize("case", CRASH_PARAMS)
def test_wrong_typed_or_missing_fields_do_not_kill_the_runtime(rbackend, hub, frames, case):
    bad = _crash_cases(frames)[case]
    k = first_index(frames, "emg_batch") + 1
    script = [*frames[:k], bad, *frames[k:]]
    rt = rbackend.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True),
                              processing_config_for("sim"))
    wait_until(lambda: rt.counters["frames"] == len(script) or rt.error is not None)
    assert rt.error is None, rt.error
    assert rt.alive
    assert rt.counters["frames"] == len(script)
    assert rt.events["invalid_record"] == 1


def test_deeply_nested_json_under_size_limit_does_not_kill_the_runtime(rbackend, hub, frames):
    bomb = b"[" * 30_000 + b"]" * 30_000 + b"\n"
    assert len(bomb) < MAX_LINE_BYTES
    script = [*frames[:5], bomb, *frames[5:]]
    rt = rbackend.add_runtime("wearable", ScriptedAdapter(script, clock="device", hold_open=True),
                              processing_config_for("sim"))
    wait_until(lambda: rt.counters["frames"] == len(script) or rt.error is not None)
    assert rt.error is None, rt.error
    assert rt.alive
    assert rt.counters["invalid_frames"] == 1
    assert rt.counters["records"] == len(frames)


# --------------------------------------------------------------------------- SerialAdapter framing


class FakeLink:
    """What is on the other end of the USB cable.

    ``connections`` is a queue of scripts, one per successful ``Serial()`` open;
    each script is a list of byte chunks (one per read) and exceptions (raised by
    the read that reaches them). With no script left, opening fails like an
    unplugged port. An exhausted script reads as silence.
    """

    def __init__(self, *connections: list):
        self.connections = deque(deque(c) for c in connections)
        self.opened: list[tuple[str, int]] = []
        self.ports: list["FakeSerial"] = []
        self.lock = threading.Lock()

    def install(self, monkeypatch):
        link = self

        class FakeSerial:
            def __init__(self, port, baud, timeout=None):
                with link.lock:
                    if not link.connections:
                        raise serial.SerialException(f"could not open port {port}")
                    self.script = link.connections.popleft()
                    link.opened.append((port, baud))
                    link.ports.append(self)
                self.closed = False
                self.timeout = timeout

            @property
            def in_waiting(self):
                with link.lock:
                    head = self.script[0] if self.script else None
                return len(head) if isinstance(head, (bytes, bytearray)) else 0

            def read(self, n=1):
                with link.lock:
                    if not self.script:
                        return b""
                    head = self.script[0]
                    if isinstance(head, BaseException):
                        self.script.popleft()
                        raise head
                    chunk, rest = bytes(head[:n]), head[n:]
                    if rest:
                        self.script[0] = rest
                    else:
                        self.script.popleft()
                    return chunk

            def close(self):
                self.closed = True

        monkeypatch.setattr(serial, "Serial", FakeSerial)
        return self


def chunked(data: bytes, seed: int, lo: int = 1, hi: int = 300) -> list[bytes]:
    rng = random.Random(seed)
    out, i = [], 0
    while i < len(data):
        n = rng.randint(lo, hi)
        out.append(data[i:i + n])
        i += n
    return out


def read_all(ad: SerialAdapter, max_reads: int = 10_000, timeout: float = 0.02) -> list[bytes]:
    """Read lines until the adapter reports silence."""
    out = []
    for _ in range(max_reads):
        line = ad.read_line(timeout)
        if line is None:
            return out
        out.append(line)
    raise AssertionError("adapter never went quiet")


GOOD = [b'{"n":1}\n', b'{"n":2}\n', b'{"n":3}\n']


def test_serial_reassembles_lines_split_across_reads(monkeypatch):
    stream = b"partial-from-before-connect" + b"\n" + b"".join(GOOD)
    FakeLink(chunked(stream, seed=1, lo=1, hi=4)).install(monkeypatch)
    ad = SerialAdapter("/dev/fake0", baud=115200)
    ad.open()
    assert read_all(ad) == GOOD  # the leftover tail of an earlier frame is dropped (and counted)
    assert ad.partial_discarded == 1


def test_serial_line_spanning_many_reads_waits_for_newline(monkeypatch):
    FakeLink([b"junk\n", b'{"n":', b"1", b"}"]).install(monkeypatch)
    ad = SerialAdapter("/dev/fake0")
    ad.open()
    assert ad.read_line(0.02) is None  # incomplete: nothing yet, nothing lost
    assert ad.read_line(0.02) is None


def test_serial_buffered_partial_completes_on_later_read(monkeypatch):
    link = FakeLink([b"junk\n", b'{"n":', b"1"])
    link.install(monkeypatch)
    ad = SerialAdapter("/dev/fake0")
    ad.open()
    assert ad.read_line(0.02) is None
    with link.lock:
        link.ports[0].script.extend([b"}\n", b'{"n":2}\n'])
    assert read_all(ad) == [b'{"n":1}\n', b'{"n":2}\n']


def test_serial_first_line_after_connect_is_kept_when_it_can_be_a_frame(monkeypatch):
    """Firmware may send its hello the moment the port opens: a first line that starts like a frame
    is kept (a damaged one is rejected later by the validator as a visible invalid_record); only a
    first line that cannot be a frame is dropped and counted."""
    FakeLink([b"".join(GOOD)]).install(monkeypatch)
    ad = SerialAdapter("/dev/fake0")
    ad.open()
    assert read_all(ad) == GOOD and ad.partial_discarded == 0


def test_serial_oversized_line_is_passed_on_truncated_then_resyncs(monkeypatch):
    good = b'{"n":9}\n'
    over = [b"x" * 4096] * 18  # 73 728 bytes, no newline yet
    FakeLink([b"sync\n", *over, b"tail-of-the-long-line\n", good]).install(monkeypatch)
    ad = SerialAdapter("/dev/fake0")
    ad.open()
    lines = read_all(ad)
    assert len(lines) == 2
    long_line, nxt = lines
    assert len(long_line) == MAX_LINE_BYTES + 1  # bounded memory: passed on truncated
    rec, issues = contract.parse_line(long_line)
    assert rec is None and [i.code for i in issues] == ["line_too_long"]
    assert nxt == good  # the rest of the long line was dropped up to its newline


def test_serial_very_long_line_yields_only_one_rejected_frame(monkeypatch):
    """Several MAX_LINE_BYTES of junk without a newline must not turn into several frames."""
    good = b'{"n":7}\n'
    junk = [b"z" * 8192] * 30  # ~245 KB
    FakeLink([b"sync\n", *junk, b"\n", good]).install(monkeypatch)
    ad = SerialAdapter("/dev/fake0")
    ad.open()
    lines = read_all(ad)
    assert len(lines) == 2
    assert contract.parse_line(lines[0])[1][0].code == "line_too_long"
    assert lines[1] == good


def test_serial_oversized_line_arriving_whole_is_rejected_and_next_line_kept(monkeypatch):
    good = b'{"n":5}\n'
    FakeLink([b"sync\n", b"q" * 70_000 + b"\n" + good]).install(monkeypatch)
    ad = SerialAdapter("/dev/fake0")
    ad.open()
    lines = read_all(ad)
    assert len(lines) == 2
    rec, issues = contract.parse_line(lines[0])
    assert rec is None and issues[0].code == "line_too_long"
    assert lines[1] == good


@pytest.mark.parametrize("exc", [serial.SerialException("device reports readiness to read but returned no data"),
                                 OSError(6, "Device not configured")],
                         ids=["SerialException", "OSError"])
def test_serial_read_error_becomes_adapter_disconnected(monkeypatch, exc):
    link = FakeLink([b"sync\n" + b"".join(GOOD), exc])
    link.install(monkeypatch)
    ad = SerialAdapter("/dev/fake0")
    ad.open()
    # lines that arrived before the failure are still delivered
    assert ad.read_line(0.05) == GOOD[0]
    assert ad.read_line(0.05) == GOOD[1]
    assert ad.read_line(0.05) == GOOD[2]
    with pytest.raises(AdapterDisconnected):
        ad.read_line(0.05)
    assert link.ports[0].closed
    with pytest.raises(AdapterDisconnected):  # stays disconnected until reopened
        ad.read_line(0.05)


def test_serial_open_failure_is_adapter_disconnected(monkeypatch):
    FakeLink().install(monkeypatch)  # nothing plugged in
    ad = SerialAdapter("/dev/fake0")
    with pytest.raises(AdapterDisconnected):
        ad.open()


def test_serial_glob_port_without_match_is_adapter_disconnected(monkeypatch, tmp_path):
    FakeLink([b""]).install(monkeypatch)
    ad = SerialAdapter(str(tmp_path / "cu.usbmodem*"))
    with pytest.raises(AdapterDisconnected):
        ad.open()


def test_serial_reconnect_drops_stale_partial_and_resyncs(monkeypatch):
    """Half a line buffered when the cable was pulled must not be glued onto the
    first bytes of the next connection."""
    exc = serial.SerialException("unplugged")
    link = FakeLink([b"sync\n", GOOD[0], b'{"n":', exc],
                    [b'2}\n', GOOD[2]])
    link.install(monkeypatch)
    ad = SerialAdapter("/dev/fake0")
    ad.open()
    assert ad.read_line(0.05) == GOOD[0]
    with pytest.raises(AdapterDisconnected):
        while True:
            assert ad.read_line(0.05) is None
    ad.open()
    # b'2}\n' is the first (partial) line of the new connection: discarded,
    # never combined with the stale b'{"n":' into a valid-looking frame
    assert read_all(ad) == [GOOD[2]]
    assert len(link.opened) == 2


def test_runtime_over_serial_survives_garbage_and_unplug(monkeypatch, rbackend, hub, frames):
    """End to end over the real SerialAdapter: arbitrary chunking, garbage lines,
    a read failure mid-line, reconnection. Disconnect is visible; counts are exact."""
    cut = 50
    garbage = [b"#boot banner v0.1\n", b"\xff\xfe\xfd\n", b"[]\n", b"\n"]
    conn1 = (b"noise from before the host attached\n" + b"".join(frames[:cut // 2])
             + b"".join(garbage) + b"".join(frames[cut // 2:cut])
             + frames[cut][:40])  # half a frame when the cable is pulled
    conn2 = b"rest of a line\n" + b"".join(frames[cut:])
    link = FakeLink([*chunked(conn1, seed=3, hi=700), serial.SerialException("unplugged")],
                    chunked(conn2, seed=4, hi=700))
    link.install(monkeypatch)

    rt = rbackend.add_runtime("wearable", SerialAdapter("/dev/fake0"), processing_config_for("serial"))
    assert wait_until(lambda: rt.counters["records"] == len(frames) or rt.error is not None, timeout=15)
    assert rt.error is None and rt.alive
    st = rt.status()
    assert st["counters"]["records"] == len(frames)
    assert st["counters"]["invalid_frames"] == len(garbage)
    assert st["counters"]["frames"] == len(frames) + len(garbage)
    assert st["counters"]["connection_disconnected"] == 1
    assert st["counters"]["connection_connected"] == 2
    assert st["connection"] == "connected"
    conn_events = [m["payload"]["connection"] for m in hub.snapshot()
                   if m["channel"] == "runtime" and m["payload"].get("event") == "device_connection"]
    assert conn_events == ["connected", "disconnected", "connected"]
    assert st["adapter"]["kind"] == "serial" and st["adapter"]["port"] == "/dev/fake0"
    assert {"emg", "imu"} <= {m["payload"]["stream"] for m in hub.derived("stream_features")}
