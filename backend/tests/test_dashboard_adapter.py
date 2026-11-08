"""The dashboard's BackendAdapter turns the backend's real messages into the dashboard's channels (run under node)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def test_backend_adapter_translation():
    r = subprocess.run(["node", str(Path(__file__).with_name("dashboard_adapter_check.js"))],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["wsUrl"] == "ws://h:1/ws/live"
    assert o["state"] == ["sleeve:ready", "station:ready"]
    assert o["browserWs"] == [1, True]                                      # a browser: just the URL, as before
    assert o["authWs"] == ["ws://h:2/ws/live", "Basic dGVhbTpwdw=="]
    assert o["authFetch"] == "Basic dGVhbTpwdw=="
    assert o["history"] == [["s1", "replay", 30, 2.5, "stopped"],           # wearable sessions only, none still recording
                            ["s3", "simulated", 30, 0.059868, "stopped"],        # the backend's session_au (start was null)
                            ["s4", "live", 30, None, "stopped"]]            # session_au null: the backend could not attribute it
    assert o["historyTool"] == [["s1", None, None], ["s3", 12.45, 1], ["s4", None, None]]
    assert o["t1"] == [50, 2.5, 4.2, None, "simulated", True, 183.1]       # display 0.5 -> 50 %; no total outside a recording
    assert o["t2"] == [None, None, None, False]                            # nothing invented before calibration / conversion
    assert o["ignoredOther"] is True
    assert o["start"] == ["accepted", "s9"] and o["total"] == 0.75         # cumulative 4.75 - 4 at the start
    assert o["session"] == [["s9", "stopped", None, 10, 42.5, 3.1]]       # uncalibrated: no score; readings from the backend
    assert o["lateStop"] == ["accepted", True, "the replayed recording reached its end"]
    assert o["histStation"] == [["ss0", "simulated", 110, 3, 410, 1, 1]]  # finished tests only; threshold re-derived from the trials
    assert o["stStart"] == "accepted" and o["stSource"] == "simulated"     # stored as simulated, never live
    assert o["stStim"] == [[150, False], [110, False], [70, False], [0, True], [110, False], [70, False]]
    assert o["stResults"][3] == [4, "missing", 0]                          # a timeout is missing, never "not felt"
    assert o["stDone"] == ["simulated", 83.3, [70, 110, 70], 1, 0, 1, False]
    assert o["stStates"] == ["testing", "completed"] and o["stAbortIdle"] == "error"
    assert o["disc"] == "disconnected:unplugged" and o["err"] == "error:boom"
    assert o["cal"] == ["failed:recording in progress"]
    assert o["calRelaxed"][0] == "relax" and o["calRelaxed"][-1] == "relaxed_captured"   # step 1 ends waiting for step 2
    assert o["calFailed"] == ["The squeeze wasn’t clearly stronger than rest. Squeeze harder, or check the pads, and try again."]
    assert o["grip"] == [125, 100, True, False, True]          # unbounded value, clamped bar, flags
    assert o["tool"] == [["tool", 12.5, False]] and o["event"] == ["boot_change"]
    assert o["shift"] == ["accepted", "tool_session_reset", "yes"]
