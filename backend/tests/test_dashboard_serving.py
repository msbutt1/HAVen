"""The backend serves the dashboard at /dashboard/ (same origin: no CORS, no Host/Origin surprises)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vibeguard_backend.app import DASHBOARD_CSP, create_app, find_dashboard_dir
from vibeguard_backend.service import Backend

REPO_DASH = Path(__file__).resolve().parents[2] / "dashboard"


@pytest.fixture
def be(tmp_path):
    b = Backend(tmp_path / "data")
    yield b
    b.shutdown()


def test_dashboard_is_served_with_a_strict_csp(be):
    with TestClient(create_app(be, dashboard_dir=REPO_DASH), base_url="http://localhost") as c:
        r = c.get("/dashboard/", follow_redirects=False)
        assert r.status_code == 200 and "VibeGuard dashboard" in r.text
        assert r.headers["content-security-policy"] == DASHBOARD_CSP
        assert "script-src 'self'" in DASHBOARD_CSP and "unsafe-inline'; img" in DASHBOARD_CSP
        assert "'unsafe-eval'" not in DASHBOARD_CSP and "connect-src 'self'" in DASHBOARD_CSP
        assert r.headers["x-content-type-options"] == "nosniff"
        js = c.get("/dashboard/js/backend-adapter.js")
        assert js.status_code == 200 and "BackendAdapter" in js.text
        assert c.get("/dashboard", follow_redirects=False).headers["location"] == "/dashboard/"


def test_api_and_status_page_are_unchanged_by_the_dashboard(be):
    with TestClient(create_app(be, dashboard_dir=REPO_DASH), base_url="http://localhost") as c:
        assert c.get("/api/runtimes").status_code == 200
        assert "Content-Security-Policy" in c.get("/").headers
        assert "script-src 'self'" not in c.get("/").headers["content-security-policy"]


def test_no_dashboard_when_the_folder_is_missing(be, tmp_path):
    with TestClient(create_app(be, dashboard_dir=tmp_path / "nope"), base_url="http://localhost") as c:
        assert c.get("/dashboard/").status_code == 404
    with TestClient(create_app(be), base_url="http://localhost") as c:
        assert c.get("/dashboard/").status_code == 404


def test_cross_site_requests_to_the_dashboard_are_still_refused(be):
    with TestClient(create_app(be, dashboard_dir=REPO_DASH), base_url="http://localhost") as c:
        assert c.get("/dashboard/", headers={"origin": "https://evil.example"}).status_code == 403
        assert c.get("/dashboard/", headers={"host": "evil.example"}).status_code == 403


def test_find_dashboard_dir_in_a_checkout(monkeypatch):
    monkeypatch.delenv("VG_DASHBOARD_DIR", raising=False)
    assert find_dashboard_dir() == REPO_DASH
    monkeypatch.setenv("VG_DASHBOARD_DIR", "/nonexistent")
    assert find_dashboard_dir() == REPO_DASH  # a bad override falls back to the checkout


# ---------------------------------------------------------------- password protection (for a public hostname)

def _auth(user="demo", pw="s3cret"):
    import base64
    return {"authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_basic_auth_protects_everything_including_the_websocket(be):
    app = create_app(be, dashboard_dir=REPO_DASH, basic_auth=("demo", "s3cret"))
    with TestClient(app, base_url="http://localhost") as c:
        for path in ("/", "/dashboard/", "/api/runtimes", "/api/history", "/docs"):
            r = c.get(path)
            assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"], path
        assert c.get("/api/runtimes", headers=_auth("demo", "wrong")).status_code == 401
        assert c.get("/api/runtimes", headers=_auth("other", "s3cret")).status_code == 401
        assert c.post("/api/sessions", json={}).status_code == 401           # no control without credentials
        assert c.delete("/api/runtimes/wearable").status_code == 401
        assert c.get("/api/runtimes", headers=_auth()).status_code == 200
        # an app that handles the password itself gets a plain 401, without the challenge iOS would hold
        r = c.get("/api/health", headers={"x-vibeguard-client": "expo"})
        assert r.status_code == 401 and "www-authenticate" not in r.headers
        assert "VibeGuard dashboard" in c.get("/dashboard/", headers=_auth()).text
        from starlette.testclient import WebSocketDenialResponse
        for bad in ({}, _auth("demo", "wrong")):
            with pytest.raises(WebSocketDenialResponse) as denied:   # a challenge, so browsers retry with the password
                with c.websocket_connect("/ws/live", headers=bad):
                    pass
            assert denied.value.status_code == 401 and "Basic" in denied.value.headers["www-authenticate"]
        with c.websocket_connect("/ws/live", headers=_auth()) as ws:
            assert ws.receive_json()["channel"] == "snapshot"


def test_no_auth_configured_means_no_challenge(be):
    with TestClient(create_app(be), base_url="http://localhost") as c:
        assert c.get("/api/runtimes").status_code == 200
