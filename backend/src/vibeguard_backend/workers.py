"""Workers on a map: where each worker is (from their phone) and what their sleeve says.

A *worker* ties three things together: a name, the sleeve runtime that worker wears (optional), and the
latest position the worker's phone reported. The sleeve has no GPS and the device telemetry carries no
position, so positions arrive separately: ``POST /api/workers/{id}/position``.

Privacy (a position is personal data about where a person is):

* positions are kept **in memory only**: never written to the database, session files, backups or logs,
  and gone when the backend stops;
* only the latest position and a short trail (``TRAIL_POINTS``) are kept; a worker can be removed, or
  just stop sharing (``DELETE .../position``), at any time;
* every position carries its ``source`` (``live`` from a phone, ``simulated`` from the demo fleet), and
  the sleeve data keeps the provenance of its own stream.

Health (what the map shows next to a dot) is exactly what the sleeve pipeline already produces:
connection and freshness, activation (relative to that wearer's calibration), measured vibration when it
could be converted to m/s^2, and the EXPERIMENTAL exposure score. There are no validated limits behind these
numbers, so nothing here says a worker is safe or unsafe: ``status`` only says whether the sleeve is sending
fresh data.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from .service import BadRequest, NotFound

WORKER_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_WORKERS = 200
TRAIL_POINTS = 30
MAX_LABEL = 80
STALE_POSITION_S = 120.0   # a position older than this is drawn as "last known"
MAX_ACCURACY_M = 100_000.0
POSITION_SOURCES = ("live", "simulated")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(t * 1000) % 1000:03d}Z"


def _finite(value: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRequest(f"{name} must be a number")
    v = float(value)
    if not math.isfinite(v) or not lo <= v <= hi:
        raise BadRequest(f"{name} must be between {lo:g} and {hi:g}")
    return v


def _label(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest("label must be a string")
    value = value.strip()
    if len(value) > MAX_LABEL:
        raise BadRequest(f"label is longer than {MAX_LABEL} characters")
    if any(ord(c) < 32 for c in value):
        raise BadRequest("label must not contain control characters")
    return value or None


def _worker_id(value: Any) -> str:
    if not isinstance(value, str) or not WORKER_ID.match(value):
        raise BadRequest("worker id must be 1-64 characters: letters, digits, '.', '_' or '-'")
    return value


class WorkerRegistry:
    """Thread-safe, in-memory. ``runtimes`` is a callable returning the backend's current runtimes."""

    def __init__(self, runtimes: Callable[[], dict], clock: Callable[[], float] = time.time):
        self._runtimes = runtimes
        self._clock = clock
        self._lock = threading.Lock()
        self._workers: dict[str, dict] = {}

    # ------------------------------------------------------------------ writes

    def upsert(self, worker_id: Any, label: Any = None, runtime: Any = None) -> dict:
        wid = _worker_id(worker_id)
        label = _label(label)
        if runtime is not None and (not isinstance(runtime, str) or not 0 < len(runtime) <= 128):
            raise BadRequest("runtime must be the name of a sleeve runtime")
        with self._lock:
            w = self._workers.get(wid)
            if w is None:
                if len(self._workers) >= MAX_WORKERS:
                    raise BadRequest(f"at most {MAX_WORKERS} workers")
                w = self._workers[wid] = {"worker_id": wid, "label": label or wid, "runtime": runtime,
                                          "position": None, "trail": deque(maxlen=TRAIL_POINTS)}
            else:
                if label is not None:
                    w["label"] = label
                if runtime is not None:
                    w["runtime"] = runtime
            return self._view(w, False)

    def set_position(self, worker_id: Any, lat: Any, lon: Any, accuracy_m: Any = None, at: Any = None,
                     source: Any = "live") -> dict:
        wid = _worker_id(worker_id)
        lat_v = _finite(lat, "lat", -90.0, 90.0)
        lon_v = _finite(lon, "lon", -180.0, 180.0)
        acc = None if accuracy_m is None else _finite(accuracy_m, "accuracy_m", 0.0, MAX_ACCURACY_M)
        if source not in POSITION_SOURCES:
            raise BadRequest(f"source must be one of {POSITION_SOURCES}")
        if at is not None and (not isinstance(at, str) or len(at) > 40 or any(ord(c) < 32 for c in at)):
            raise BadRequest("at must be a short timestamp string")
        now = self._clock()
        with self._lock:
            w = self._workers.get(wid)
            if w is None:
                if len(self._workers) >= MAX_WORKERS:
                    raise BadRequest(f"at most {MAX_WORKERS} workers")
                w = self._workers[wid] = {"worker_id": wid, "label": wid, "runtime": None, "position": None,
                                          "trail": deque(maxlen=TRAIL_POINTS)}
            w["position"] = {"lat": lat_v, "lon": lon_v, "accuracy_m": acc, "at": at, "received": now,
                             "source": source}
            w["trail"].append((lat_v, lon_v, now))
            return self._view(w, False)

    def clear_position(self, worker_id: Any) -> dict:
        """The worker stops sharing: the position and the trail are forgotten at once."""
        wid = _worker_id(worker_id)
        with self._lock:
            w = self._workers.get(wid)
            if w is None:
                raise NotFound(f"no worker {wid!r}")
            w["position"] = None
            w["trail"].clear()
            return self._view(w, False)

    def remove(self, worker_id: Any) -> dict:
        wid = _worker_id(worker_id)
        with self._lock:
            if self._workers.pop(wid, None) is None:
                raise NotFound(f"no worker {wid!r}")
        return {"worker_id": wid, "removed": True}

    # ------------------------------------------------------------------ reads

    def list(self, trail: bool = False) -> list[dict]:
        with self._lock:
            return [self._view(w, trail) for w in sorted(self._workers.values(), key=lambda x: x["worker_id"])]

    def get(self, worker_id: Any, trail: bool = False) -> dict:
        wid = _worker_id(worker_id)
        with self._lock:
            w = self._workers.get(wid)
            if w is None:
                raise NotFound(f"no worker {wid!r}")
            return self._view(w, trail)

    def _view(self, w: dict, trail: bool) -> dict:  # called with the lock held
        now = self._clock()
        pos = w["position"]
        out: dict[str, Any] = {"worker_id": w["worker_id"], "label": w["label"], "runtime": w["runtime"]}
        if pos is None:
            out["position"] = None
        else:
            age = max(0.0, now - pos["received"])
            out["position"] = {"lat": pos["lat"], "lon": pos["lon"], "accuracy_m": pos["accuracy_m"],
                               "at": pos["at"], "received_at": _iso(pos["received"]), "age_s": round(age, 1),
                               "stale": age > STALE_POSITION_S, "source": pos["source"]}
            if trail:
                out["trail"] = [[la, lo] for la, lo, _ in w["trail"]]
        out["sleeve"] = self._sleeve(w["runtime"])
        out["status"] = out["sleeve"]["status"]
        return out

    def _sleeve(self, name: str | None) -> dict:
        """What the worker's sleeve runtime says right now (no runtime, no data: ``status`` says so)."""
        if not name:
            return {"status": "no_sleeve", "note": "no sleeve is assigned to this worker"}
        rt = self._runtimes().get(name)
        if rt is None:
            return {"status": "offline", "runtime": name, "note": f"no sleeve runtime named {name!r}"}
        try:
            snap = rt.snapshot()
        except Exception:  # noqa: BLE001 - one broken runtime must not blank the whole map
            return {"status": "offline", "runtime": name, "note": "the sleeve runtime could not be read"}
        latest = snap.get("latest") or {}
        emg = latest.get("emg") if isinstance(latest.get("emg"), dict) and latest["emg"].get("features") else None
        imu = latest.get("imu") if isinstance(latest.get("imu"), dict) and latest["imu"].get("features") else None
        score = latest.get("score") if isinstance(latest.get("score"), dict) and latest["score"].get("rate") else None

        def first(d: Any) -> float | None:
            if isinstance(d, dict) and d:
                v = next(iter(d.values()))
                return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
            return None

        activation = vibration = None
        if emg:
            norm = ((emg["features"].get("emg_activation") or {}).get("normalized") or {})
            if norm.get("status") == "ok":
                v = first(norm.get("display"))
                activation = None if v is None else round(v * 100, 1)
        if imu:
            phys = ((imu["features"].get("vibration") or {}).get("physical") or {})
            if phys.get("status") == "ok" and isinstance(phys.get("combined_rms"), (int, float)):
                vibration = round(float(phys["combined_rms"]), 3)
        connected = snap.get("connection") == "connected"
        fresh = bool(snap.get("data_fresh"))
        status = "ok" if connected and fresh else "stale" if connected else "offline"
        prov = (snap.get("provenance") or {}).get("source")
        cal = (snap.get("calibration") or {}).get("state")
        return {
            "status": status, "runtime": name, "device_id": snap.get("device_id"),
            "state": snap.get("state"), "connection": snap.get("connection"), "data_fresh": fresh,
            "source": prov, "calibrated": cal == "calibrated",
            "activation_pct": activation,
            "vibration_ms2": vibration,
            "score_rate_au_h": None if not score else (score["rate"].get("value") if isinstance(score["rate"], dict) else None),
            "score_total_au": None if not score else ((score.get("cumulative") or {}).get("value")),
            "last_rx_age_s": snap.get("last_rx_age_s"),
        }
