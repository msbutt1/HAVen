from conftest import ScriptedAdapter, sim_frames, wait_until


def test_scripted_demo_session_records_and_finishes(backend, sim_config):
    import threading
    gate = threading.Event()
    frames = sim_frames(duration_s=5)
    rt = backend.add_runtime("wearable", ScriptedAdapter([frames[0], ("gate", gate), *frames[1:]]), sim_config)
    assert wait_until(lambda: rt.counters["records"] == 1)
    s = backend.start_session("wearable", "smoke")
    gate.set()
    assert wait_until(lambda: rt.finished)
    row = backend.session(s["session_id"])
    assert row["state"] == "completed" and row["end_reason"] == "end_of_input"
    assert row["source"] == "simulated"
    assert row["counters"]["frames"] == len(frames) - 1
