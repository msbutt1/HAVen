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
    assert o["state"] == ["sleeve:ready", "station:disconnected"]
    assert o["history"] == [["s1", "replay", 30, 2.5, "stopped"]]          # wearable sessions only, none still recording
    assert o["t1"] == [50, 2.5, 4.2, None, "simulated", True, 183.1]       # display 0.5 -> 50 %; no total outside a recording
    assert o["t2"] == [None, None, None, False]                            # nothing invented before calibration / conversion
    assert o["ignoredOther"] is True
    assert o["start"] == ["accepted", "s9"] and o["total"] == 0.75         # cumulative 4.75 - 4 at the start
    assert o["session"] == [["s9", "stopped", None, 10]]                   # uncalibrated: no score
    assert o["station"] == "error"                                         # not connected: never a fake success
    assert o["disc"] == "disconnected:unplugged" and o["err"] == "error:boom"
    assert o["cal"] == ["failed:recording in progress"]
