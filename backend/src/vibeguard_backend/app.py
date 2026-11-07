"""HTTP + WebSocket API (FastAPI). Local only; no authentication by design.

Endpoints (all JSON; see backend/README.md and backend/examples/ for payloads):

  GET  /api/health
  GET  /api/runtimes                         device sources and their host state
  GET  /api/runtimes/{name}                  status + latest values (snapshot)
  DELETE /api/runtimes/{name}                stop a replay
  POST /api/runtimes/{name}/calibration      {"action": "relaxed"|"squeeze"|"cancel"}
  POST /api/runtimes/{name}/simulate-disconnect  {"seconds": 3}   (simulated device only)
  POST /api/sessions                         {"runtime": "wearable", "label": "..."}
  POST /api/sessions/{id}/stop
  GET  /api/sessions[?source=&state=]        wearable sessions
  GET  /api/sessions/{id}
  GET  /api/sessions/{id}/files/{name}       telemetry.ndjson | derived.ndjson | ...
  POST /api/sessions/{id}/replay             {"speed": 1.0}
  POST /api/station/sessions                 {"source": "live", "label": "..."}
  POST /api/station/sessions/{id}/trials     {"stimulus": {...}, "response": "felt"|"not_felt"|"missing"}
  POST /api/station/sessions/{id}/complete | /abort
  GET  /api/station/sessions, /api/station/sessions/{id}
  GET  /api/history[?source=]                wearable + station, newest first, provenance-labelled
  GET  /api/devices, /api/calibrations
  WS   /ws/live?channels=derived,runtime,...&runtime=wearable

Every POST accepts "request_id": a retry with the same id returns the first result.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .hub import CHANNELS, DEFAULT_CHANNELS, utc_now_iso
from .runtime import RuntimeConflict, RuntimeGone
from .service import Backend, BadRequest, NotFound
from .station import StationError
from .status_page import CSP, STATUS_PAGE
from .store import IdempotencyConflict


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking service call off the event loop and map errors to HTTP codes."""
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (BadRequest, ValueError, TypeError, OverflowError, sqlite3.InterfaceError,
            sqlite3.ProgrammingError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except StationError as exc:  # the station controller refused or failed
        raise HTTPException(502, str(exc)) from exc
    except (RuntimeConflict, RuntimeGone, IdempotencyConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (cf.TimeoutError, TimeoutError) as exc:
        raise HTTPException(503, "the device runtime did not answer in time") from exc


LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
MAX_BODY_BYTES = 1_000_000
_HOSTPORT = re.compile(r"^(?P<host>\[[0-9a-f:.]+\]|[a-z0-9]([a-z0-9.-]*[a-z0-9])?)(?::(?P<port>\d{1,5}))?$")


def _split_hostport(value: str) -> tuple[str, str | None] | None:
    """('host', 'port'|None) from a Host header or an Origin's netloc; None if malformed.
    Anything with userinfo, a path or a non-numeric port is malformed."""
    m = _HOSTPORT.match(value.strip().lower())
    return (m.group("host"), m.group("port")) if m else None


def _host_only(host_header: str) -> str:
    parsed = _split_hostport(host_header or "")
    return parsed[0] if parsed else "\0malformed"


def _origin_host(origin: str) -> tuple[str, str | None] | None:
    from urllib.parse import urlsplit
    try:
        u = urlsplit(origin)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or u.username or u.password or u.path not in ("", "/") \
            or u.query or u.fragment or not u.netloc:
        return None
    return _split_hostport(u.netloc)


def create_app(backend: Backend, on_shutdown: Callable[[], None] | None = None,
               allowed_hosts: tuple[str, ...] | None = LOCAL_HOSTS) -> FastAPI:
    """``allowed_hosts``: Host header values accepted (defends against DNS rebinding); ports are
    ignored. Pass None to disable the Host check (e.g. when deliberately listening on all
    interfaces); the Origin check then only accepts local origins and the page's own origin."""
    hosts = None if allowed_hosts is None else {_host_only(h) for h in allowed_hosts}
    local = {"localhost", "127.0.0.1", "[::1]"}

    def trusted_origin(origin: str | None, host_header: str | None) -> bool:
        """A request that carries an Origin (every browser cross-site request does) must come from
        a local page, from the page this very server served (same origin), or from an allowed host."""
        if not origin:
            return True  # not a browser (curl, scripts, the dashboard's own server-side calls)
        parsed = _origin_host(origin)
        if parsed is None:
            return False  # 'null', file://, userinfo tricks, garbage
        host, port = parsed
        if host in local or (hosts is not None and host in hosts):
            return True
        own = _split_hostport(host_header or "")  # same origin: the page served by this very server
        return own is not None and own == (host, port)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if on_shutdown:
            on_shutdown()

    app = FastAPI(title="VibeGuard backend", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)  # the bundled docs page loads assets from a CDN: offline it is blank
    app.state.backend = backend

    from fastapi.exceptions import RequestValidationError
    from fastapi.requests import Request

    def _cors(request: Request) -> dict:
        origin = request.headers.get("origin")
        return {"access-control-allow-origin": origin, "vary": "Origin"} if origin and trusted_origin(
            origin, request.headers.get("host")) else {}

    @app.exception_handler(RequestValidationError)
    async def bad_body(request: Request, exc: RequestValidationError):
        # never echo the input: it can hold NaN/Infinity, which JSON cannot serialise
        errs = [{"loc": [str(x) for x in e.get("loc", ())], "msg": e.get("msg", "invalid")} for e in exc.errors()]
        return JSONResponse({"detail": errs}, status_code=422, headers=_cors(request))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):  # never leak a traceback to the dashboard
        return JSONResponse({"detail": f"internal error: {type(exc).__name__}"}, status_code=500,
                            headers=_cors(request))
    # The dashboard runs on another localhost port during development.
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
                       allow_methods=["*"], allow_headers=["*"])
    b = backend

    @app.middleware("http")
    async def local_only(request, call_next):
        """Reject what a hostile web page could send to localhost: a foreign Origin (CSRF), an
        unexpected Host (DNS rebinding), or a huge body."""
        if hosts is not None and _host_only(request.headers.get("host", "")) not in hosts:
            return JSONResponse({"detail": "host not allowed"}, status_code=403)
        if not trusted_origin(request.headers.get("origin"), request.headers.get("host")):
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": f"body larger than {MAX_BODY_BYTES} bytes"}, status_code=413)
        if request.method in ("POST", "PUT", "PATCH") and not length and \
                "chunked" in request.headers.get("transfer-encoding", "").lower():
            return JSONResponse({"detail": "chunked request bodies are not accepted"}, status_code=411)
        return await call_next(request)

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    async def home() -> HTMLResponse:
        return HTMLResponse(STATUS_PAGE, headers={"Content-Security-Policy": CSP,
                                                  "X-Content-Type-Options": "nosniff"})

    @app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    async def docs() -> HTMLResponse:
        """An offline API reference built from the routes (the stock Swagger page needs a CDN)."""
        import html
        rows = []
        for r in app.routes:
            if getattr(r, "include_in_schema", True) is False or not hasattr(r, "methods"):
                continue
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                doc = (r.endpoint.__doc__ or "").strip().splitlines()
                rows.append(f"<tr><td><code>{m}</code></td><td><code>{html.escape(r.path)}</code></td>"
                            f"<td>{html.escape(doc[0] if doc else '')}</td></tr>")
        body = ("<!doctype html><meta charset=utf-8><title>VibeGuard API</title>"
                "<style>body{font:15px system-ui;margin:24px;max-width:900px}td{padding:3px 12px 3px 0}"
                "code{font:13px ui-monospace,monospace}</style><h1>VibeGuard backend API</h1>"
                "<p>Full description: <a href='/openapi.json'>/openapi.json</a>; examples and the WebSocket "
                "(<code>/ws/live</code>) are in backend/README.md.</p><table>" + "".join(rows) + "</table>")
        return HTMLResponse(body, headers={"Content-Security-Policy": CSP})

    @app.api_route("/api/health", methods=["GET", "HEAD"])
    async def health() -> dict:
        return {"ok": True, "version": __version__, "data_dir": str(b.data_dir),
                "runtimes": {n: rt.host_state for n, rt in list(b.runtimes.items())},
                "recovered_on_start": b.recovered, "ws_clients": b.hub.subscriber_count}

    # ---------------------------------------------------------------- runtimes

    @app.get("/api/runtimes")
    async def runtimes() -> list[dict]:
        return b.runtime_statuses()

    @app.get("/api/runtimes/{name}")
    async def runtime(name: str) -> dict:
        return (await _call(b.runtime, name)).snapshot()

    @app.post("/api/runtimes/{name}/restart")
    async def restart_runtime(name: str) -> dict:
        """Play a finished backup fallback again from its first frame."""
        return await _call(b.restart_backup, name)

    @app.delete("/api/runtimes/{name}")
    async def remove_runtime(name: str) -> dict:
        rt = await _call(b.runtime, name)
        if rt.role != "replay":
            raise HTTPException(409, "only replay runtimes can be removed while the backend runs")
        if rt.meta.get("backup"):
            raise HTTPException(409, "this is the backup fallback: removing it would leave no wearable; "
                                     "use POST /api/runtimes/{name}/restart to play it again")
        return await _call(b.remove_runtime, name)

    @app.post("/api/runtimes/{name}/calibration")
    async def calibrate(name: str, body: dict = Body(...)) -> dict:
        return await _call(b.calibrate, name, body.get("action"), body.get("request_id"))

    @app.post("/api/runtimes/{name}/simulate-disconnect")
    async def simulate_disconnect(name: str, body: dict = Body(default={})) -> dict:
        seconds = body.get("seconds", 3.0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 60:
            raise HTTPException(400, "seconds must be a number in (0, 60]")
        rt = await _call(b.runtime, name)
        return await _call(rt.inject_disconnect, float(seconds))

    # ---------------------------------------------------------------- wearable sessions

    @app.post("/api/sessions", status_code=201)
    async def start_session(body: dict = Body(default={})) -> dict:
        return await _call(b.start_session, body.get("runtime", "wearable"), body.get("label"),
                           body.get("request_id"))

    @app.post("/api/sessions/{sid}/stop")
    async def stop_session(sid: str, body: dict = Body(default={})) -> dict:
        return await _call(b.stop_session, sid, body.get("request_id"))

    @app.get("/api/sessions")
    async def sessions(source: str | None = None, state: str | None = None,
                       limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
        return await _call(b.store.wearable_sessions, source, state, limit)

    @app.get("/api/sessions/{sid}")
    async def session(sid: str) -> dict:
        return await _call(b.session, sid)

    @app.get("/api/sessions/{sid}/files/{name}")
    async def session_file(sid: str, name: str) -> FileResponse:
        path = await _call(b.session_file, sid, name)
        media = "application/x-ndjson" if name.endswith(".ndjson") else (
            "text/csv" if name.endswith(".csv") else "application/json")
        return FileResponse(path, media_type=media, filename=f"{sid}-{name}")

    @app.post("/api/sessions/{sid}/replay", status_code=201)
    async def replay(sid: str, body: dict = Body(default={})) -> dict:
        return await _call(b.start_replay, sid, body.get("speed", 1.0), body.get("fast", False),
                           body.get("request_id"))

    # ---------------------------------------------------------------- station

    @app.post("/api/station/sessions", status_code=201)
    async def station_start(body: dict = Body(default={})) -> dict:
        return await _call(b.start_station_session, body.get("source"), body.get("label"),
                           body.get("protocol"), body.get("wearable_session_id"), body.get("request_id"))

    @app.post("/api/station/sessions/{sid}/trials", status_code=201)
    async def station_trial(sid: str, body: dict = Body(...)) -> dict:
        fields = ("trial_index", "measured_amplitude", "is_catch", "presented_at", "responded_at",
                  "response_latency_ms", "note", "request_id")
        return await _call(b.add_trial, sid, body.get("stimulus"), body.get("response"),
                           **{k: body[k] for k in fields if k in body})

    @app.post("/api/station/sessions/{sid}/stimulus", status_code=201)
    async def station_stimulus(sid: str, body: dict = Body(...)) -> dict:
        """PROPOSED (see STATION_INTERFACE.md): deliver a stimulus and open a trial."""
        return await _call(b.station_engine.present, sid, body.get("stimulus"), body.get("is_catch", False),
                           body.get("response_timeout_s", 3.0), body.get("request_id"))

    @app.post("/api/station/sessions/{sid}/respond")
    async def station_respond(sid: str, body: dict = Body(...)) -> dict:
        return await _call(b.station_engine.respond, sid, body.get("response"), body.get("request_id"))

    @app.get("/api/station/sessions/{sid}/trial")
    async def station_open_trial(sid: str) -> dict:
        await _call(b.station_session, sid)
        return {"open_trial": b.station_engine.open_trial(sid)}

    @app.post("/api/station/sessions/{sid}/complete")
    async def station_complete(sid: str, body: dict = Body(default={})) -> dict:
        if body.get("request_id") is not None:  # same id, same result
            return await _call(b._idempotent, body.get("request_id"), f"finish_station:{sid}:completed",
                               lambda: b.station_engine.complete(sid, body.get("reason")))
        return await _call(b.station_engine.complete, sid, body.get("reason"))

    @app.post("/api/station/sessions/{sid}/abort")
    async def station_abort(sid: str, body: dict = Body(default={})) -> dict:
        return await _call(b.station_engine.abort, sid, body.get("reason"), body.get("request_id"))

    @app.get("/api/station/sessions")
    async def station_sessions(source: str | None = None) -> list[dict]:
        return await _call(b.store.station_sessions, source)

    @app.get("/api/station/sessions/{sid}")
    async def station_session(sid: str) -> dict:
        return await _call(b.station_session, sid)

    # ---------------------------------------------------------------- history & records

    @app.get("/api/history")
    async def history(source: str | None = None, limit: int = Query(200, ge=1, le=1000)) -> dict:
        return await _call(b.history, source, limit)

    @app.get("/api/devices")
    async def devices() -> list[dict]:
        return await _call(b.store.devices)

    @app.get("/api/calibrations")
    async def calibrations(device_id: str | None = None) -> list[dict]:
        return await _call(b.store.calibrations, device_id)

    # ---------------------------------------------------------------- live

    @app.websocket("/ws/live")
    async def live(ws: WebSocket, channels: str | None = None, runtime: str | None = None) -> None:
        wanted = DEFAULT_CHANNELS if not channels else frozenset(c for c in channels.split(",") if c in CHANNELS)
        runtimes = None if not runtime else frozenset(runtime.split(","))
        if not trusted_origin(ws.headers.get("origin"), ws.headers.get("host")) or (
                hosts is not None and _host_only(ws.headers.get("host", "")) not in hosts):
            await ws.close(code=1008)  # browsers do not apply CORS to WebSockets: check it here
            return
        await ws.accept()
        sub = b.hub.subscribe(wanted, runtimes, include_replays=runtimes is not None)
        try:
            # first message: current state of every runtime, so a new page is never blank
            # hub_seq: messages with a hub_seq <= this are already reflected in the snapshot
            seq = b.hub.current_seq
            await ws.send_json({"v": 1, "hub_seq": seq, "channel": "snapshot", "runtime": None,
                                "rx_wall": utc_now_iso(), "payload": {
                "runtimes": [rt.snapshot() for n, rt in list(b.runtimes.items()) if runtimes is None or n in runtimes],
                "channels": sorted(wanted)}})
            receiver = asyncio.create_task(_drain_client(ws))
            while not receiver.done():
                getter = asyncio.create_task(sub.queue.get())
                done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    await ws.send_json(getter.result())
                else:
                    getter.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            b.hub.unsubscribe(sub)

    return app


async def _drain_client(ws: WebSocket) -> None:
    """Consume (and ignore) whatever the client sends, text or binary, so a disconnect is noticed."""
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError):
        return
