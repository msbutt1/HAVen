"""Provenance is preserved end to end: device session -> store row -> session files
-> replay runtime -> replayed derived output -> history API.

Everything runs offline on SIMULATED frames. "Live" streams are simulator frames
whose provenance was rewritten to ``{"source": "live"}`` and re-validated against
the contract, so the backend sees exactly what real firmware would send.

The original sessions are recorded with a device-clock adapter and gated so the
session starts before the first frame: the session's processor state then begins
from zero, exactly like a replay, and the two derived streams are comparable
record for record (apart from provenance and the provenance-dependent
calibration_id, the matching rule of signals' ``vg-demo replay``).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import ScriptedAdapter, sim_frames, wait_until
from vibeguard_signals import contract
from vibeguard_signals.demo import CALIBRATION_SCHEDULE
from vibeguard_signals.processing import to_ndjson_line
from vibeguard_backend.app import create_app
from vibeguard_backend.history import seed_history
from vibeguard_backend.hub import Hub
from vibeguard_backend.service import Backend, processing_config_for

DURATION_S = 30.0  # the demo timeline: calibration captures need ~8 s of device time


# --------------------------------------------------------------------------- helpers

class CapturingHub(Hub):
    """A real Hub that also keeps a copy of everything published (what a WS client
    with an unbounded queue would see)."""

    def __init__(self) -> None:
        super().__init__()
        self._cap_lock = threading.Lock()
        self.messages: list[dict] = []

    def publish(self, channel, runtime, payload):
        msg = super().publish(channel, runtime, payload)
        # snapshot the payload as it was serialised at publish time
        snap = json.loads(json.dumps(payload, default=str)) if channel != "derived" else \
            json.loads(to_ndjson_line(payload))
        with self._cap_lock:
            self.messages.append({**msg, "payload": snap})
        return msg

    def derived(self, runtime: str) -> list[dict]:
        with self._cap_lock:
            return [m["payload"] for m in self.messages
                    if m["channel"] == "derived" and m["runtime"] == runtime]

    def channel(self, *channels: str, runtime: str | None = None) -> list[dict]:
        """Payloads on any of ``channels``, in publish (hub_seq) order."""
        with self._cap_lock:
            msgs = sorted(self.messages, key=lambda m: m["hub_seq"])
        return [m["payload"] for m in msgs
                if m["channel"] in channels and (runtime is None or m["runtime"] == runtime)]


@pytest.fixture
def cbackend(tmp_path: Path):
    b = Backend(tmp_path / "data", hub=CapturingHub())
    yield b
    b.shutdown()


def _first_sample_s(rec: dict) -> float | None:
    t = rec.get("timing")
    if not t:
        return None
    return (t["t0_us"] if t["mode"] == "uniform" else t["t_us"][0]) / 1e6


def calibration_points(frames: list[bytes]) -> list[tuple[int, str]]:
    """Index of the frame after which each demo calibration command is issued
    (same rule as signals' demo: just before the first batch at/after the time)."""
    recs = [contract.parse_line(f)[0] if not contract.parse_line(f)[1] else None for f in frames]
    points = []
    for t_s, action in CALIBRATION_SCHEDULE:
        after = None
        for i, r in enumerate(recs):
            if r is None:  # malformed frame: commands follow the last valid record
                continue
            first = _first_sample_s(r)
            if first is not None and first >= t_s:
                break
            after = i
        assert after is not None
        points.append((after, action))
    return points


def with_provenance(frames: list[bytes], prov: dict) -> list[bytes]:
    out = []
    for f in frames:
        rec = json.loads(f)
        rec["provenance"] = dict(prov)
        assert contract.validate_record(rec) == [], "rewritten frame must stay contract-valid"
        out.append(contract.serialize_record(rec).encode())
    return out


def record_session(backend: Backend, frames: list[bytes], name: str = "wearable",
                   calibrate: bool = True, label: str | None = None,
                   mid_session_check=None) -> dict:
    """Run ``frames`` through a device-clock runtime with a session that starts before
    the first frame, issue the demo calibration commands through the public API at the
    demo's points, and return the completed session row."""
    points = calibration_points(frames) if calibrate else []
    gates = [threading.Event() for _ in range(len(points) + 1)]
    script: list = [("gate", gates[0])]
    prev = 0
    for gi, (idx, _) in enumerate(points, start=1):
        script += frames[prev:idx + 1]
        script.append(("gate", gates[gi]))
        prev = idx + 1
    script += frames[prev:]
    rt = backend.add_runtime(name, ScriptedAdapter(script, clock="device"),
                             processing_config_for("sim"))
    assert wait_until(lambda: rt.connection == "connected")
    row = backend.start_session(name, label)
    gates[0].set()
    for gi, (idx, action) in enumerate(points, start=1):
        n_valid = sum(1 for f in frames[:idx + 1] if contract.parse_line(f)[1] == [])
        assert wait_until(lambda: rt.counters["records"] == n_valid), rt.status()
        if mid_session_check is not None and gi == 1:
            mid_session_check(row["session_id"])
        backend.calibrate(name, action)
        gates[gi].set()
    sid = row["session_id"]
    # ``finished`` flips just before the session is closed: wait for the row itself
    assert wait_until(lambda: rt.finished and backend.session(sid)["state"] != "recording",
                      timeout=20), rt.status()
    row = backend.session(sid)
    assert row["state"] == "completed", row
    return row


def read_ndjson(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def strip_provenance(o):
    """signals' demo replay matching rule: ignore provenance and calibration_id values."""
    if isinstance(o, dict):
        return {k: ("<calibration_id>" if k == "calibration_id" and v else strip_provenance(v))
                for k, v in o.items() if k != "provenance"}
    if isinstance(o, list):
        return [strip_provenance(x) for x in o]
    return o


def final_score(derived: list[dict]) -> dict:
    scores = [o for o in derived if o["type"] == "experimental_score"]
    assert scores, "no experimental_score records"
    assert scores[-1].get("trigger") == "end_of_input", scores[-1]
    return scores[-1]


def run_replay(backend: Backend, session_id: str, **kw) -> tuple:
    status = backend.start_replay(session_id, **kw)
    rt = backend.runtime(status["runtime"])
    assert wait_until(lambda: rt.finished, timeout=20), rt.status()
    return rt, backend.hub.derived(rt.name)  # type: ignore[attr-defined]


def first_diff(a: list, b: list) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"first difference at #{i}:\n  original={json.dumps(x)[:400]}\n  replay  ={json.dumps(y)[:400]}"
    return f"lengths differ: original={len(a)} replay={len(b)}"


# --------------------------------------------------------------------------- (1) simulated session

def test_simulated_session_row_and_files_carry_simulated_provenance(cbackend):
    frames = sim_frames(duration_s=DURATION_S, run_id="prov1")
    row = record_session(cbackend, frames, label="sim provenance")

    assert row["source"] == "simulated"
    assert row["original_source"] is None
    assert row["recording_id"] == "sim-prov1"
    assert row["simulated_history"] is False
    assert row["counters"].get("provenance_changes", 0) == 0

    d = Path(row["dir"])
    # the raw recording is the device stream exactly as received
    assert (d / "telemetry.ndjson").read_bytes() == b"".join(frames)
    assert row["files"]["telemetry.ndjson"]["bytes"] == sum(len(f) for f in frames)

    derived = read_ndjson(d / "derived.ndjson")
    assert len(derived) == row["counters"]["derived_records"] > 100
    provs = {json.dumps(o["provenance"], sort_keys=True) for o in derived if "provenance" in o}
    assert provs == {json.dumps({"source": "simulated", "recording_id": "sim-prov1"}, sort_keys=True)}
    assert all(o["provenance"]["source"] == "simulated" for o in derived if o.get("provenance"))

    # the calibration commands issued through the API are recorded with their stream position
    cmds = read_ndjson(d / "commands.ndjson")
    assert [c["action"] for c in cmds] == ["relaxed", "squeeze"]
    recs = [json.loads(f) for f in frames]
    for c, (idx, _) in zip(cmds, calibration_points(frames)):
        assert (c["after_boot_id"], c["after_seq"]) == (recs[idx]["boot_id"], recs[idx]["seq"])

    # the processor actually calibrated and scored (otherwise the replay check is vacuous)
    assert row["calibration_id"]
    assert row["score"]["status"] == "ok", row["score"]
    assert row["score"]["session_au"] > 0

    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["session"]["source"] == "simulated"
    assert manifest["session"]["recording_id"] == "sim-prov1"

    # the device registry and stored calibration remember the source too
    dev = {x["device_id"]: x for x in cbackend.store.devices()}[recs[0]["device_id"]]
    assert dev["last_source"] == "simulated"
    cals = cbackend.store.calibrations(recs[0]["device_id"])
    assert cals and all(c["source"] == "simulated" for c in cals)


def test_session_started_before_first_frame_is_labelled_while_recording(cbackend):
    """A session row must carry its provenance as soon as data has arrived, not only
    when it is stopped: the dashboard lists and filters recording sessions, and a
    crash during the session must not leave an unlabelled row behind."""
    frames = sim_frames(duration_s=DURATION_S, run_id="prov-mid")
    seen = {}

    def check(sid: str) -> None:
        seen["row"] = cbackend.session(sid)
        seen["filtered"] = [r["session_id"] for r in cbackend.store.wearable_sessions(source="simulated")]

    record_session(cbackend, frames, mid_session_check=check)
    mid = seen["row"]
    assert mid["state"] == "recording"
    assert mid["source"] == "simulated", f"recording session row has source={mid['source']!r}"
    assert mid["recording_id"] == "sim-prov-mid"
    assert mid["session_id"] in seen["filtered"]


def test_session_cut_off_by_crash_keeps_its_provenance_after_restart(tmp_path):
    """Simulate a kill during a session: a second backend process opens the same data
    directory while the first is still mid-session (the on-disk state a crash leaves)."""
    data = tmp_path / "data"
    first = Backend(data)
    try:
        gate = threading.Event()
        frames = with_provenance(sim_frames(duration_s=3, run_id="crash"), {"source": "live"})
        rt = first.add_runtime("wearable", ScriptedAdapter([("gate", gate), *frames[:20],
                                                            ("gate", threading.Event())],
                                                           clock="device"),
                               processing_config_for("serial"))
        assert wait_until(lambda: rt.connection == "connected")
        sid = first.start_session("wearable", "will crash")["session_id"]
        gate.set()
        assert wait_until(lambda: rt.counters["records"] == 20)

        second = Backend(data)  # "restarted" process
        try:
            assert second.recovered["wearable_sessions_interrupted"] == 1
            row = second.session(sid)
            assert row["state"] == "interrupted" and row["end_reason"] == "backend_restart"
            assert row["source"] == "live", f"interrupted live session has source={row['source']!r}"
            assert row["recording_id"] == f"rec-{sid}"
        finally:
            second.shutdown()
    finally:
        first.shutdown()


# --------------------------------------------------------------------------- (2) replay of it

@pytest.fixture
def recorded_sim(cbackend):
    frames = sim_frames(duration_s=DURATION_S, run_id="prov2")
    row = record_session(cbackend, frames, label="original")
    return cbackend, row, frames


def test_fast_replay_is_labelled_replay_and_reproduces_the_session(recorded_sim):
    backend, row, frames = recorded_sim
    original = read_ndjson(Path(row["dir"]) / "derived.ndjson")

    rt, derived = run_replay(backend, row["session_id"], fast=True)

    # runtime-level provenance as the dashboard sees it
    st = rt.status()
    assert st["state"] == "completed"
    assert st["role"] == "replay"
    assert {k: st["provenance"].get(k) for k in ("source", "original_source", "recording_id")} == \
        {"source": "replay", "original_source": "simulated", "recording_id": "sim-prov2"}
    assert st["meta"]["replay_of_session"] == row["session_id"]
    assert st["meta"]["original_source"] == "simulated"
    assert st["meta"]["recording_id"] == "sim-prov2"
    assert st["adapter"]["kind"] == "replay" and st["adapter"]["skipped_frames"] == 0

    # every derived output is labelled replay, none claims to be simulated or live
    labelled = [o for o in derived if "provenance" in o]
    assert len(labelled) == len(derived)
    assert {json.dumps(o["provenance"], sort_keys=True) for o in labelled} == {json.dumps(
        {"source": "replay", "original_source": "simulated", "recording_id": "sim-prov2"}, sort_keys=True)}

    # the raw telemetry re-emitted on the telemetry/device channels is labelled replay too
    raw = backend.hub.channel("telemetry", "device", runtime=rt.name)
    assert len(raw) == len(frames)
    assert {r["provenance"]["source"] for r in raw} == {"replay"}
    # and only provenance changed: device fields are untouched
    for sent, got in zip([json.loads(f) for f in frames], raw):
        assert {k: v for k, v in sent.items() if k != "provenance"} == \
            {k: v for k, v in got.items() if k != "provenance"}

    # calibration was reissued at the recorded points, so the output is identical
    assert len(derived) == len(original)
    assert strip_provenance(derived) == strip_provenance(original), \
        first_diff(strip_provenance(original), strip_provenance(derived))
    assert final_score(derived)["cumulative"] == final_score(original)["cumulative"]
    assert final_score(derived)["cumulative"]["value"] > 0
    assert rt.counters.get("calibration_restored", 0) == 0  # replay never borrows a stored calibration


@pytest.fixture
def paced_replay(recorded_sim):
    backend, row, _ = recorded_sim
    original = read_ndjson(Path(row["dir"]) / "derived.ndjson")
    rt, derived = run_replay(backend, row["session_id"], speed=20.0)
    return rt, derived, original


def test_paced_replay_is_labelled_replay(paced_replay):
    rt, derived, original = paced_replay
    st = rt.status()
    assert st["adapter"]["fast"] is False and st["adapter"]["speed"] == 20.0
    assert st["state"] == "completed"
    assert {k: st["provenance"].get(k) for k in ("source", "original_source", "recording_id")} == \
        {"source": "replay", "original_source": "simulated", "recording_id": "sim-prov2"}
    labelled = [o for o in derived if "provenance" in o]
    assert labelled and len(labelled) == len(derived)
    assert {(o["provenance"]["source"], o["provenance"]["original_source"], o["provenance"]["recording_id"])
            for o in labelled} == {("replay", "simulated", "sim-prov2")}
    # device time drives the score, so pacing never changes the accumulated total
    assert final_score(derived)["cumulative"] == final_score(original)["cumulative"]


def test_paced_replay_reproduces_the_session(paced_replay):
    _, derived, original = paced_replay
    # pacing changes when records are delivered, never what is derived from them
    assert strip_provenance(derived) == strip_provenance(original), \
        first_diff(strip_provenance(original), strip_provenance(derived))


def test_replay_recalibrates_from_commands_not_from_stored_calibration(recorded_sim):
    """Without the recorded commands the replay would stay uncalibrated: the match
    above depends on the commands, and the replay leaves the original's stored
    calibration for the simulated device untouched."""
    backend, row, _ = recorded_sim
    dev = row["device_id"]
    before = backend.store.latest_calibration(dev, "simulated")
    assert before is not None

    rt, derived = run_replay(backend, row["session_id"], fast=True)

    events = [o for o in derived if o["type"] == "stream_event" and o["event"] == "calibration_state"]
    assert any(e["detail"].get("state") == "calibrated" for e in events), events[-3:]
    assert backend.store.latest_calibration(dev, "simulated")["calibration_id"] == before["calibration_id"]
    replay_cals = backend.store.latest_calibration(dev, "replay")
    assert replay_cals is not None
    assert replay_cals["record"]["provenance"]["source"] == "replay"


def test_replay_survives_backend_restart_with_provenance(tmp_path):
    data = tmp_path / "data"
    first = Backend(data, hub=CapturingHub())
    frames = sim_frames(duration_s=DURATION_S, run_id="prov-rs")
    try:
        row = record_session(first, frames)
    finally:
        first.shutdown()

    second = Backend(data, hub=CapturingHub())
    try:
        again = second.session(row["session_id"])
        assert again["state"] == "completed"
        assert (again["source"], again["recording_id"]) == ("simulated", "sim-prov-rs")
        assert second.recovered["wearable_sessions_interrupted"] == 0
        rt, derived = run_replay(second, row["session_id"], fast=True)
        assert rt.provenance["original_source"] == "simulated"
        original = read_ndjson(Path(row["dir"]) / "derived.ndjson")
        assert strip_provenance(derived) == strip_provenance(original), \
            first_diff(strip_provenance(original), strip_provenance(derived))
    finally:
        second.shutdown()


def test_session_recorded_on_a_replay_runtime_keeps_original_source(recorded_sim):
    backend, row, _ = recorded_sim
    status = backend.start_replay(row["session_id"], speed=10.0)
    rt = backend.runtime(status["runtime"])
    assert wait_until(lambda: rt.counters["records"] > 5)
    rs = backend.start_session(rt.name, "replayed")
    assert wait_until(lambda: rt.finished and backend.session(rs["session_id"])["state"] != "recording",
                      timeout=20)
    rrow = backend.session(rs["session_id"])

    assert rrow["source"] == "replay"
    assert rrow["original_source"] == "simulated"
    assert rrow["recording_id"] == "sim-prov2"
    assert rrow["state"] == "completed"
    derived = read_ndjson(Path(rrow["dir"]) / "derived.ndjson")
    assert derived and all(o["provenance"]["source"] == "replay" for o in derived if "provenance" in o)
    tel = [json.loads(line) for line in Path(rrow["dir"], "telemetry.ndjson").read_bytes().splitlines()]
    assert {r["provenance"]["source"] for r in tel} == {"replay"}

    # replaying the replay: still traced back to the simulated original
    rt2, derived2 = run_replay(backend, rrow["session_id"], fast=True)
    assert rt2.provenance["source"] == "replay"
    assert rt2.provenance["original_source"] == "simulated"
    assert rt2.provenance["recording_id"] == "sim-prov2"
    assert rt2.meta["original_source"] == "simulated"

    hist = backend.history(source="replay")["items"]
    assert [h["session_id"] for h in hist if h["kind"] == "wearable"] == [rrow["session_id"]]


def test_replay_skips_malformed_frames_but_keeps_them_in_the_recording(cbackend):
    frames = sim_frames(duration_s=DURATION_S, run_id="prov-bad")
    bad = [b'{"schema":"vibeguard.device","truncated\n', b"\xff\xfe not json at all\n"]
    mixed = frames[:300] + [bad[0]] + frames[300:600] + [bad[1]] + frames[600:]
    row = record_session(cbackend, mixed)

    d = Path(row["dir"])
    assert (d / "telemetry.ndjson").read_bytes() == b"".join(mixed)
    assert row["counters"]["invalid_frames"] == 2
    assert row["source"] == "simulated"  # malformed frames do not disturb provenance

    rt, derived = run_replay(cbackend, row["session_id"], fast=True)
    assert rt.status()["adapter"]["skipped_frames"] == 2
    assert rt.provenance["source"] == "replay"
    original = read_ndjson(d / "derived.ndjson")
    invalid = [o for o in original if o["type"] == "stream_event" and o["event"] == "invalid_record"]
    assert len(invalid) == 2
    without_invalid = [o for o in original if o not in invalid]
    assert strip_provenance(derived) == strip_provenance(without_invalid), \
        first_diff(strip_provenance(without_invalid), strip_provenance(derived))


# --------------------------------------------------------------------------- (3) live stream

def test_live_stream_session_and_its_replay(cbackend):
    frames = with_provenance(sim_frames(duration_s=DURATION_S, run_id="prov3"), {"source": "live"})
    row = record_session(cbackend, frames, label="live")
    sid = row["session_id"]

    assert row["source"] == "live"
    assert row["original_source"] is None
    assert row["recording_id"] == f"rec-{sid}"
    assert row["simulated_history"] is False
    assert (Path(row["dir"]) / "telemetry.ndjson").read_bytes() == b"".join(frames)
    original = read_ndjson(Path(row["dir"]) / "derived.ndjson")
    assert {o["provenance"]["source"] for o in original if "provenance" in o} == {"live"}
    assert json.loads((Path(row["dir"]) / "manifest.json").read_text())["session"]["recording_id"] == f"rec-{sid}"

    rt, derived = run_replay(cbackend, sid, fast=True)
    assert {k: rt.provenance.get(k) for k in ("source", "original_source", "recording_id")} == \
        {"source": "replay", "original_source": "live", "recording_id": f"rec-{sid}"}
    assert {json.dumps(o["provenance"], sort_keys=True) for o in derived if "provenance" in o} == \
        {json.dumps({"source": "replay", "original_source": "live", "recording_id": f"rec-{sid}"},
                    sort_keys=True)}
    assert rt.meta["original_source"] == "live" and rt.meta["recording_id"] == f"rec-{sid}"
    assert strip_provenance(derived) == strip_provenance(original), \
        first_diff(strip_provenance(original), strip_provenance(derived))

    # live and simulated sessions never share a filter bucket
    assert [r["session_id"] for r in cbackend.store.wearable_sessions(source="live")] == [sid]
    assert cbackend.store.wearable_sessions(source="simulated") == []


def test_live_calibration_is_not_restored_into_a_simulated_stream(cbackend):
    """A calibration made on live data must not be applied to simulated data from
    the same device id (and vice versa): restores are keyed by source."""
    live = with_provenance(sim_frames(duration_s=DURATION_S, run_id="cal-live"), {"source": "live"})
    record_session(cbackend, live, name="a")
    sim = sim_frames(duration_s=3, run_id="cal-sim")
    rt = cbackend.add_runtime("b", ScriptedAdapter(sim, clock="device"), processing_config_for("sim"))
    assert wait_until(lambda: rt.finished)
    assert rt.counters.get("calibration_restored", 0) == 0
    assert rt.calibration_state == "uncalibrated"


# --------------------------------------------------------------------------- (4) mixing

def test_provenance_switch_mid_session_is_surfaced_and_voids_the_session_score(cbackend):
    frames = sim_frames(duration_s=DURATION_S, run_id="mix")
    cut = int(len(frames) * 0.8)  # well after calibration and scored vibration
    stream = frames[:cut] + with_provenance(frames[cut:], {"source": "live"})
    row = record_session(cbackend, stream, label="mixed")

    assert row["counters"].get("provenance_changes", 0) > 0
    assert row["counters"]["events"]["provenance_changed"] == 1
    assert cbackend.runtime("wearable").events["provenance_changed"] == 1
    score = row["score"]
    assert score["status"] == "unavailable"
    assert score["reason"] == "score_accumulation_reset_during_session"
    assert score["session_au"] is None
    assert score["at_end"]["accumulation"] != (score["at_start"] or {}).get("accumulation", 1)

    derived = read_ndjson(Path(row["dir"]) / "derived.ndjson")
    resets = [o for o in derived if o["type"] == "stream_event" and o["event"] == "score_accumulation_reset"]
    assert len(resets) == 1 and resets[0]["detail"]["reason"] == "provenance_changed"
    srcs = [o["provenance"]["source"] for o in derived if "provenance" in o]
    # simulated first, then live, never interleaved back
    assert srcs[0] == "simulated" and srcs[-1] == "live"
    assert srcs == sorted(srcs, key=lambda s: s == "live")


def test_session_provenance_changes_counts_changes_not_records(cbackend):
    """simulated -> live -> simulated is two provenance changes, whatever the number
    of records in each stretch."""
    frames = sim_frames(duration_s=DURATION_S, run_id="mix2")
    a, b = int(len(frames) * 0.5), int(len(frames) * 0.7)
    stream = frames[:a] + with_provenance(frames[a:b], {"source": "live"}) + frames[b:]
    row = record_session(cbackend, stream, label="mixed twice")

    assert row["counters"]["events"]["provenance_changed"] == 2
    assert row["score"]["status"] == "unavailable"
    assert row["counters"]["provenance_changes"] == 2, (
        f"provenance_changes={row['counters']['provenance_changes']} for 2 changes "
        f"({b - a} live records)")


def test_provenance_switch_at_a_reboot_still_voids_the_session_score(cbackend):
    """The device reboots and comes back with different provenance (e.g. the
    simulator was swapped for the real board under the same device id). The session
    then mixes simulated and live data; its score must not be reported as one sum."""
    sim = sim_frames(duration_s=DURATION_S, run_id="boot-a")
    live = with_provenance(sim_frames(duration_s=DURATION_S, run_id="boot-b"), {"source": "live"})
    assert json.loads(sim[0])["boot_id"] != json.loads(live[0])["boot_id"]
    row = record_session(cbackend, sim + live, label="reboot mix")

    assert len(row["boot_ids"]) == 2
    assert row["counters"].get("provenance_changes", 0) >= 1  # the backend did notice
    score = row["score"]
    assert score["status"] != "ok", (
        f"session mixing simulated and live data reports one summed score: "
        f"session_au={score['session_au']} accumulation start/end="
        f"{(score['at_start'] or {}).get('accumulation')}/{score['at_end']['accumulation']}")


# --------------------------------------------------------------------------- (5) history

def test_seeded_history_is_simulated_everywhere(cbackend):
    created = seed_history(cbackend, days=2)
    assert len(created["wearable_sessions"]) == 2 and len(created["station_sessions"]) == 2

    for sid in created["wearable_sessions"]:
        row = cbackend.session(sid)
        assert row["source"] == "simulated" and row["simulated_history"] is True
        assert row["recording_id"].startswith("sim-")
        assert "SIMULATED" in row["label"]
        tel = [contract.parse_line(line) for line in
               (Path(row["dir"]) / "telemetry.ndjson").read_bytes().splitlines(keepends=True)]
        assert all(issues == [] for _, issues in tel)
        assert {rec["provenance"]["source"] for rec, _ in tel} == {"simulated"}
        assert {rec["provenance"]["recording_id"] for rec, _ in tel} == {row["recording_id"]}
        derived = read_ndjson(Path(row["dir"]) / "derived.ndjson")
        assert {o["provenance"]["source"] for o in derived if "provenance" in o} == {"simulated"}
        manifest = json.loads((Path(row["dir"]) / "manifest.json").read_text())
        assert manifest["simulated_history"] is True
    for ssid in created["station_sessions"]:
        st = cbackend.station_session(ssid)
        assert st["source"] == "simulated" and st["simulated_history"] is True

    client = TestClient(create_app(cbackend))
    body = client.get("/api/history").json()
    assert len(body["items"]) == 4
    for item in body["items"]:
        assert item["source"] == "simulated", item
        assert item["simulated_history"] is True, item
    assert "SIMULATED" in body["labels"]["simulated"]
    assert "REPLAY" in body["labels"]["replay"]
    assert client.get("/api/history", params={"source": "live"}).json()["items"] == []
    assert len(client.get("/api/history", params={"source": "simulated"}).json()["items"]) == 4
    assert all(s["source"] == "simulated" for s in client.get("/api/sessions").json())


def test_seeded_history_derived_output_is_a_single_finished_stream(cbackend):
    created = seed_history(cbackend, days=1)
    row = cbackend.session(created["wearable_sessions"][0])
    derived = read_ndjson(Path(row["dir"]) / "derived.ndjson")
    ends = [o for o in derived if o["type"] == "stream_event" and o["event"] == "end_of_input"]
    finals = [o for o in derived if o["type"] == "experimental_score" and o.get("trigger") == "end_of_input"]
    assert len(ends) == 1, f"derived.ndjson has {len(ends)} end_of_input events"
    assert len(finals) == 1, f"derived.ndjson has {len(finals)} final scores"
    assert row["counters"]["derived_records"] == len(derived)


def test_seeded_history_session_replays_to_the_same_output(cbackend):
    created = seed_history(cbackend, days=1)
    row = cbackend.session(created["wearable_sessions"][0])
    original = read_ndjson(Path(row["dir"]) / "derived.ndjson")

    rt, derived = run_replay(cbackend, row["session_id"], fast=True)
    assert rt.provenance["source"] == "replay"
    assert rt.provenance["original_source"] == "simulated"
    assert rt.provenance["recording_id"] == row["recording_id"]
    assert final_score(derived)["cumulative"] == final_score(original)["cumulative"]
    assert strip_provenance(derived) == strip_provenance(original), \
        first_diff(strip_provenance(original), strip_provenance(derived))
