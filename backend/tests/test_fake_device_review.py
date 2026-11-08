"""Adversarial review of FakeDevice and the fake-device CLI (pseudo-terminal "firmware").

Passing tests record behaviour that was measured and works. Each xfail(strict=True) reproduces a defect:
when someone fixes it the test turns into an XPASS failure and the marker must be removed.
Everything is offline and runs on a pty; macOS note: only some baud rates can be set on a pty (115200, 230400).
Tests that could hang run the risky call in a child process with a hard timeout.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

serial = pytest.importorskip("serial")

from vibeguard_signals import contract  # noqa: E402
from vibeguard_backend import hardware  # noqa: E402
from vibeguard_backend.hardware import FakeDevice, serial_check  # noqa: E402

pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs pseudo-terminals")


# ------------------------------------------------------------------------------ helpers

@pytest.fixture
def make():
    made = []

    def _make(**kw):
        d = FakeDevice(**kw)
        made.append(d)
        return d.start()
    yield _make
    for d in made:
        d.stop()


def drain(s, secs):
    out, end = b"", time.monotonic() + secs
    while time.monotonic() < end:
        out += s.read(1 << 20)
    return out


def records(raw: bytes):
    out = []
    for line in raw.split(b"\n"):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def check(report, name):
    found = [c for c in report.checks if c.name == name]
    assert found, f"no check named {name!r}: {[c.name for c in report.checks]}"
    return found[0]


def nfds() -> int:
    return len(os.listdir("/dev/fd"))


def child(code: str, timeout: float):
    """Run python code in a child; (returncode or None if it had to be killed, stdout, stderr)."""
    import tempfile
    # output goes to files and a killed child is not waited for: a process stuck in close(2) on the pty can
    # stay unreapable for a long time, and waiting for it would hang the test run too
    with tempfile.TemporaryFile("w+") as fo, tempfile.TemporaryFile("w+") as fe:
        p = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=fo, stderr=fe, text=True)
        try:
            p.wait(timeout=timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            p.kill()
            rc = None
        fo.seek(0)
        fe.seek(0)
        return rc, fo.read(), fe.read()


def cli(args, wait: float):
    """Start `vg-backend fake-device`; returns (process, first line after the banner text so far)."""
    code = "import sys; from vibeguard_backend.cli import main; sys.exit(main(['fake-device'] + sys.argv[1:]))"
    p = subprocess.Popen([sys.executable, "-u", "-c", code] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
    time.sleep(wait)
    return p


# ------------------------------------------------------------------------------ lifecycle: what works

def test_stop_twice_and_stop_with_a_host_connected_that_is_reading(make):
    d = make(speed=5)
    s = serial.Serial(d.path, 115200, timeout=0.1)
    assert records(drain(s, 0.5))[0]["type"] == "hello"
    t = time.monotonic()
    d.stop()
    assert time.monotonic() - t < 2 and not d._thread.is_alive()
    d.stop()  # second stop is a no-op
    s.close()


def test_start_stop_cycles_leak_neither_fds_nor_threads():
    base_fds = nfds()
    paths = set()
    for _ in range(10):
        d = FakeDevice(speed=5).start()
        paths.add(d.path)
        d.stop()
    assert nfds() <= base_fds  # nothing leaked (lower is fine: earlier tests' sockets may be collected meanwhile)
    # the pty name is recycled: a stale `serve` pointed at the old name reconnects to the NEW device
    assert len(paths) <= 2


def test_host_that_leaves_and_comes_back_after_a_pause_gets_a_new_boot_with_hello(make):
    d = make(speed=5)
    s = serial.Serial(d.path, 115200, timeout=0.1)
    first = [r for r in records(drain(s, 0.5)) if r["type"] == "hello"]
    s.close()
    time.sleep(0.4)
    assert d._thread.is_alive()  # one thread serves all boots: no thread per connection
    s = serial.Serial(d.path, 115200, timeout=0.1)
    second = records(drain(s, 0.5))
    s.close()
    assert first and second[0]["type"] == "hello" and second[0]["boot_id"] != first[0]["boot_id"]


def test_nothing_is_sent_before_a_host_opens_the_port_on_macos_style_ptys(make):
    d = make(speed=5)
    time.sleep(0.6)
    assert d.bytes_written == 0 and d.frames_written == 0


def test_loop_wrap_is_a_reboot_with_a_new_boot_id(make):
    """With --loop (the default) the end of every run (30 s in the CLI) is a reboot the host sees mid-session.
    serial-check --seconds 60 therefore always shows 'device rebooted': expected, but undocumented."""
    d = make(speed=50, duration_s=1.0, loop=True)
    s = serial.Serial(d.path, 115200, timeout=0.1)
    hellos = [r["boot_id"] for r in records(drain(s, 1.5)) if r["type"] == "hello"]
    s.close()
    assert len(set(hellos)) >= 3


# ------------------------------------------------------------------------------ pacing / faults: what works

def test_hang_after_and_unplug_after_are_measured_from_the_host_connecting(make):
    d = make(speed=5, hang_after_s=1.0)
    time.sleep(0.8)  # the device existed for a while before the host came
    s = serial.Serial(d.path, 115200, timeout=0.05)
    t0, last = time.monotonic(), None
    while time.monotonic() - t0 < 2.5:
        if s.read(1 << 20):
            last = time.monotonic() - t0
    assert last is not None and 0.8 <= last <= 1.6, last  # data stops ~1 s after the connection, link stays up
    s.close()

    u = make(speed=5, unplug_after_s=1.0)
    time.sleep(0.6)
    s = serial.Serial(u.path, 115200, timeout=0.05)
    t0, err_at = time.monotonic(), None
    while time.monotonic() - t0 < 3:
        try:
            s.read(1 << 20)
        except Exception:
            err_at = time.monotonic() - t0
            break
    assert err_at is not None and 0.8 <= err_at <= 2.0, err_at
    u._thread.join(2)
    assert not u._thread.is_alive() and u.master == -1  # the cable is pulled: the port is gone
    s.close()


def test_baud_limit_throttles_to_baud_over_ten_bytes_per_second(make):
    d = make(speed=50, baud_limit=115200)
    s = serial.Serial(d.path, 115200, timeout=0.05)
    t0, n = time.monotonic(), 0
    while time.monotonic() - t0 < 2.0:
        n += len(s.read(1 << 20))
    s.close()
    expected = 115200 / 10 * 2.0
    assert 0.8 * expected <= n <= 1.2 * expected + 3000, (n, expected)


def test_rate_error_up_to_ten_percent_still_produces_contract_valid_frames(make):
    d = make(speed=50, duration_s=2.0, loop=False, rate_error=0.10)
    s = serial.Serial(d.path, 115200, timeout=0.1)
    raw = drain(s, 1.5)
    s.close()
    lines = [ln for ln in raw.split(b"\n") if ln.strip()]
    assert lines
    for ln in lines:
        _, issues = contract.parse_line(ln)
        assert not issues, issues


def test_rate_error_shows_up_as_an_emg_rate_warning_in_serial_check(make):
    d = make(speed=5, rate_error=0.05)
    r = serial_check(d.path, 230400, 3)
    assert check(r.report, "emg sample rate").level == "WARN"


def test_garbage_and_silent_modes_fail_the_expected_checks(make):
    g = make(mode="garbage")
    r = serial_check(g.path, 230400, 1.5)
    assert check(r.report, "contract frames").level == "FAIL"
    z = make(mode="silent")
    r = serial_check(z.path, 230400, 1.5)
    assert check(r.report, "data arrives").level == "FAIL"


def test_drop_hello_gives_a_hello_warning_not_a_failure(make):
    d = make(speed=5, drop_hello=True)
    r = serial_check(d.path, 230400, 2)
    assert check(r.report, "hello received").level == "WARN"
    assert check(r.report, "contract frames").level == "PASS"


def test_cli_ctrl_c_exits_cleanly_and_prints_the_port():
    p = cli([], 1.5)
    banner_start = time.monotonic()
    p.send_signal(signal.SIGINT)
    rc = p.wait(8)
    out, err = p.communicate()
    assert rc == 0 and "/dev/" in out and "Traceback" not in err
    assert time.monotonic() - banner_start < 8


# ------------------------------------------------------------------------------ defects (xfail, strict)

def test_stop_returns_when_the_host_holds_the_port_but_never_reads():
    code = ("import time, serial\n"
            "from vibeguard_backend.hardware import FakeDevice\n"
            "d = FakeDevice(speed=5).start()\n"
            "s = serial.Serial(d.path, 115200, timeout=0.1)\n"
            "time.sleep(1.0)\n"
            "d.stop()\n"
            "print('STOPPED')\n")
    rc, out, _ = child(code, 5)
    assert rc == 0 and "STOPPED" in out, "stop() did not return within 5 s"


def test_hang_after_longer_than_one_run_still_fires(make):
    d = make(speed=1.0, duration_s=1.0, hang_after_s=1.5)
    s = serial.Serial(d.path, 115200, timeout=0.05)
    t0, last = time.monotonic(), None
    while time.monotonic() - t0 < 3.5:
        if s.read(1 << 20):
            last = time.monotonic() - t0
    s.close()
    assert last is not None and last < 3.0, f"still receiving data at {last:.1f} s"


@pytest.mark.xfail(strict=True, reason="DEFECT: a host that reopens within ~50 ms is not noticed (the write that "
                                       "detects a closed port never fails), so no new boot/hello is sent; the stream "
                                       "continues mid-run. A real board resets on open")
def test_quick_reopen_always_starts_a_new_boot_with_hello():
    firsts = []
    for _ in range(4):
        d = FakeDevice(speed=1.0).start()
        try:
            s = serial.Serial(d.path, 115200, timeout=0.1)
            time.sleep(0.15)
            s.read(1 << 20)
            s.close()
            s = serial.Serial(d.path, 115200, timeout=0.1)
            time.sleep(0.2)
            recs = records(s.read(1 << 20))
            s.close()
            firsts.append(recs[0]["type"] if recs else None)
        finally:
            d.stop()
    assert firsts == ["hello"] * 4, firsts


def test_reopening_a_hung_device_gives_a_fresh_boot(make):
    d = make(speed=5, hang_after_s=0.5)
    s = serial.Serial(d.path, 115200, timeout=0.1)
    drain(s, 1.2)
    s.close()
    time.sleep(0.5)
    s = serial.Serial(d.path, 115200, timeout=0.1)
    recs = records(drain(s, 1.5))
    s.close()
    assert recs and recs[0]["type"] == "hello"


def test_rate_error_beyond_the_contract_tolerance_is_refused():
    with pytest.raises(ValueError):
        FakeDevice(rate_error=0.2).stop()


def test_rate_error_also_skews_the_imu_clock(make):
    d = make(speed=5, rate_error=0.05)
    r = serial_check(d.path, 230400, 3)
    assert check(r.report, "imu sample rate").level != "PASS"


@pytest.mark.parametrize("kw", [{"speed": 0.0}, {"speed": -1.0}, {"baud_limit": -5}, {"scenario": "nope"}])
def test_nonsense_parameters_are_refused_at_construction(kw):
    d = None
    try:
        with pytest.raises(ValueError):
            d = FakeDevice(**kw)
    finally:
        if d is not None:
            d.stop() if d._thread.is_alive() else d._close_master()


def test_cli_rejects_speed_zero():
    p = cli(["--speed", "0"], 1.0)
    try:
        rc = p.wait(3)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = None
    assert rc == 2


def test_stop_before_start_is_safe_and_closes_the_fds():
    base = nfds()
    d = FakeDevice()
    try:
        d.stop()
    except RuntimeError:
        pass
    assert nfds() == base


@pytest.mark.xfail(strict=True, reason="KNOWN LIMITATION (Linux): a write to a pty master succeeds even before any "
                   "program has opened the port, so the fake device cannot tell when a host connects and starts its "
                   "first boot at once; the first hello may be missed (a reconnect gets a fresh boot). Documented in "
                   "HARDWARE_BRINGUP.md")
def test_no_boot_starts_without_a_host_when_master_writes_always_succeed(monkeypatch):
    d = FakeDevice(speed=5)
    real_write = os.write

    def linux_like(fd, data):
        return len(data) if fd == d.master else real_write(fd, data)
    monkeypatch.setattr(hardware.os, "write", linux_like)
    try:
        d.start()
        time.sleep(0.6)
        assert d.frames_written == 0 and d.bytes_written == 0
    finally:
        d.stop()


def test_serial_check_on_a_pty_with_an_unsupported_baud_explains_itself(make):
    d = make(speed=5)
    r = serial_check(d.path, 921600, 1)
    c = check(r.report, "port opens")
    if c.level == "PASS":
        pytest.skip("this platform's pty accepts 921600")
    assert "baud" in (c.detail + c.fix).lower()


def test_cli_banner_does_not_suggest_a_baud_the_guide_says_is_too_slow():
    p = cli([], 1.5)
    p.send_signal(signal.SIGINT)
    p.wait(8)
    out, _ = p.communicate()
    assert "--baud 115200" not in out
