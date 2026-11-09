"""Acceptance: sequence gaps and device disconnects are surfaced, never hidden.

Ground truth for "how many records were lost" comes from the simulator's own
transport model (``SimRecord.lost``), not from the backend, so these tests check
the backend's counts against an independent source. Everything is SIMULATED and
offline.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from conftest import ScriptedAdapter, SimAdapter, sim_frames, wait_until
from haven_signals.simulator import SimConfig, Simulation
from haven_backend.runtime import RuntimeConflict

STALE_DEADLINE_S = 0.5  # a silent stream must be visibly stale within this


# ---------------------------------------------------------------- helpers


@pytest.fixture
def published(backend, monkeypatch):
    """Every hub message, with the host monotonic time it was published."""
    msgs: list[tuple[float, dict]] = []
    lock = threading.Lock()
    original = backend.hub.publish

    def record(channel, runtime, payload):
        msg = original(channel, runtime, payload)
        with lock:
            msgs.append((time.monotonic(), msg))
        return msg

    monkeypatch.setattr(backend.hub, "publish", record)
    return msgs


def stream_events(msgs, event: str, runtime: str | None = None) -> list[tuple[float, dict]]:
    return [(t, m["payload"]) for t, m in list(msgs)
            if m["channel"] == "derived" and m["payload"].get("type") == "stream_event"
            and m["payload"].get("event") == event and (runtime is None or m["runtime"] == runtime)]


def connection_msgs(msgs, runtime: str) -> list[tuple[float, dict]]:
    return [(t, m["payload"]) for t, m in list(msgs)
            if m["channel"] == "runtime" and m["runtime"] == runtime
            and m["payload"].get("event") == "device_connection"]


def detectable_missing(scenario: str, duration_s: float, seed: int = 1) -> int:
    """Records the simulator lost in transport that a host CAN detect: a later
    record of the same boot arrives (loss at the very end of a boot is invisible)."""
    sim = Simulation(SimConfig(scenario=scenario, duration_s=duration_s, seed=seed), run_id="t1")
    recs = list(sim.records())
    last_rx: dict[str, int] = {}
    for r in recs:
        if not r.lost:
            b = r.record["boot_id"]
            last_rx[b] = max(last_rx.get(b, -1), r.record["seq"])
    return sum(1 for r in recs if r.lost and r.record["seq"] < last_rx.get(r.record["boot_id"], -1))


def sim_total_received(scenario: str, duration_s: float, seed: int = 1) -> int:
    return len(sim_frames(scenario, duration_s, seed))


def seq_of(frame: bytes) -> int:
    return json.loads(frame)["seq"]


def run_to_end(backend, name, frames, config, clock="device"):
    rt = backend.add_runtime(name, ScriptedAdapter(frames, clock=clock), config)
    assert wait_until(lambda: rt.finished, timeout=15), rt.status()
    return rt


def run_session_to_end(backend, name, frames, config):
    """Start a session after the first frame, then let the rest flow to the end."""
    gate = threading.Event()
    rt = backend.add_runtime(name, ScriptedAdapter([frames[0], ("gate", gate), *frames[1:]],
                                                   clock="device"), config)
    assert wait_until(lambda: rt.counters["records"] == 1)
    sess = backend.start_session(name, "gaps")
    gate.set()
    sid = sess["session_id"]
    # rt.finished flips before the session row is closed, so wait for the row itself
    assert wait_until(lambda: backend.session(sid)["state"] != "recording", timeout=15), rt.status()
    return rt, backend.session(sid)


# ---------------------------------------------------------------- sequence gaps


def test_missing_batches_seq_gap_total_equals_records_lost_in_transport(backend, sim_config, published):
    frames = sim_frames("missing_batches", 10)
    truth = detectable_missing("missing_batches", 10)
    assert truth > 0, "scenario must actually lose records"

    rt = run_to_end(backend, "wearable", frames, sim_config)

    assert rt.counters["records"] == len(frames)
    assert rt.events["seq_gap"] == truth
    gaps = stream_events(published, "seq_gap", "wearable")
    assert gaps, "every gap must be published on the derived channel"
    assert sum(p["detail"]["missing_records"] for _, p in gaps) == truth
    # loss is not mistaken for reordering or duplication
    assert rt.events.get("late_record", 0) == 0
    assert rt.events.get("duplicate_record", 0) == 0
    # status() exposes the same counts to the dashboard
    assert rt.status()["events"]["seq_gap"] == truth


def test_hand_dropped_frames_add_exactly_their_count(backend, sim_config, published):
    frames = sim_frames("happy_path", 5)
    baseline = run_to_end(backend, "base", frames, sim_config).events.get("seq_gap", 0)

    dropped_single = {12, 40}
    dropped_run = set(range(70, 77))  # seven consecutive records lost together
    drop = dropped_single | dropped_run
    assert max(drop) < len(frames) - 1
    kept = [f for i, f in enumerate(frames) if i not in drop]

    rt = run_to_end(backend, "dropped", kept, sim_config)

    assert rt.events["seq_gap"] == baseline + len(drop)
    gaps = [p for _, p in stream_events(published, "seq_gap", "dropped")]
    # a run of consecutive losses is one event that says how many were lost
    run_event = [p for p in gaps if p["source_seq"] == seq_of(frames[max(dropped_run) + 1])]
    assert len(run_event) == 1 and run_event[0]["detail"]["missing_records"] == len(dropped_run)
    for i in dropped_single:
        ev = [p for p in gaps if p["source_seq"] == seq_of(frames[i + 1])]
        assert len(ev) == 1 and ev[0]["detail"]["missing_records"] == 1


def test_seq_gaps_are_counted_in_the_active_session_and_its_files(backend, sim_config):
    frames = sim_frames("missing_batches", 10)
    truth = detectable_missing("missing_batches", 10)

    rt, row = run_session_to_end(backend, "wearable", frames, sim_config)

    assert row["state"] == "completed"
    assert row["counters"]["events"]["seq_gap"] == truth == rt.events["seq_gap"]
    # the session's own derived record holds the evidence, not just a counter
    derived = Path(row["dir"]) / "derived.ndjson"
    gaps = [json.loads(line) for line in derived.read_text().splitlines()
            if '"seq_gap"' in line]
    assert sum(g["detail"]["missing_records"] for g in gaps if g.get("event") == "seq_gap") == truth
    # the raw recording keeps the hole: frames recorded == frames received during the session
    tel = (Path(row["dir"]) / "telemetry.ndjson").read_bytes().splitlines()
    assert len(tel) == len(frames) - 1 == row["counters"]["frames"]


def test_reboot_is_a_boot_change_not_a_sequence_gap(backend, sim_config, published):
    frames = sim_frames("reboot", 10)
    boots = list(dict.fromkeys(json.loads(f)["boot_id"] for f in frames))
    assert len(boots) == 2

    rt, row = run_session_to_end(backend, "wearable", frames, sim_config)

    reboots = [p for _, p in stream_events(published, "boot_change", "wearable")
               if p["detail"]["previous_boot_id"] is not None]
    assert len(reboots) == 1
    assert reboots[0]["detail"]["previous_boot_id"] == boots[0]
    assert rt.boot_id == boots[1] and rt.status()["boot_id"] == boots[1]
    # seq restarting at a reboot is not loss, lateness or duplication
    for ev in ("seq_gap", "late_record", "duplicate_record"):
        assert rt.events.get(ev, 0) == 0, ev
    # the session saw the reboot (it started after the first boot was announced)
    assert row["boot_ids"] == boots
    assert row["counters"]["events"]["boot_change"] == 1


def test_fault_mix_surfaces_gaps_reboot_and_sensor_faults_in_session(backend, sim_config, published):
    frames = sim_frames("fault_mix", 10)
    truth = detectable_missing("fault_mix", 10)
    assert truth > 0

    rt, row = run_session_to_end(backend, "wearable", frames, sim_config)

    assert rt.events["seq_gap"] == truth
    assert row["counters"]["events"]["seq_gap"] == truth
    assert row["counters"]["events"]["boot_change"] == 1
    assert len(row["boot_ids"]) == 2
    # the device-reported sensor faults are surfaced too
    assert row["counters"]["events"].get("device_sensor_state", 0) >= 1
    assert stream_events(published, "device_sensor_state", "wearable")
    # nothing was silently swallowed: every frame was received and parsed
    assert rt.counters["records"] == len(frames)
    assert rt.counters.get("invalid_frames", 0) == 0


def test_device_that_never_rebooted_reports_no_boot_change(backend, sim_config):
    rt = run_to_end(backend, "wearable", sim_frames("happy_path", 3), sim_config)
    # a dashboard reading status()["events"] must not report a reboot that never happened
    assert rt.status()["events"].get("boot_change", 0) == 0


# ---------------------------------------------------------------- disconnects


def test_disconnect_is_visible_goes_stale_and_recovers_after_reconnect(backend, sim_config, published):
    frames = sim_frames("happy_path", 4)
    assert detectable_missing("happy_path", 4) == 0
    lost = set(range(80, 90))  # frames generated while the cable was out
    before = frames[:80]
    after = frames[90:]
    adapter = ScriptedAdapter([*before, "disconnect", *after], clock="host",
                              auto_reconnect=False, hold_open=True)
    rt = backend.add_runtime("wearable", adapter, sim_config)

    assert wait_until(lambda: rt.host_state == "disconnected")
    disc = [(t, p) for t, p in connection_msgs(published, "wearable") if p["connection"] == "disconnected"]
    assert len(disc) == 1, connection_msgs(published, "wearable")
    t_disc = disc[0][0]
    assert disc[0][1]["detail"]
    assert rt.counters["records"] == len(before)
    st = rt.status()
    assert st["state"] == "disconnected" and st["connection"] == "disconnected"
    # the state change itself also reaches the runtime channel
    assert any(m["channel"] == "runtime" and m["payload"].get("state") == "disconnected"
               for _, m in list(published))

    # silence becomes staleness quickly, for both streams
    assert wait_until(lambda: {p["stream"] for _, p in stream_events(published, "stream_stale", "wearable")}
                      >= {"emg", "imu"}, timeout=2)
    stale = stream_events(published, "stream_stale", "wearable")
    for stream in ("emg", "imu"):
        t_stale = min(t for t, p in stale if p["stream"] == stream)
        assert t_stale - t_disc < STALE_DEADLINE_S, (stream, t_stale - t_disc)
    # old values are never shown as current
    snap = rt.snapshot()
    for stream in ("emg", "imu"):
        latest = snap["latest"][stream]
        assert latest["status"] == "unavailable" and "stale" in latest["reasons"], latest
        assert latest["features"]["window_stats"]["ac_rms"] is None

    # while unplugged the backend keeps retrying and stays disconnected
    assert wait_until(lambda: adapter.open_calls >= 3, timeout=3)
    assert rt.host_state == "disconnected"

    n_msgs = len(published)
    adapter.reconnect()
    assert wait_until(lambda: rt.counters["records"] == len(before) + len(after), timeout=5), rt.status()
    assert wait_until(lambda: rt.host_state == "ready")
    conns = [p["connection"] for _, p in connection_msgs(published, "wearable")]
    assert conns[-2:] == ["disconnected", "connected"]
    assert rt.counters["connection_disconnected"] == 1
    # the records lost while unplugged are reported as a gap once data flows again
    assert rt.events["seq_gap"] == len(lost)
    after_msgs = published[n_msgs:]
    assert {p["stream"] for _, p in stream_events(after_msgs, "stream_recovered", "wearable")} == {"emg", "imu"}
    fresh = [m["payload"] for _, m in after_msgs if m["channel"] == "derived"
             and m["payload"].get("type") == "stream_features"
             and m["payload"]["status"] in ("ok", "degraded", "warming_up")]
    assert {p["stream"] for p in fresh} == {"emg", "imu"}


def test_session_survives_a_disconnect_and_counts_it(backend, sim_config, published):
    frames = sim_frames("happy_path", 4)
    gate = threading.Event()
    adapter = ScriptedAdapter([frames[0], ("gate", gate), *frames[1:60], "disconnect", *frames[60:]],
                              clock="host", auto_reconnect=False, hold_open=True)
    rt = backend.add_runtime("wearable", adapter, sim_config)
    assert wait_until(lambda: rt.counters["records"] == 1)
    sess = backend.start_session("wearable", "unplug")
    gate.set()

    assert wait_until(lambda: rt.connection == "disconnected")
    # the session is not silently ended by an unplug, but the dashboard can see it
    assert rt.host_state == "disconnected"
    assert rt.status()["active_session_id"] == sess["session_id"]
    assert backend.session(sess["session_id"])["state"] == "recording"
    assert wait_until(lambda: rt.events["stream_stale"] >= 2, timeout=2)

    adapter.reconnect()
    assert wait_until(lambda: rt.counters["records"] == len(frames), timeout=5)
    assert wait_until(lambda: rt.host_state == "recording")
    row = backend.stop_session(sess["session_id"])

    assert row["state"] == "completed"
    assert row["counters"]["connection_disconnected"] == 1
    assert row["counters"]["connection_connected"] == 1
    assert row["counters"]["events"]["stream_stale"] >= 2
    assert row["counters"]["events"]["stream_recovered"] >= 2
    assert row["counters"]["frames"] == len(frames) - 1


def test_starting_a_session_while_disconnected_is_refused(backend, sim_config):
    frames = sim_frames("happy_path", 2)
    adapter = ScriptedAdapter([*frames[:20], "disconnect", *frames[20:]], clock="host",
                              auto_reconnect=False, hold_open=True)
    rt = backend.add_runtime("wearable", adapter, sim_config)
    assert wait_until(lambda: rt.host_state == "disconnected")

    with pytest.raises(RuntimeConflict):
        backend.start_session("wearable", "nope", request_id="r-1")
    assert backend.history()["items"] == []

    adapter.reconnect()
    assert wait_until(lambda: rt.host_state == "ready", timeout=5)
    row = backend.start_session("wearable", "after-reconnect", request_id="r-2")
    assert row["state"] == "recording"


def test_disconnect_before_any_data_shows_disconnected_without_fake_values(backend, sim_config, published):
    frames = sim_frames("happy_path", 2)
    adapter = ScriptedAdapter(["disconnect", *frames], clock="host", auto_reconnect=False, hold_open=True)
    rt = backend.add_runtime("wearable", adapter, sim_config)

    assert wait_until(lambda: rt.host_state == "disconnected")
    snap = rt.snapshot()
    assert snap["latest"] == {"emg": None, "imu": None, "score": None, "tool_use": None, "exposure": None}
    assert snap["last_rx_age_s"] is None
    assert [p["connection"] for _, p in connection_msgs(published, "wearable")][-1] == "disconnected"

    adapter.reconnect()
    assert wait_until(lambda: rt.counters["records"] == len(frames), timeout=5)
    assert rt.events.get("seq_gap", 0) == 0


def test_sim_inject_disconnect_surfaces_disconnect_staleness_and_reconnect(backend, sim_config, published):
    duration, speed = 12.0, 6.0
    adapter = SimAdapter(scenario="happy_path", duration_s=duration, speed=speed, loop=False, run_id="inj")
    rt = backend.add_runtime("wearable", adapter, sim_config)
    assert wait_until(lambda: rt.counters["records"] > 40, timeout=5)
    assert rt.host_state == "ready"

    reply = backend.runtime("wearable").inject_disconnect(0.6)
    assert reply["unplugged_for_s"] == 0.6

    assert wait_until(lambda: rt.host_state == "disconnected", timeout=2)
    disc = [(t, p) for t, p in connection_msgs(published, "wearable") if p["connection"] == "disconnected"]
    assert len(disc) == 1
    assert wait_until(lambda: {p["stream"] for t, p in stream_events(published, "stream_stale", "wearable")
                               if t >= disc[0][0]} >= {"emg", "imu"}, timeout=2)
    stale_t = [t for t, p in stream_events(published, "stream_stale", "wearable") if t >= disc[0][0]]
    assert min(stale_t) - disc[0][0] < STALE_DEADLINE_S
    assert rt.snapshot()["latest"]["emg"]["status"] == "unavailable"

    assert wait_until(lambda: rt.host_state == "ready", timeout=5)
    assert [p["connection"] for _, p in connection_msgs(published, "wearable")][-1] == "connected"
    n = rt.counters["records"]
    assert wait_until(lambda: rt.counters["records"] > n + 10, timeout=5), "data must flow again"
    assert wait_until(lambda: rt.finished, timeout=10)
    # whatever was lost is accounted for exactly as a sequence gap
    total = sim_total_received("happy_path", duration)
    assert rt.events.get("seq_gap", 0) == total - rt.counters["records"]


def test_sim_inject_disconnect_loses_frames_generated_while_unplugged(backend, sim_config):
    duration, speed, unplug_s = 12.0, 6.0, 0.6
    adapter = SimAdapter(scenario="happy_path", duration_s=duration, speed=speed, loop=False, run_id="inj")
    rt = backend.add_runtime("wearable", adapter, sim_config)
    assert wait_until(lambda: rt.counters["records"] > 40, timeout=5)
    rt.inject_disconnect(unplug_s)
    assert wait_until(lambda: rt.host_state == "disconnected", timeout=2)
    assert wait_until(lambda: rt.finished, timeout=10)

    total = sim_total_received("happy_path", duration)  # what an uninterrupted host would receive
    received = rt.counters["records"]
    # every missing record is accounted for as a sequence gap ...
    assert rt.events["seq_gap"] == total - received
    # ... and the frames generated during the unplug window really are gone, as on a
    # real link (adapter docstring): at least half of the frames due in that window.
    frames_per_sim_s = total / duration
    window_frames = frames_per_sim_s * unplug_s * speed
    assert total - received >= 0.5 * window_frames, (total - received, round(window_frames))


def test_device_clock_disconnect_does_not_leave_ok_values_as_current(backend, sim_config):
    frames = sim_frames("happy_path", 4)
    adapter = ScriptedAdapter([*frames[:120], "disconnect"], clock="device", auto_reconnect=False)
    rt = backend.add_runtime("wearable", adapter, sim_config)
    assert wait_until(lambda: rt.host_state == "disconnected")
    time.sleep(0.6)  # longer than any staleness limit; there is no event to wait for
    snap = rt.snapshot()
    for stream in ("emg", "imu"):
        assert snap["latest"][stream]["status"] not in ("ok", "degraded"), snap["latest"][stream]


def test_inject_disconnect_is_refused_for_non_simulated_adapters(backend, sim_config):
    rt = backend.add_runtime("wearable", ScriptedAdapter([], hold_open=True), sim_config)
    assert wait_until(lambda: rt.host_state == "ready")
    with pytest.raises(RuntimeConflict):
        rt.inject_disconnect(1.0)
    assert rt.host_state == "ready"


def test_ws_subscriber_sees_disconnect_and_staleness(backend, sim_config):
    """A real hub subscription (what /ws/live uses) receives the disconnect story."""
    frames = sim_frames("happy_path", 3)
    adapter = ScriptedAdapter([*frames[:60], "disconnect"], clock="host", auto_reconnect=False)

    async def scenario() -> list[dict]:
        sub = backend.hub.subscribe(runtimes=frozenset({"wearable"}))
        backend.add_runtime("wearable", adapter, sim_config)
        got: list[dict] = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(sub.queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            got.append(msg)
            stale = {m["payload"].get("stream") for m in got if m["channel"] == "derived"
                     and m["payload"].get("event") == "stream_stale"}
            if stale >= {"emg", "imu"}:
                break
        backend.hub.unsubscribe(sub)
        return got

    got = asyncio.run(scenario())
    assert all(m["runtime"] == "wearable" for m in got)
    assert not any(m["channel"] == "telemetry" for m in got), "telemetry is opt-in"
    seqs = [m["hub_seq"] for m in got]
    assert seqs == sorted(seqs)
    conn = [m for m in got if m["channel"] == "runtime" and m["payload"].get("event") == "device_connection"]
    assert [m["payload"]["connection"] for m in conn][-1] == "disconnected"
    i_disc = got.index(conn[-1])
    stale_after = [m for m in got[i_disc:] if m["channel"] == "derived"
                   and m["payload"].get("event") == "stream_stale"]
    assert {m["payload"]["stream"] for m in stale_after} == {"emg", "imu"}
