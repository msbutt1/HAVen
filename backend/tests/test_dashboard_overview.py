"""The dashboard's Overview screen: what its tiles compute from saved items, and the browser demo's sample history."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run():
    r = subprocess.run(["node", str(Path(__file__).with_name("dashboard_overview_check.js"))],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_sample_history_is_labelled_and_deterministic():
    s = _run()["sample"]
    assert s["sessions"] > 30 and s["tests"] > 5
    assert s["same"] and s["allSimulated"] and s["noFuture"] and s["peakCapped"] and s["toolWithin"]


def test_overview_sums():
    o = _run()
    # oldest first; the item with an unreadable date is left out
    assert o["order"] == [["replay", "live"], [["simulated", 0, 0, 0], ["live", 2, 1, 1]]]
    st = o["stats"]
    assert st["n"] == 2 and st["recS"] == 1800 and st["tests"] == 2
    # tool share only over recordings that measured tool use (300 s of 600 s, not of 1800 s)
    assert st["toolS"] == 300 and st["toolShare"] == 0.5
    assert st["peakVib"] == 6 and st["grip"] == 50         # missing grip is left out, never counted as 0
    assert st["lastThreshold"] == 80
    live = o["liveOnly"]
    assert live["n"] == 1 and live["tests"] == 1 and live["peakVib"] == 6
