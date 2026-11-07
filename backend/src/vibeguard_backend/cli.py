"""vg-backend: run the VibeGuard local backend.

    vg-backend serve                                   # simulated wearable (default)
    vg-backend serve --device serial --port '/dev/cu.usbmodem*' --baud 921600
    vg-backend serve --device none                     # API only (station, history, replay)
    vg-backend seed-history --days 14                  # labelled SIMULATED history
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("VG_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vg-backend", description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                   help=f"SQLite + session files (default {DEFAULT_DATA_DIR}, or $VG_DATA_DIR)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the HTTP/WebSocket API and the wearable runtime")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--http-port", type=int, default=8765)
    s.add_argument("--device", choices=("sim", "serial", "none"), default="sim")
    s.add_argument("--scenario", default="demo", help="simulated scenario (vg-sim --list)")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--duration", type=float, default=30.0,
                   help="simulated run length; the simulator then 'reboots' and continues")
    s.add_argument("--speed", type=float, default=1.0, help="simulated pacing factor")
    s.add_argument("--port", help="serial port or glob, e.g. '/dev/cu.usbmodem*'")
    s.add_argument("--baud", type=int, default=921600)
    s.add_argument("--imu-counts-per-g", type=float, default=None,
                   help="confirmed accelerometer sensitivity (counts per g) for m/s^2 output")
    s.add_argument("--imu-conversion-source", default=None,
                   help="where the sensitivity comes from (datasheet, configured range)")
    s.add_argument("--log-level", default="info")

    h = sub.add_parser("seed-history", help="add labelled SIMULATED history sessions")
    h.add_argument("--days", type=int, default=14)
    h.add_argument("--seed", type=int, default=7)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(args, "log_level", "info").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .service import Backend, processing_config_for

    if args.cmd == "seed-history":
        from .history import seed_history

        # recover=False: a running server owns recovery; seeding must never touch its sessions
        backend = Backend(args.data_dir, recover=False)
        created = seed_history(backend, days=args.days, seed=args.seed)
        print(f"added {len(created['wearable_sessions'])} SIMULATED wearable sessions and "
              f"{len(created['station_sessions'])} SIMULATED station sessions to {args.data_dir}")
        backend.shutdown()
        return 0

    import uvicorn

    from .adapters import SerialAdapter, SimAdapter
    from .app import create_app

    from .service import DataDirInUse

    try:  # validate everything before the data directory is touched
        if args.device == "sim":
            adapter = SimAdapter(scenario=args.scenario, seed=args.seed, duration_s=args.duration,
                                 speed=args.speed, loop=True)
            config = processing_config_for("sim")
        elif args.device == "serial":
            if not args.port:
                raise ValueError("--device serial needs --port")
            adapter = SerialAdapter(args.port, args.baud)
            config = processing_config_for("serial", args.imu_counts_per_g, args.imu_conversion_source)
        else:
            adapter = config = None
        backend = Backend(args.data_dir, exclusive=True)
    except DataDirInUse as exc:
        print(f"vg-backend: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"vg-backend: {exc}", file=sys.stderr)
        return 2
    if adapter is not None:
        backend.add_runtime("wearable", adapter, config)
    app = create_app(backend, on_shutdown=backend.shutdown)
    print(f"vg-backend: http://{args.host}:{args.http_port}  data={args.data_dir}  device={args.device}",
          file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.http_port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
