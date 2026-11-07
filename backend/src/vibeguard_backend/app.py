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
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import __version__
from .hub import CHANNELS, DEFAULT_CHANNELS, utc_now_iso
from .runtime import RuntimeConflict, RuntimeGone
from .service import Backend, BadRequest, NotFound
from .store import IdempotencyConflict


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking service call off the event loop and map errors to HTTP codes."""
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (BadRequest, ValueError, TypeError, sqlite3.InterfaceError, sqlite3.ProgrammingError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except (RuntimeConflict, RuntimeGone, IdempotencyConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (cf.TimeoutError, TimeoutError) as exc:
        raise HTTPException(503, "the device runtime did not answer in time") from exc


def create_app(backend: Backend, on_shutdown: Callable[[], None] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if on_shutdown:
            on_shutdown()

    app = FastAPI(title="VibeGuard backend", version=__version__, lifespan=lifespan)
    app.state.backend = backend

    @app.exception_handler(Exception)
    async def unexpected(_request, exc: Exception):  # never leak a traceback to the dashboard
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": f"internal error: {type(exc).__name__}"}, status_code=500)
    # The dashboard runs on another localhost port during development.
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
                       allow_methods=["*"], allow_headers=["*"])
    b = backend

    @app.get("/api/health")
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

    @app.delete("/api/runtimes/{name}")
    async def remove_runtime(name: str) -> dict:
        rt = await _call(b.runtime, name)
        if rt.role != "replay":
            raise HTTPException(409, "only replay runtimes can be removed while the backend runs")
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

    @app.post("/api/station/sessions/{sid}/complete")
    async def station_complete(sid: str, body: dict = Body(default={})) -> dict:
        return await _call(b.finish_station_session, sid, "completed", body.get("reason"),
                           body.get("request_id"))

    @app.post("/api/station/sessions/{sid}/abort")
    async def station_abort(sid: str, body: dict = Body(default={})) -> dict:
        return await _call(b.finish_station_session, sid, "aborted", body.get("reason", "aborted_by_operator"),
                           body.get("request_id"))

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
        await ws.accept()
        sub = b.hub.subscribe(wanted, runtimes)
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
    """Consume (and ignore) client messages so a disconnect is noticed promptly."""
    try:
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return
