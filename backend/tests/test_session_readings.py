"""Each recording stores what the screens showed during it: mean/peak activation and peak vibration."""
import threading

from conftest import ScriptedAdapter, sim_frames, wait_until
from haven_backend.runtime import _ActiveSession, _readings_view
from haven_backend.service import Backend, processing_config_for


def test_readings_view_rounds_and_reports_nothing_it_did_not_see():
    s = _ActiveSession.__new__(_ActiveSession)
    s.act_sum, s.act_n, s.act_peak, s.vib_peak, s.vib_n = 0.0, 0, None, None, 0
    v = _readings_view(s)
    assert v["mean_activation_pct"] is None and v["peak_activation_pct"] is None and v["peak_vibration_ms2"] is None
    s.act_sum, s.act_n, s.act_peak, s.vib_peak, s.vib_n = 150.0, 3, 80.04, 2.34567, 7
    v = _readings_view(s)
    assert v["mean_activation_pct"] == 50.0 and v["peak_activation_pct"] == 80.0
    assert v["peak_vibration_ms2"] == 2.346 and v["vibration_windows"] == 7 and v["activation_windows"] == 3


def test_a_recording_on_the_simulator_stores_its_peak_vibration(tmp_path):
    gate = threading.Event()
    frames = sim_frames("happy_path", duration_s=10.0)
    half = len(frames) // 2
    ad = ScriptedAdapter(frames[:20] + [("gate", gate)] + frames[20:half], hold_open=True)
    b = Backend(tmp_path / "data")
    try:
        rt = b.add_runtime("wearable", ad, processing_config_for("sim"))
        assert wait_until(lambda: rt.counters["records"] >= 20, 10)
        sess = b.start_session("wearable")
        gate.set()
        assert wait_until(lambda: rt.counters["records"] >= half - 2, 15)
        row = b.stop_session(sess["session_id"])
        r = row["counters"]["readings"]
        assert r["vibration_windows"] > 0 and isinstance(r["peak_vibration_ms2"], float) and r["peak_vibration_ms2"] > 0
        assert r["mean_activation_pct"] is None and r["activation_windows"] == 0   # never calibrated: no activation
        assert b.session(sess["session_id"])["counters"]["readings"] == r          # stored, not just returned
    finally:
        b.shutdown()
