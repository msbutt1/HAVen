"""Daily exposure on the backend: session summary, daily totals, personal limit, limits endpoint.

SIMULATED data only (fake Wi-Fi wearable, tool_vs_arm scenario) plus pure-function cases.
"""

import socket
import time

import pytest
from fastapi.testclient import TestClient

from conftest import wait_until
from haven_backend.app import create_app
from haven_backend.exposure import PersonalLimit, daily_exposure, session_exposure_summary
from haven_backend.fake_wifi import FakeWifiWearable
from haven_backend.service import Backend, processing_config_for
from haven_backend.wifi import WifiAdapter


def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rec(acc=1, e_tool=0.0, c_tool=0.0, e_grip=0.0, c_grip=0.0, unknown=0.0):
    return {"type": "exposure", "accumulation": acc, "gate": "gripped", "grip_unknown_s": unknown,
            "by_gate": {"tool_on": {"energy_m2s3": e_tool, "contact_s": c_tool},
                        "gripped": {"energy_m2s3": e_grip, "contact_s": c_grip}},
            "grip_during_contact": {"mean": 0.3}, "limits": {"personal_limit_points": 400}}


# -- pure functions -----------------------------------------------------------------------------

def test_session_summary_is_end_minus_start_of_one_accumulation():
    # 1 h at 5 m/s^2 inside the session = 25 * 3600 energy = 50 points
    s = session_exposure_summary(rec(e_grip=100.0, c_grip=10), None, rec(e_grip=100.0 + 90000, c_grip=3610,
                                                                          e_tool=90000, c_tool=3600))
    assert s["status"] == "ok" and s["gate"] == "gripped"
    assert s["points"] == pytest.approx(50.0) and s["contact_s"] == pytest.approx(3600)
    assert s["a8_ms2"] == pytest.approx(5.0 * (1 / 8) ** 0.5, rel=1e-3)


def test_session_summary_refuses_a_reset_or_mixed_sources():
    assert session_exposure_summary(rec(acc=1), None, rec(acc=2))["status"] == "unavailable"
    assert session_exposure_summary(None, rec(), rec(e_grip=5), mixed_sources=True)["reason"] == "session_mixes_data_sources"
    assert session_exposure_summary(None, None, None)["reason"] == "no_exposure_records_during_session"


def row(day, source, energy, contact=600.0):
    ex = {"status": "ok", "by_gate": {g: {"energy_m2s3": energy, "contact_s": contact} for g in ("tool_on", "gripped")}}
    return {"started_at": f"{day}T18:00:00+00:00", "source": source, "counters": {"exposure": ex}}


def test_daily_totals_add_sessions_but_never_sources():
    rows = [row("2026-11-07", "live", 25 * 3600 * 2), row("2026-11-07", "live", 25 * 3600 * 2),
            row("2026-11-07", "simulated", 999999.0), {"started_at": "2026-11-07T18:00:00+00:00",
                                                        "counters": {"exposure": {"status": "unavailable"}}}]
    d = daily_exposure(rows, 400, today="2026-11-08")
    live = [x for x in d["days"] if x["source"] == "live"][0]
    assert live["sessions"] == 2 and live["points"] == pytest.approx(200.0)   # 4 h at 5 m/s^2
    assert live["a8_ms2"] == pytest.approx(5.0 * (4 / 8) ** 0.5, rel=1e-3) and live["level"] == "above_eav"
    assert {x["source"] for x in d["days"]} == {"live", "simulated"}
    assert d["rolling_7_day"]["live"]["years_to_10pct_vwf"] is not None


def test_daily_percent_uses_the_personal_limit_and_old_days_drop_out():
    d = daily_exposure([row("2026-11-07", "live", 25 * 3600 * 2), row("2026-10-01", "live", 1.0)], 100,
                       today="2026-11-08")
    assert len(d["days"]) == 1 and d["days"][0]["percent_of_limit"] == pytest.approx(100.0)
    assert d["days"][0]["level"] == "above_limit"


def test_personal_limit_persists_and_can_only_be_lowered(tmp_path):
    p = PersonalLimit(tmp_path)
    assert p.points == 400 and not p.view()["lowered"]
    assert p.set(100, "2026-11-08T00:00:00Z")["equivalent_a8_ms2"] == pytest.approx(2.5)
    assert PersonalLimit(tmp_path).points == 100
    for bad in (0, 401, "100", True, float("inf")):
        with pytest.raises(ValueError):
            p.set(bad, "x")


# -- API ----------------------------------------------------------------------------------------

def test_limits_and_personal_limit_endpoints(tmp_path):
    b = Backend(tmp_path / "data")
    try:
        with TestClient(create_app(b), base_url="http://localhost") as c:
            lim = c.get("/api/exposure/limits").json()
            assert lim["limits"]["exposure_action_value"]["a8_ms2"] == 2.5
            assert lim["limits"]["exposure_limit_value"]["points"] == 400
            assert "2002/44/EC" in lim["sources"]["eav_elv"]
            assert c.get("/api/exposure/personal-limit").json()["personal_limit_points"] == 400
            r = c.put("/api/exposure/personal-limit", json={"personal_limit_points": 150})
            assert r.status_code == 200 and r.json()["personal_limit_points"] == 150
            assert c.put("/api/exposure/personal-limit", json={"personal_limit_points": 1000}).status_code == 400
            assert c.get("/api/exposure/daily").json()["personal_limit_points"] == 150
    finally:
        b.shutdown()


def test_a_recording_stores_its_exposure_and_the_live_record_is_published(tmp_path):
    tcp, udp = free_port(), free_port(socket.SOCK_DGRAM)
    b = Backend(tmp_path / "data")
    fw = None
    try:
        b.set_personal_exposure_limit(200)
        rt = b.add_runtime("wearable", WifiAdapter("127.0.0.1", tcp, udp), processing_config_for("wifi"))
        assert rt.proc.exposure.personal_limit_points == 200           # applied to a new runtime
        fw = FakeWifiWearable("tool_vs_arm", duration_s=40.0, speed=4.0, discovery_port=udp).start()
        assert wait_until(lambda: rt.counters["records"] > 20, 15)
        sess = b.start_session("wearable")
        assert wait_until(lambda: ((rt.latest.get("exposure") or {}).get("by_gate") or {})
                          .get("tool_on", {}).get("contact_s", 0) > 1.0, 30)
        time.sleep(0.5)
        latest = rt.latest["exposure"]
        assert latest["type"] == "exposure" and latest["limits"]["personal_limit_points"] == 200
        row = b.stop_session(sess["session_id"])
        ex = row["counters"]["exposure"]
        assert ex["status"] == "ok" and ex["by_gate"]["tool_on"]["contact_s"] > 0
        assert ex["by_gate"]["tool_on"]["points"] > 0
        day = b.exposure_daily()
        assert day["days"] and day["days"][0]["source"] == row["source"]
    finally:
        if fw:
            fw.stop()
        b.shutdown()
