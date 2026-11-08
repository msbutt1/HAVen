"""A SIMULATED fleet for demos: several workers, each with a replayed sleeve and a wandering phone position.

Nothing here is real. Every position is labelled ``simulated`` and every sleeve is a replay of the backup
recording (``provenance.source = "replay"``), so the map can never show it as live data. Worker 1 uses the
backend's own ``wearable`` runtime (when there is one), so the Live screen and the map agree; the others get a
replay runtime of their own.
"""

from __future__ import annotations

import logging
import math
import random
import threading
from pathlib import Path

from .backup import start_backup_replay
from .service import Backend

log = logging.getLogger("vibeguard.fleet")

DEFAULT_CENTER = (41.3111, 69.2797)  # an arbitrary demo location; override with --demo-center
RADIUS_M = 250.0
STEP_S = 2.0
METERS_PER_DEG_LAT = 111_320.0


def start_demo_fleet(backend: Backend, n: int, backup: str | Path, center: tuple[float, float] = DEFAULT_CENTER,
                     seed: int = 1) -> threading.Thread:
    if not 1 <= n <= 50:
        raise ValueError("the demo fleet needs between 1 and 50 workers")
    lat0, lon0 = center
    if not (-90 <= lat0 <= 90 and -180 <= lon0 <= 180):
        raise ValueError("demo centre is not a valid latitude/longitude")
    rng = random.Random(seed)
    names = []
    for i in range(1, n + 1):
        runtime = f"fleet-{i}"
        if i == 1 and "wearable" in backend.runtimes:
            runtime = "wearable"
        elif runtime not in backend.runtimes:
            start_backup_replay(backend, backup, name=runtime, speed=0.9 + 0.05 * (i % 5))
            backend.loop_backup(runtime)
        wid = f"w{i}"
        backend.workers.upsert(wid, f"Worker {i}", runtime)
        names.append(wid)
    cos = math.cos(math.radians(lat0)) or 1e-6
    state = {w: [rng.uniform(-RADIUS_M, RADIUS_M) * 0.8, rng.uniform(-RADIUS_M, RADIUS_M) * 0.8] for w in names}

    def run() -> None:
        while not backend._loop_stop.wait(STEP_S):
            for wid in names:
                p = state[wid]
                p[0] += rng.gauss(0, 12)  # metres east / north
                p[1] += rng.gauss(0, 12)
                r = math.hypot(*p)
                if r > RADIUS_M:  # stay on the demo site
                    p[0] *= RADIUS_M / r
                    p[1] *= RADIUS_M / r
                try:
                    backend.workers.set_position(wid, lat0 + p[1] / METERS_PER_DEG_LAT,
                                                 lon0 + p[0] / (METERS_PER_DEG_LAT * cos),
                                                 accuracy_m=round(rng.uniform(6, 18), 1), source="simulated")
                except Exception:  # noqa: BLE001 - a demo helper must never take the backend down
                    log.exception("demo fleet position failed")
    # first positions at once, so the map is not empty for the first two seconds
    for wid in names:
        p = state[wid]
        backend.workers.set_position(wid, lat0 + p[1] / METERS_PER_DEG_LAT, lon0 + p[0] / (METERS_PER_DEG_LAT * cos),
                                     accuracy_m=10.0, source="simulated")
    t = threading.Thread(target=run, name="demo-fleet", daemon=True)
    t.start()
    return t
