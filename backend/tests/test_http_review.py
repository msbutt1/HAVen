"""Round 3 review of the request protections, the API surface and the status page.

Every ``xfail(strict=True)`` test documents a reproduced defect; fix it and the test starts to
XPASS, which strict mode reports as a failure, so the marker is then removed. Tests without a
marker pin behaviour that was probed and found correct. Offline, deterministic, SIMULATED data.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from vibeguard_backend.app import create_app
from vibeguard_backend.cli import main as cli_main
from vibeguard_backend.status_page import STATUS_PAGE


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend), base_url="http://localhost",
                    raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------- error handling


@pytest.mark.parametrize("body", [b"NaN", b"Infinity", b"[NaN]"])
def test_non_finite_json_body_is_a_client_error(client, body):
    r = client.post("/api/sessions", content=body, headers={"content-type": "application/json"})
    assert r.status_code in (400, 422), r.text


def test_internal_error_response_is_readable_cross_origin(backend, client):
    def boom():
        raise RuntimeError("boom")

    backend.store.devices = boom
    r = client.get("/api/devices", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_internal_error_does_not_leak_details(backend, client):
    def boom():
        raise RuntimeError("secret-path /etc/passwd")

    backend.store.devices = boom
    r = client.get("/api/devices")
    assert r.status_code == 500 and "secret" not in r.text and "Traceback" not in r.text


# ---------------------------------------------------------------- Host / Origin parsing


@pytest.mark.parametrize("origin", ["http://localhost:80@evil.example", "http://localhost:abc",
                                    "http://localhost:80/evil"])
def test_malformed_origin_is_refused(client, origin):
    assert client.get("/api/health", headers={"Origin": origin}).status_code == 403


def test_malformed_host_header_is_refused(client):
    assert client.get("/api/health", headers={"Host": "localhost:evil.example"}).status_code == 403


@pytest.mark.parametrize("origin", ["null", "file://", "http://localhost@evil.example",
                                    "http://localhost.evil.example", "https://evil.example",
                                    "http://127.0.0.1.evil.example:8765"])
def test_foreign_and_opaque_origins_are_refused(client, origin):
    assert client.get("/api/health", headers={"Origin": origin}).status_code == 403
    r = client.post("/api/station/sessions", json={"source": "simulated"}, headers={"Origin": origin})
    assert r.status_code == 403


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:3000", "http://[::1]:8080",
                                    "HTTP://LOCALHOST:5173", "http://localhost"])
def test_local_origins_are_accepted(client, origin):
    assert client.get("/api/health", headers={"Origin": origin}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example:8765", "localhost.evil.example", "", "127.0.0.1.evil.example"])
def test_foreign_hosts_are_refused(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("host", ["localhost:8765", "LOCALHOST", "127.0.0.1:1", "[::1]:8765"])
def test_local_hosts_are_accepted(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


def test_preflight_from_local_page_is_answered_and_foreign_is_refused(client):
    ok = client.options("/api/sessions", headers={"Origin": "http://localhost:5173",
                                                  "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": "content-type"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    bad = client.options("/api/sessions", headers={"Origin": "https://evil.example",
                                                   "Access-Control-Request-Method": "POST"})
    assert bad.status_code == 403 and "access-control-allow-origin" not in bad.headers


@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_wildcard_listen_still_refuses_cross_site_posts(backend, origin):
    app = create_app(backend, allowed_hosts=None)
    with TestClient(app, base_url="http://192.168.1.5:8765", raise_server_exceptions=False) as c:
        r = c.post("/api/station/sessions", headers={"Origin": origin})  # no body, no content-type
        assert r.status_code == 403, r.text


def test_wildcard_listen_accepts_same_origin_page(backend):
    app = create_app(backend, allowed_hosts=None)
    with TestClient(app, base_url="http://192.168.1.5:8765") as c:
        r = c.get("/api/health", headers={"Origin": "http://192.168.1.5:8765"})
        assert r.status_code == 200


# ---------------------------------------------------------------- CLI-computed host allow-list


def _cli_app(monkeypatch, tmp_path, *extra):
    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.setdefault("app", app))
    assert cli_main(["serve", "--device", "none", "--http-port", "1", "--data-dir", str(tmp_path / "d"),
                     *extra]) == 0
    return captured["app"]


def test_allow_host_with_port_is_honoured(monkeypatch, tmp_path):
    app = _cli_app(monkeypatch, tmp_path, "--host", "192.168.1.5", "--allow-host", "myhost:8765")
    try:
        with TestClient(app, base_url="http://myhost:8765") as c:
            assert c.get("/api/health").status_code == 200
    finally:
        app.state.backend.shutdown()


def test_allow_host_plain_name_and_lan_ip_are_honoured(monkeypatch, tmp_path):
    app = _cli_app(monkeypatch, tmp_path, "--host", "192.168.1.5", "--allow-host", "MyHost")
    try:
        for base in ("http://192.168.1.5:8765", "http://myhost:8765"):
            with TestClient(app, base_url=base) as c:
                assert c.get("/api/health", headers={"Origin": base}).status_code == 200
        with TestClient(app, base_url="http://evil.example:8765") as c:
            assert c.get("/api/health").status_code == 403
    finally:
        app.state.backend.shutdown()


def test_ipv6_listen_address_is_usable(monkeypatch, tmp_path):
    app = _cli_app(monkeypatch, tmp_path, "--host", "2001:db8::1")
    try:
        with TestClient(app, base_url="http://[2001:db8::1]:8765") as c:
            assert c.get("/api/health").status_code == 200
    finally:
        app.state.backend.shutdown()


# ---------------------------------------------------------------- body size / framing


def test_oversized_declared_body_is_413(client):
    r = client.post("/api/sessions", content=b"{}", headers={"content-length": "2000000",
                                                              "content-type": "application/json"})
    assert r.status_code in (413, 400)


def test_body_at_limit_is_not_a_413(client):
    big = json.dumps({"label": "a" * 900_000}).encode()
    r = client.post("/api/station/sessions", content=big, headers={"content-type": "application/json"})
    assert r.status_code != 413


# ---------------------------------------------------------------- methods and docs


@pytest.mark.parametrize("path", ["/", "/api/health"])
def test_head_is_supported(client, path):
    assert client.head(path).status_code == 200


def test_docs_page_works_offline(client):
    for path in ("/docs", "/redoc"):
        html = client.get(path).text
        assert not re.search(r"""(?:src|href)=["']https?://""", html), path


def test_status_page_has_csp_that_permits_its_own_script_and_fetch(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'unsafe-inline'" in csp and "connect-src 'self'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    # no external resources: nothing the CSP would block, nothing needing the network
    assert not re.search(r"""(?:src|href)=["']https?://""", r.text)
    assert "<script src" not in r.text


# ---------------------------------------------------------------- WebSocket against the real server


@pytest.fixture
def live_server(backend):
    import uvicorn

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(backend), host="127.0.0.1", port=port,
                                           log_level="critical", ws="websockets",
                                           loop="asyncio"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    end = time.monotonic() + 10
    while not server.started and time.monotonic() < end:
        time.sleep(0.02)
    assert server.started
    yield port
    server.should_exit = True
    t.join(10)


def test_websocket_origin_and_host_checks(live_server):
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    url = f"ws://127.0.0.1:{live_server}/ws/live"
    with connect(url, origin="http://localhost:5173") as ws:
        assert json.loads(ws.recv(5))["channel"] == "snapshot"
    for origin in ("null", "http://evil.example", "http://localhost@evil.example"):
        with pytest.raises(InvalidStatus) as ei:
            connect(url, origin=origin)
        assert ei.value.response.status_code == 403


def test_websocket_ignores_a_binary_frame(backend, live_server):
    from websockets.sync.client import connect

    with connect(f"ws://127.0.0.1:{live_server}/ws/live") as ws:
        ws.recv(5)
        ws.send(b"\x00\x01")
        time.sleep(0.5)
        try:
            ws.ping().wait(2)
            alive = True
        except Exception:  # ConnectionClosed: the server dropped us
            alive = False
    # asserted on plain values: the websockets frames must not be introspected by pytest's reporter
    assert alive
    assert backend.hub.subscriber_count == 1


# ---------------------------------------------------------------- status page script (needs node)

NODE = shutil.which("node")

_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const els = {};
const el = id => els[id] || (els[id] = {textContent: '', innerHTML: '', style: {}});
let tickNo = 0, timer = null;
const fetch = async (u) => {
  const cfg = input.ticks[Math.min(tickNo, input.ticks.length - 1)];
  const key = u.startsWith('/api/runtimes/wearable') ? 'runtime' : 'sessions';
  const c = cfg[key];
  if (c === 'reject') throw new TypeError('Failed to fetch');
  return {ok: c.status < 400, status: c.status, json: async () => c.body};
};
const ctx = vm.createContext({document: {getElementById: el}, fetch, console,
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  setTimeout: () => 0, clearTimeout: () => {},
  setInterval: f => { timer = f; return 1; }});
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
(async () => {
  const out = [];
  vm.runInContext(input.script, ctx);
  await settle();
  out.push(JSON.parse(JSON.stringify(els)));
  for (let n = 1; n < input.ticks.length; n++) {
    tickNo = n; timer(); await settle();
    out.push(JSON.parse(JSON.stringify(els)));
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _script() -> str:
    m = re.search(r"<script>(.*)</script>", STATUS_PAGE, re.S)
    assert m
    return m.group(1)


def _run_page(*ticks: dict) -> list[dict]:
    if NODE is None:
        pytest.skip("node is not installed")
    p = subprocess.run([NODE, "-e", _HARNESS], input=json.dumps({"script": _script(), "ticks": list(ticks)}),
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _runtime(**over) -> dict:
    rt = {
        "state": "ready", "provenance": {"source": "simulated"}, "last_rx_age_s": 0.05, "data_fresh": True,
        "calibration": {"state": "uncalibrated", "reason": None}, "active_session_id": None,
        "counters": {"frames": 100}, "events": {"seq_gap": 2},
        "latest": {
            "imu": {"features": {"vibration": {"physical": {"status": "ok", "combined_rms": 3.4667}}}},
            "emg": {"features": {"emg_activation": {"normalized": {"status": "unavailable"}}}},
            "score": {"status": "accumulating", "cumulative": {"value": 1.25}, "reasons": []},
        },
    }
    rt.update(over)
    return rt


# what GET /api/runtimes/{name} really returns while the device is unplugged (probed on a live server)
DISCONNECTED = _runtime(
    state="disconnected", last_rx_age_s=4.2, data_fresh=False,
    latest={"imu": {"status": "unavailable", "reason": "device_disconnected", "reasons": ["device_disconnected"]},
            "emg": {"status": "unavailable", "reason": "device_disconnected", "reasons": ["device_disconnected"]},
            "score": {"status": "unavailable", "reason": "device_disconnected",
                      "reasons": ["device_disconnected"], "last_seen": {"type": "experimental_score"}}})
SESSIONS = [{"started_at": "2026-11-07T19:00:00Z", "label": "demo", "state": "completed",
             "source": "simulated", "score": None}]


def ok(rt: dict) -> dict:
    return {"runtime": {"status": 200, "body": rt}, "sessions": {"status": 200, "body": SESSIONS}}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_status_page_survives_a_masked_score_during_disconnect():
    (tick,) = _run_page(ok(DISCONNECTED))
    assert tick["err"]["style"].get("display") != "block", "the API answered; the page must not claim otherwise"
    assert "frames" in tick["counters"]["innerHTML"]
    assert "DISCONNECTED" in tick["state"]["innerHTML"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_status_page_does_not_keep_showing_old_values_when_api_is_gone():
    first, second = _run_page(ok(_runtime()), {"runtime": "reject", "sessions": "reject"})
    assert "READY" in first["state"]["innerHTML"] and "s ago" in first["age"]["textContent"]
    assert second["err"]["style"]["display"] == "block"
    assert "READY" not in second["state"]["innerHTML"]
    assert "s ago" not in second["age"]["textContent"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_status_page_clears_measurements_when_runtime_disappears():
    gone = {"runtime": {"status": 404, "body": {"detail": "no runtime"}},
            "sessions": {"status": 200, "body": SESSIONS}}
    first, second = _run_page(ok(_runtime()), gone)
    assert first["vib"]["textContent"].startswith("3.47")
    assert second["vib"]["textContent"] == "-"
    assert "s ago" not in second["age"]["textContent"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_status_page_escapes_every_api_string():
    evil = '<img src=x onerror=alert(1)>'
    rt = _runtime(state=evil, provenance={"source": evil},
                  counters={evil: evil}, events={'"><script>1</script>': "'"},
                  active_session_id=evil, calibration={"state": evil, "reason": evil})
    sessions = [{"started_at": evil, "label": evil, "state": evil, "source": evil,
                 "score": {"status": evil, "session_au": 1}}]
    (tick,) = _run_page({"runtime": {"status": 200, "body": rt}, "sessions": {"status": 200, "body": sessions}})
    for field in ("state", "prov", "counters", "sessions"):
        html = tick[field]["innerHTML"]
        assert html, field
        assert not re.search(r"<(img|script|svg)", html), (field, html)
