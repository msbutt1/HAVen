"""Adversarial review of the hardware profile and ``serve --profile`` path. SIMULATED firmware, offline.

xfail(strict=True) marks a reproduced defect: when it is fixed the test XPASSes, strict mode fails the run,
and the marker must be removed. Plain tests pin what was verified to work.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import ScriptedAdapter, sim_frames, wait_until
from test_backup_review import _check, _runtime, fake  # noqa: F401  (fixture)

from vibeguard_backend.adapters import SerialAdapter
from vibeguard_backend.backup import export_backup, start_backup_replay
from vibeguard_backend.cli import main as cli_main
from vibeguard_backend.hardware import ProfileError, load_profile
from vibeguard_backend.preflight import FAIL, PASS, WARN, run_preflight
from vibeguard_backend.service import Backend, processing_config_for
from vibeguard_backend.status_page import STATUS_PAGE

ROOT = Path(__file__).resolve().parents[1]
SRC = "MPU-6050 datasheet p.13, +/-2 g range"


def write(tmp_path, obj, name="hw.json"):
    p = tmp_path / name
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return p


def good(**over):
    """A valid profile; emg.output and baud are required (nothing is assumed), so the base states them."""
    return {"name": "bench", "baud": 115200, "emg": {"output": "raw"}, **over}


# ============================================================================ load_profile validation

@pytest.mark.parametrize("raw", [
    '{"name":"x","imu":{"counts_per_g":NaN,"conversion_source":"datasheet p1 x"}}',
    '{"name":"x","imu":{"counts_per_g":Infinity,"conversion_source":"datasheet p1 x"}}',
    '{"name":"x","imu":{"counts_per_g":1e999,"conversion_source":"datasheet p1 x"}}',
    '{"name":"x","imu":{"counts_per_g":-16384,"conversion_source":"datasheet p1 x"}}',
    '{"name":"x","imu":{"counts_per_g":0,"conversion_source":"datasheet p1 x"}}',
    '{"name":"x","imu":{"counts_per_g":true,"conversion_source":"datasheet p1 x"}}',
    '{"name":"x","imu":{"counts_per_g":"16384","conversion_source":"datasheet p1 x"}}',
    '{"name":"x","emg":{"mains_hz":true}}', '{"name":"x","emg":{"mains_hz":[50]}}',
    '{"name":"x","baud":921600.0}', '{"name":"x","baud":"921600"}', '{"name":"x","baud":true}',
    '{"name":"x","emg":{"output":"Envelope"}}', '{"name":"x","emg":{"output":null}}',
    '{"name":"x","extra":1}', '{"name":5}', '{"name":"   "}', '[]', '', '{"name":"x","imu":[]}',
])
def test_profile_values_that_would_poison_the_conversion_are_refused(tmp_path, raw):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, raw))


def test_envelope_is_refused_with_the_reason_and_a_source_is_required_for_a_sensitivity(tmp_path):
    with pytest.raises(ProfileError, match="ENVELOPE"):
        load_profile(write(tmp_path, good(emg={"output": "envelope"})))
    with pytest.raises(ProfileError, match="conversion_source"):
        load_profile(write(tmp_path, good(imu={"counts_per_g": 16384})))


def test_a_valid_profile_keeps_unusual_but_harmless_text_as_data(tmp_path):
    p = load_profile(write(tmp_path, good(name="Café über ☃ <b>", notes="x\ty")))
    assert p.name == "Café über ☃ <b>" and p.describe()["notes"] == "x\ty"


@pytest.mark.parametrize("obj", [
    good(emg={"outptu": "envelope"}),
    good(emg={"chanels": 2}),
    good(imu={"counts_per_gee": 16384, "conversion_source": "datasheet p1 x"}),
    good(imu={"range": 4}),
])
def test_a_typo_inside_emg_or_imu_is_an_error(tmp_path, obj):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, obj))


@pytest.mark.parametrize("port", [5, ["/dev/x"], {"a": 1}, ""])
def test_port_must_be_a_non_empty_string(tmp_path, port):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, good(port=port)))


@pytest.mark.parametrize("channels", ["lots", -3, 0, 10 ** 9, True, 1.5])
def test_emg_channels_must_be_a_small_positive_integer(tmp_path, channels):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, good(emg={"channels": channels})))


@pytest.mark.parametrize("range_g", ["x", -2, 0, True, [2]])
def test_imu_range_g_must_be_a_positive_number(tmp_path, range_g):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, good(imu={"range_g": range_g})))


@pytest.mark.parametrize("cpg", [5e-324, 1e-300, 0.5, 1e300])
def test_an_absurd_sensitivity_is_refused_at_load(tmp_path, cpg):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, good(imu={"counts_per_g": cpg, "conversion_source": "datasheet p1 x"})))


def test_a_huge_integer_sensitivity_is_a_profile_error_not_a_traceback(tmp_path):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, good(imu={"counts_per_g": 10 ** 400, "conversion_source": "datasheet p1 x"})))


@pytest.mark.parametrize("name", ["a\nb", "a\x1b[31mred", "a\x00b", "tab\there"])
def test_profile_name_has_no_control_characters(tmp_path, name):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, {"name": name}))


@pytest.mark.parametrize("field", ["name", "notes"])
def test_profile_text_fields_are_bounded(tmp_path, field):
    obj = good()
    obj[field] = "a" * 1_000_000
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, obj))


def test_a_profile_saved_with_a_utf8_bom_still_loads(tmp_path):
    p = tmp_path / "hw.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(good()).encode())
    assert load_profile(p).name == "bench"


def test_unstated_emg_output_is_not_assumed_raw(tmp_path):
    with pytest.raises(ProfileError, match="emg.output is required"):  # raw vs envelope is never defaulted
        load_profile(write(tmp_path, good(emg={"channels": 1})))


def test_no_baud_in_profile_and_cli_is_not_silently_921600(serve_env, tmp_path, capsys):
    nobaud = {"name": "bench", "port": "/dev/x", "emg": {"output": "raw"}}
    assert load_profile(write(tmp_path, nobaud)).baud is None  # the loader says unknown ...
    rc, calls = _serve_cli(tmp_path, ["--profile", str(write(tmp_path, nobaud))])
    assert rc == 2 and "baud" in capsys.readouterr().err and not calls  # ... and serve refuses instead of guessing
    rc, calls = _serve_cli(tmp_path, ["--profile", str(write(tmp_path, nobaud)), "--baud", "230400"])
    assert rc == 0 and calls["adapter"].baud == 230400


# ============================================================================ hardware.example.json

def test_the_example_file_loads_and_says_how_to_add_a_conversion():
    p = load_profile(ROOT / "hardware.example.json")
    assert p.imu_counts_per_g is None  # no invented sensitivity ships in the example
    assert "conversion_source" in (p.notes or "")  # the notes show how to add a cited one


def test_the_example_copied_unchanged_does_not_produce_a_cited_conversion(tmp_path):
    shutil.copy(ROOT / "hardware.example.json", tmp_path / "hardware.json")
    try:
        p = load_profile(tmp_path / "hardware.json")
    except ProfileError:
        return
    cfg = processing_config_for("serial", p.imu_counts_per_g, p.imu_conversion_source)
    assert cfg.imu.conversion is None


# ============================================================================ processing_config_for

def test_conversion_is_applied_to_accelerometer_axes_only_and_cites_its_source():
    cfg = processing_config_for("serial", 16384, SRC)
    assert set(cfg.imu.conversion) == {"ax", "ay", "az"}          # never to gyro channels
    assert all(v == (pytest.approx(9.80665 / 16384), 0.0) for v in cfg.imu.conversion.values())
    assert cfg.imu.conversion_source == SRC
    assert cfg.emg.conversion is None and cfg.emg.conversion_source is None


def test_no_sensitivity_means_no_conversion_for_serial_and_for_replay():
    for kind in ("serial", "replay"):
        cfg = processing_config_for(kind)
        assert cfg.imu.conversion is None and cfg.imu.conversion_source is None


def test_a_sensitivity_without_a_source_is_refused():
    with pytest.raises(ValueError):
        processing_config_for("serial", 16384, None)


def test_nan_sensitivity_is_refused():
    with pytest.raises(ValueError):
        processing_config_for("serial", float("nan"), SRC)


def test_negative_sensitivity_is_refused():
    with pytest.raises(ValueError):
        processing_config_for("serial", -16384, SRC)


def test_infinite_sensitivity_is_refused():
    with pytest.raises(ValueError):
        processing_config_for("serial", float("inf"), SRC)


# ============================================================================ serve --profile: precedence

@pytest.fixture
def serve_env(monkeypatch):
    """Run ``serve`` in-process without opening sockets; record what it hands to the runtime."""
    import uvicorn

    from vibeguard_backend import backup as backup_mod

    backends: list[Backend] = []
    orig_init = Backend.__init__

    def init(self, *a, **kw):
        orig_init(self, *a, **kw)
        backends.append(self)

    monkeypatch.setattr(Backend, "__init__", init)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    monkeypatch.setattr(backup_mod, "start_backup_replay", lambda *a, **kw: None)
    yield
    for b in backends:
        b.shutdown()


_n = [0]


def _serve_cli(tmp_path, argv, real_runtime=False):
    """Returns (exit code, what add_runtime received)."""
    import uvicorn
    assert getattr(uvicorn.run, "__name__", "") == "<lambda>", "use the serve_env fixture: never start a real server here"
    calls: dict = {}
    _n[0] += 1
    orig = Backend.add_runtime

    def rec(self, name, adapter, config, **kw):
        calls.update(name=name, adapter=adapter, config=config, meta=kw.get("meta"))
        if real_runtime:
            return orig(self, name, adapter, config, **kw)
        return None

    Backend.add_runtime = rec
    try:
        rc = cli_main(["--data-dir", str(tmp_path / f"data{_n[0]}"), "serve", *argv])
    finally:
        Backend.add_runtime = orig
    return rc, calls


def test_profile_supplies_port_baud_and_conversion_and_flags_override_it(serve_env, tmp_path):
    prof = write(tmp_path, good(port="/dev/cu.fromprofile", baud=115200,
                                imu={"counts_per_g": 16384, "conversion_source": SRC}))
    rc, c = _serve_cli(tmp_path, ["--profile", str(prof)])
    assert rc == 0 and c["adapter"].port_pattern == "/dev/cu.fromprofile" and c["adapter"].baud == 115200
    assert c["config"].imu.conversion["ax"][0] == pytest.approx(9.80665 / 16384)
    assert c["meta"]["hardware_profile_name"] == "bench"
    rc, c = _serve_cli(tmp_path, ["--profile", str(prof), "--port", "/dev/cu.flag*", "--baud", "460800"])
    assert c["adapter"].port_pattern == "/dev/cu.flag*" and c["adapter"].baud == 460800
    # a different sensitivity on the command line must cite its OWN source: the profile's citation is for 16384
    rc, c = _serve_cli(tmp_path, ["--profile", str(prof), "--imu-counts-per-g", "8192"])
    assert rc == 2 and not c
    rc, c = _serve_cli(tmp_path, ["--profile", str(prof), "--imu-counts-per-g", "8192",
                                  "--imu-conversion-source", "LSM6DS3 datasheet table 3, +/-4 g"])
    assert c["config"].imu.conversion["ax"][0] == pytest.approx(9.80665 / 8192)
    assert c["config"].imu.conversion_source == "LSM6DS3 datasheet table 3, +/-4 g"
    assert c["meta"]["effective_serial"]["counts_per_g"] == 8192 and \
        c["meta"]["effective_serial"]["conversion_source"].startswith("LSM6DS3")


def test_a_bad_or_missing_profile_exits_2_before_the_data_dir_is_created(serve_env, tmp_path, capsys):
    for argv in (["--profile", str(tmp_path / "nope.json")],
                 ["--profile", str(write(tmp_path, good(emg={"output": "envelope"}), "env.json"))],
                 ["--profile", str(write(tmp_path, good(), "noport.json"))]):
        rc, calls = _serve_cli(tmp_path, argv)
        assert rc == 2 and not calls
    assert not any(p.name.startswith("data") for p in tmp_path.iterdir())
    assert "hardware profile" in capsys.readouterr().err


def test_a_conversion_flag_without_a_source_is_a_clean_error(serve_env, tmp_path, capsys):
    rc, calls = _serve_cli(tmp_path, ["--device", "serial", "--port", "/dev/x", "--baud", "115200",
                                      "--imu-counts-per-g", "4096"])
    assert rc == 2 and "source" in capsys.readouterr().err


def test_device_none_with_a_profile_starts_no_device(serve_env, tmp_path):
    prof = write(tmp_path, good(port="/dev/cu.x"))
    rc, calls = _serve_cli(tmp_path, ["--device", "none", "--profile", str(prof)])
    assert rc == 0 and "adapter" not in calls


def test_baud_zero_on_the_command_line_is_refused(serve_env, tmp_path):
    rc, _ = _serve_cli(tmp_path, ["--device", "serial", "--port", "/dev/x", "--baud", "0"])
    assert rc == 2


def test_negative_baud_on_the_command_line_is_refused(serve_env, tmp_path):
    rc, _ = _serve_cli(tmp_path, ["--device", "serial", "--port", "/dev/x", "--baud", "-5"])
    assert rc == 2


@pytest.mark.parametrize("value", ["inf", "nan", "-16384"])
def test_bad_sensitivity_flags_are_clean_errors(serve_env, tmp_path, value):
    argv = ["--device", "serial", "--port", "/dev/cu.nonexistent", "--imu-counts-per-g", value,
            "--imu-conversion-source", SRC]
    try:
        rc, _ = _serve_cli(tmp_path, argv, real_runtime=True)
    except ValueError:
        rc = "traceback"
    assert rc == 2


def test_overriding_the_sensitivity_does_not_keep_the_old_citation_or_old_value(serve_env, tmp_path):
    prof = write(tmp_path, good(port="/dev/x", imu={"counts_per_g": 16384, "conversion_source": SRC}))
    rc, c = _serve_cli(tmp_path, ["--profile", str(prof), "--imu-counts-per-g", "100"])
    assert rc == 2 and not c                      # a new sensitivity must cite its own source: refused otherwise
    rc, c = _serve_cli(tmp_path, ["--profile", str(prof), "--imu-counts-per-g", "100",
                                  "--imu-conversion-source", "bench calibration, 2026-11-07"])
    used = 9.80665 / c["config"].imu.conversion["ax"][0]          # counts per g actually applied
    cited = c["config"].imu.conversion_source
    assert used == pytest.approx(100) and cited == "bench calibration, 2026-11-07" and cited != SRC
    assert c["meta"]["effective_serial"]["counts_per_g"] == 100   # what was applied is what is recorded


def test_backup_with_a_missing_profile_is_reported(serve_env, tmp_path):
    rc, _ = _serve_cli(tmp_path, ["--backup", str(ROOT / "backup" / "demo-session"),
                                  "--profile", str(tmp_path / "does-not-exist.json")])
    assert rc == 2


def test_backup_overrides_device_and_never_opens_serial(serve_env, tmp_path):
    rc, calls = _serve_cli(tmp_path, ["--backup", str(ROOT / "backup" / "demo-session"),
                                      "--device", "serial", "--port", "/dev/x"])
    assert rc == 0 and "adapter" not in calls


# ============================================================================ port glob

def test_a_glob_that_matches_nothing_names_the_pattern(tmp_path):
    from vibeguard_backend.adapters import AdapterDisconnected

    a = SerialAdapter(str(tmp_path / "cu.usbmodem*"))
    with pytest.raises(AdapterDisconnected, match=r"no port matches .*cu\.usbmodem\*"):
        a._resolve()


def test_an_ambiguous_port_glob_is_reported(tmp_path):
    (tmp_path / "cu.usbmodem1101").touch()
    (tmp_path / "cu.usbmodem2201").touch()
    a = SerialAdapter(str(tmp_path / "cu.usbmodem*"))
    chosen = a._resolve()
    assert chosen.endswith("1101")
    assert "cu.usbmodem2201" in json.dumps(a.describe())


# ============================================================================ profile in meta, manifests, backups

@pytest.fixture
def profiled_session(backend):
    """A short SIMULATED session recorded with the same runtime meta that `serve --profile` produces."""
    profile = {"name": "bench<rig>", "port": "/dev/cu.usbmodem*", "baud": 921600,
               "emg": {"output": "raw", "channels": 3},     # the SIMULATED device has 1 EMG channel
               "imu": {"model": "MPU-6050", "counts_per_g": 4096, "conversion_source": "simulator scale 4096/g (SIMULATED)"}}
    frames = sim_frames(duration_s=3)
    gate = threading.Event()
    rt = backend.add_runtime("wearable", ScriptedAdapter([frames[0], ("gate", gate), *frames[1:]], clock="device",
                                                         hold_open=True),
                             processing_config_for("serial", 4096, profile["imu"]["conversion_source"]),
                             meta={"hardware_profile": profile, "hardware_profile_name": profile["name"]})
    assert wait_until(lambda: rt.counters["records"] == 1)
    s = backend.start_session("wearable", "profiled")
    gate.set()
    assert wait_until(lambda: rt.counters["records"] == len(frames), 20)
    return rt, backend.stop_session(s["session_id"]), profile


def test_the_profile_is_recorded_in_runtime_status_session_manifest_and_backup_manifest(backend, profiled_session, tmp_path):
    rt, row, profile = profiled_session
    assert rt.status()["meta"]["hardware_profile"] == profile
    manifest = json.loads((Path(row["dir"]) / "manifest.json").read_text())
    assert manifest["runtime_meta"]["hardware_profile"] == profile
    assert manifest["processing_config"]["imu"]["conversion_source"] == profile["imu"]["conversion_source"]
    export_backup(backend, row["session_id"], tmp_path / "bk")
    bk = json.loads((tmp_path / "bk" / "manifest.json").read_text())
    assert bk["runtime_meta"]["hardware_profile_name"] == profile["name"]
    assert bk["processing_config"]["imu"]["conversion"]["ax"][0] == pytest.approx(9.80665 / 4096)


def test_a_replayed_backup_keeps_the_cited_conversion(backend, profiled_session, tmp_path):
    _, row, profile = profiled_session
    export_backup(backend, row["session_id"], tmp_path / "bk")
    r = start_backup_replay(backend, tmp_path / "bk", name="replayed", speed=1000.0)
    assert r.config.imu.conversion_source == profile["imu"]["conversion_source"]


def test_a_replayed_backup_still_says_which_hardware_profile_it_came_from(backend, profiled_session, tmp_path):
    _, row, profile = profiled_session
    export_backup(backend, row["session_id"], tmp_path / "bk")
    r = start_backup_replay(backend, tmp_path / "bk", name="replayed2", speed=1000.0)
    assert r.status()["meta"].get("hardware_profile_name") == profile["name"]


def test_profile_facts_that_contradict_the_hello_are_flagged(profiled_session):
    rt, _, _ = profiled_session
    status = json.dumps(rt.status())
    assert "mismatch" in status or "contradict" in status


# ============================================================================ preflight rate checks

def _rt_with_windows(nominal, measured, **over):
    rt = _runtime("ready", frames=100)
    rt.update({"role": "wearable", "data_fresh": True, "provenance": {"source": "live"}, "hello": None,
               "latest": {s: {"window": {"nominal_rate_hz": nominal, "measured_rate_hz": measured}}
                          for s in ("emg", "imu")}})
    rt.update(over)
    return rt


def _preflight(fake, rt):
    f = fake(**{"/api/runtimes/wearable": rt})
    return run_preflight(f.url, expect="any")


def test_preflight_rate_check_levels_and_replay_runtimes(fake):
    rep = _preflight(fake, _rt_with_windows(1000, 1005))
    assert _check(rep, "emg sample rate").level == PASS
    rep = _preflight(fake, _rt_with_windows(1000, 950))
    assert _check(rep, "emg sample rate").level == WARN
    rep = _preflight(fake, _rt_with_windows(1000, 800))
    assert _check(rep, "emg sample rate").level == FAIL
    rep = _preflight(fake, _rt_with_windows(1000, 1005, role="replay", provenance={"source": "replay", "original_source": "live"}))
    assert _check(rep, "imu sample rate").level == PASS


@pytest.mark.parametrize("latest", [None, {}, {"emg": None, "imu": None}, {"emg": {}, "imu": {"window": None}},
                                    {"emg": {"window": {"nominal_rate_hz": 0, "measured_rate_hz": 400}},
                                     "imu": {"window": {"nominal_rate_hz": 400, "measured_rate_hz": 0}}},
                                    {"emg": {"window": {"nominal_rate_hz": None, "measured_rate_hz": None}}}])
def test_preflight_survives_missing_latest_features_and_zero_rates(fake, latest):
    rep = _preflight(fake, _rt_with_windows(1000, 1000, latest=latest))
    assert not any(c.name.endswith("sample rate") for c in rep.checks)   # skipped, not divided by zero


def test_preflight_rate_thresholds_match_serial_check(fake):
    rep = _preflight(fake, _rt_with_windows(1000, 930))      # 7% off
    assert _check(rep, "emg sample rate").level == FAIL


# ============================================================================ status page (needs node)

NODE = shutil.which("node")
DRIVER = r"""
const fs=require('fs');const [,, pagePath, rtPath]=process.argv;
const rt=JSON.parse(fs.readFileSync(rtPath));const html={},text={},els={};
const el=id=>els[id]||(els[id]={style:{},set innerHTML(v){html[id]=v},get innerHTML(){return html[id]||''},
  set textContent(v){text[id]=v},get textContent(){return text[id]}});
global.document={getElementById:el};global.setInterval=()=>0;
global.fetch=async u=>({ok:true,status:200,json:async()=>u.startsWith('/api/sessions')?[]:rt});
const src=fs.readFileSync(pagePath,'utf8').replace('tick();setInterval(tick,1000);','');
eval(src+';globalThis.tick=tick;');
tick().then(()=>setTimeout(()=>console.log(JSON.stringify({html,text})),30));
"""
needs_node = pytest.mark.skipif(NODE is None, reason="node is needed to run the status page script")
XSS = '<img src=x onerror="window.__pwn=1">'


def render_page(tmp_path, rt):
    import re
    page = tmp_path / "page.js"
    page.write_text(re.search(r"<script>(.*)</script>", STATUS_PAGE, re.S).group(1))
    (tmp_path / "rt.json").write_text(json.dumps(rt))
    (tmp_path / "driver.js").write_text(DRIVER)
    out = subprocess.run([NODE, str(tmp_path / "driver.js"), str(page), str(tmp_path / "rt.json")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _status(**over):
    st = {"runtime": "wearable", "state": "ready", "error": None, "provenance": {"source": "live"},
          "last_rx_age_s": 0.1, "data_fresh": True, "calibration": {"state": "uncalibrated", "reason": None},
          "active_session_id": None, "counters": {}, "events": {}, "latest": {}, "meta": {}, "hello": None}
    st.update(over)
    return st


@needs_node
def test_hostile_hello_and_profile_name_never_reach_innerhtml_unescaped(tmp_path):
    hello = {"firmware": {"name": XSS, "version": "<script>window.__pwn=2</script>"}, "hardware": {"board": XSS},
             "streams": {"emg": {"nominal_rate_hz": 1000, "channels": [XSS], "raw_limits": {"min": "<b>", "max": 4095},
                                 "sensor_model": XSS},
                         "imu": {"nominal_rate_hz": None, "channels": ["ax"], "raw_limits": None, "sensor_model": None}}}
    page = render_page(tmp_path, _status(hello=hello, meta={"hardware_profile_name": XSS + "\nline2"}))
    inner = page["html"]["facts"]
    assert "<img" not in inner and "<script" not in inner and "<b>" not in inner and "&lt;img" in inner
    assert "unresolved" in inner                                      # null facts are shown as unresolved
    head = page["text"]["facts_head"]                                 # textContent: shown literally, never parsed
    assert XSS in head and "hardware profile:" in head


@needs_node
def test_hostile_session_label_and_counter_names_are_escaped(tmp_path):
    page = render_page(tmp_path, _status(counters={XSS: 1}, events={"<script>": 2}))
    assert "<img" not in page["html"]["counters"] and "<script" not in page["html"]["counters"]


@needs_node
def test_error_banner_shows_the_error_text_literally(tmp_path):
    err = "ValueError: \"port\" must be None or a string, not <class 'list'>"
    page = render_page(tmp_path, _status(state="error", error=err))
    assert err in page["text"]["banner"]


@needs_node
def test_score_with_no_conversion_is_not_displayed_as_a_zero(tmp_path):
    score = {"status": "paused", "cumulative": {"value": 0.0}, "reasons": ["vibration_no_conversion"]}
    page = render_page(tmp_path, _status(latest={"score": score}))
    assert "0.0000" not in page["text"]["score"]
