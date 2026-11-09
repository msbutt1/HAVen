"""CLI smoke tests (simulated sessions only)."""

import json

from haven_station.cli import main


def test_run_saves_once_and_reports_simulated(tmp_path, capsys):
    assert main(["run", "--mode", "preview", "--seed", "1", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "[SIMULATED]" in out and "PREVIEW_short_run_not_equivalent_to_full_test" in out
    saved = json.loads(next(tmp_path.glob("*.station_result.json")).read_text(encoding="utf-8"))
    assert saved["provenance"]["source"] == "simulated" and saved["is_preview"] is True
    assert main(["run", "--mode", "preview", "--seed", "1", "--out", str(tmp_path)]) == 0
    assert "already existed" in capsys.readouterr().out
    assert len(list(tmp_path.glob("*.station_result.json"))) == 1


def test_run_writes_trial_and_event_streams(tmp_path):
    main(["run", "--seed", "2", "--session-id", "cli-2", "--out", str(tmp_path)])
    trials = [json.loads(l) for l in (tmp_path / "cli-2.trials.ndjson").read_text().splitlines()]
    events = [json.loads(l) for l in (tmp_path / "cli-2.events.ndjson").read_text().splitlines()]
    assert trials and trials[0]["schema"] == "vibeguard.station_trial"
    assert events[0]["type"] == "state_changed" and events[-1]["type"] == "result_saved"


def test_abort_option_saves_an_aborted_result(tmp_path, capsys):
    assert main(["run", "--abort-after", "3", "--out", str(tmp_path)]) == 0
    assert "state     aborted" in capsys.readouterr().out
