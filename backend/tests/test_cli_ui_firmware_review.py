"""Review of the CLI wiring, status page and docs for the firmware paths (stream adapter, device_id selection).

Each xfail(strict=True) test reproduces one defect found by running the real commands; remove the marker when fixed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import uvicorn
from test_http_review import NODE, _run_page, _runtime, ok

from haven_backend import hardware
from haven_backend.adapters import AdapterDisconnected
from haven_backend.cli import main as cli_main

EXAMPLE_PROFILE = Path(__file__).resolve().parents[1] / "hardware.example.json"


@pytest.fixture
def no_server(monkeypatch):
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)


def test_missing_stream_file_is_rejected_before_the_data_dir_is_touched(no_server, tmp_path):
    d = tmp_path / "data"
    rc = cli_main(["serve", "--device", "stream", "--stream", str(tmp_path / "nope.ndjson"), "--data-dir", str(d)])
    assert rc == 2 and not d.exists()


def test_empty_device_id_is_refused(no_server, tmp_path):
    d = tmp_path / "data"
    rc = cli_main(["serve", "--device", "serial", "--port", "/dev/null", "--baud", "115200", "--device-id", "",
                   "--data-dir", str(d)])
    assert rc == 2 and not d.exists()


@pytest.mark.parametrize("extra", [["--port", "/dev/cu.usbserial-0001"], ["--stream", "fake.ndjson"], ["--paced"]])
def test_options_for_another_device_are_not_silently_ignored(no_server, tmp_path, extra):
    d = tmp_path / "data"
    rc = cli_main(["serve", "--data-dir", str(d), *extra])  # default --device sim
    assert rc == 2 and not d.exists()


class _RecordingAdapter:
    instances: list = []

    def __init__(self, *a, **kw):
        self.args, self.kw = a, kw
        type(self).instances.append(self)
        self.port = None
        self.candidates = []

    def open(self):
        raise AdapterDisconnected("scripted: not opened")

    def close(self):
        pass


@pytest.mark.parametrize("extra, target", [([], "haven_backend.serial_select.DeviceIdSerialAdapter"),
                                           (["--try-bauds", "115200"], "haven_backend.serial_select.SerialAdapter")])
def test_serial_check_honours_the_profiles_dtr_rts(monkeypatch, extra, target):
    _RecordingAdapter.instances = []
    mod, name = target.rsplit(".", 1)
    monkeypatch.setattr(__import__(mod, fromlist=[name]), name, _RecordingAdapter)
    cli_main(["serial-check", "--port", "/dev/cu.usbserial-0001", "--profile", str(EXAMPLE_PROFILE), "--seconds", "1",
              *extra])
    assert _RecordingAdapter.instances, "the adapter was not used"
    kw = _RecordingAdapter.instances[0].kw
    assert kw.get("dtr") is False and kw.get("rts") is False


def test_wrong_board_hint_is_about_the_device_id():
    hint = hardware._classify_open_error("no port carries device vg-w-2805a5075554 (/dev/cu.usbserial-0001: vg-w-aaaaaaaaaaaa)")
    assert "cable" not in hint.lower() and "device" in hint.lower()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_finished_stream_banner_does_not_offer_replay_options_that_do_not_work():
    rt = _runtime(state="completed", adapter={"kind": "stream", "source": "fake.ndjson"})
    (tick,) = _run_page(ok(rt))
    text = tick["banner"]["textContent"]
    assert "--loop" not in text and "restart" not in text.lower()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_disconnected_banner_shows_the_reason():
    detail = "no port carries device vg-w-2805a5075554 (/dev/cu.usbserial-0001: vg-w-aaaaaaaaaaaa)"
    rt = _runtime(state="disconnected", data_fresh=False, connection="disconnected", connection_detail=detail)
    (tick,) = _run_page(ok(rt))
    assert "vg-w-aaaaaaaaaaaa" in tick["banner"]["textContent"]


# ---- verified behaviour (passing) -------------------------------------------------------------------------------------

HELLO = {"firmware": {"name": "vg-fw", "version": "0.1"},
         "hardware": {"board": "ESP32", "acquisition": "FAKE (SIMULATED)", "config_problems": "none", "link": "uart0"},
         "streams": {"emg": {"nominal_rate_hz": 1000, "channels": ["emg0"], "raw_limits": {"min": 0, "max": 4095},
                             "sensor_model": None}}}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_device_facts_escape_hostile_strings_and_ignore_negative_or_non_loss_counters():
    evil = "<img src=x onerror=alert(1)>"
    hello = json.loads(json.dumps(HELLO))
    hello["hardware"].update(board=evil, acquisition=evil, config_problems=evil)
    rt = _runtime(hello=hello, device_status={"counters": {"encode_errors": -1, "tx_dropped_records": 2,
                                                           "frames": 99, evil + "_dropped": 1}})
    (tick,) = _run_page(ok(rt))
    for el in ("facts_head", "facts_acq", "facts_counters"):
        assert tick[el]["innerHTML"] == "" or "<img" not in tick[el]["innerHTML"], "set through textContent only"
    assert "tx_dropped_records=2" in tick["facts_counters"]["textContent"]
    assert "encode_errors" not in tick["facts_counters"]["textContent"]
    assert "frames=" not in tick["facts_counters"]["textContent"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("hardware_value", [None, "oops", {"board": 5, "acquisition": {"a": 1}, "config_problems": ["x"]}])
def test_device_facts_survive_odd_hardware_shapes(hardware_value):
    hello = json.loads(json.dumps(HELLO))
    hello["hardware"] = hardware_value
    rt = _runtime(hello=hello, device_status=None)
    (tick,) = _run_page(ok(rt))
    assert tick["facts_head"]["textContent"].startswith("Firmware vg-fw 0.1")
    assert "<td>emg</td>" in tick["facts"]["innerHTML"]
    assert tick["err"]["style"].get("display") != "block"
