"""vg-backend: run and operate the VibeGuard local backend.

    vg-backend serve                                   # simulated wearable (default)
    vg-backend serve --device serial --port '/dev/cu.usbmodem*' --baud 921600
    vg-backend serve --device none                     # API only (station, history, replay)
    vg-backend serve --backup backup/demo-session      # labelled REPLAY fallback as the 'wearable'
    vg-backend preflight --backup backup/demo-session  # demo-day readiness checklist
    vg-backend export-backup SESSION_ID backup/my-run  # keep a known-good recording
    vg-backend seed-history --days 14                  # labelled SIMULATED history

``--data-dir`` (or $VG_DATA_DIR) works before or after the subcommand.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("VG_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))


def _common() -> argparse.ArgumentParser:
    """Options every subcommand accepts. SUPPRESS keeps a value given before the subcommand."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS,
                   help=f"SQLite + session files (default {DEFAULT_DATA_DIR}, or $VG_DATA_DIR)")
    return p


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vg-backend", description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                   help=f"SQLite + session files (default {DEFAULT_DATA_DIR}, or $VG_DATA_DIR)")
    common = _common()
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", parents=[common], help="run the HTTP/WebSocket API and the wearable runtime")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--allow-host", action="append", default=[],
                   help="extra Host header value to accept (when listening beyond localhost)")
    s.add_argument("--http-port", type=int, default=8765)
    s.add_argument("--device", choices=("sim", "serial", "none"), default="sim")
    s.add_argument("--backup", type=Path, default=None,
                   help="start with this backup recording as the 'wearable' (labelled REPLAY); overrides --device")
    s.add_argument("--loop", action="store_true",
                   help="with --backup: play the recording again each time it ends (each pass is labelled REPLAY)")
    s.add_argument("--scenario", default="demo", help="simulated scenario (vg-sim --list)")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--duration", type=float, default=30.0,
                   help="simulated run length; the simulator then 'reboots' and continues")
    s.add_argument("--speed", type=float, default=1.0, help="simulated/backup pacing factor")
    s.add_argument("--port", help="serial port or glob, e.g. '/dev/cu.usbmodem*'")
    s.add_argument("--baud", type=int, default=921600)
    s.add_argument("--imu-counts-per-g", type=float, default=None,
                   help="confirmed accelerometer sensitivity (counts per g) for m/s^2 output")
    s.add_argument("--imu-conversion-source", default=None,
                   help="where the sensitivity comes from (datasheet, configured range)")
    s.add_argument("--log-level", default="info")

    h = sub.add_parser("seed-history", parents=[common], help="add labelled SIMULATED history sessions")
    h.add_argument("--days", type=int, default=14)
    h.add_argument("--seed", type=int, default=7)

    f = sub.add_parser("preflight", parents=[common], help="demo-day readiness checklist for a running backend")
    f.add_argument("--url", default="http://127.0.0.1:8765")
    f.add_argument("--runtime", default="wearable")
    f.add_argument("--expect", choices=("any", "live", "simulated", "replay"), default="any",
                   help="fail unless the data source is this (default: just report it)")
    f.add_argument("--backup", type=Path, default=None, help="also verify this backup recording")

    e = sub.add_parser("export-backup", parents=[common], help="export a finished session as a verified backup")
    e.add_argument("session_id")
    e.add_argument("dest", type=Path)
    e.add_argument("--note", default=None)

    v = sub.add_parser("verify-backup", parents=[common], help="check a backup folder is complete and unmodified")
    v.add_argument("path", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(args, "log_level", "info").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "preflight":
        from .preflight import run_preflight

        report = run_preflight(args.url, args.runtime, args.expect, args.backup)
        print(report.render())
        return 1 if report.failed else 0

    if args.cmd == "verify-backup":
        from .backup import BackupError, verify_backup

        try:
            meta = verify_backup(args.path)
        except BackupError as exc:
            print(f"vg-backend: {exc}", file=sys.stderr)
            return 1
        print(f"OK: {meta['frames']} frames, {meta['label_for_dashboard']}, recording {meta['recording_id']}")
        return 0

    from .service import Backend, DataDirInUse, NotFound, processing_config_for

    if args.cmd == "seed-history":
        from .history import seed_history

        # recover=False: a running server owns recovery; seeding must never touch its sessions
        backend = Backend(args.data_dir, recover=False)
        created = seed_history(backend, days=args.days, seed=args.seed)
        print(f"added {len(created['wearable_sessions'])} SIMULATED wearable sessions and "
              f"{len(created['station_sessions'])} SIMULATED station sessions to {backend.data_dir}"
              + (f" ({created['skipped']} days already present, skipped)" if created["skipped"] else ""))
        backend.shutdown()
        return 0

    if args.cmd == "export-backup":
        from .backup import BackupError, export_backup
        from .runtime import RuntimeConflict

        if not (Path(args.data_dir) / "vibeguard.sqlite3").is_file():
            print(f"vg-backend: no VibeGuard data in {args.data_dir} (wrong --data-dir?)", file=sys.stderr)
            return 1
        backend = Backend(args.data_dir, recover=False)
        try:
            meta = export_backup(backend, args.session_id, args.dest, args.note)
        except (BackupError, NotFound, RuntimeConflict, OSError) as exc:
            print(f"vg-backend: {exc}", file=sys.stderr)
            return 1
        finally:
            backend.shutdown()
        if meta.get("interrupted"):
            print("vg-backend: WARNING the session was cut off by a crash: this recording is truncated "
                  "and was not calibrated by the end", file=sys.stderr)
        print(f"exported {meta['frames']} frames ({meta['label_for_dashboard']}) to {args.dest}; "
              f"replay it with: vg-backend serve --backup {args.dest}")
        return 0

    import uvicorn

    from .adapters import SerialAdapter, SimAdapter
    from .app import LOCAL_HOSTS, create_app
    from .backup import BackupError

    try:  # validate everything before the data directory is touched
        adapter = config = None
        if args.backup is not None:
            from .backup import BackupError, verify_backup
            try:
                verify_backup(args.backup)
            except BackupError as exc:
                raise ValueError(f"backup is not usable: {exc}") from exc
        elif args.device == "sim":
            adapter = SimAdapter(scenario=args.scenario, seed=args.seed, duration_s=args.duration,
                                 speed=args.speed, loop=True)
            config = processing_config_for("sim")
        elif args.device == "serial":
            if not args.port:
                raise ValueError("--device serial needs --port")
            adapter = SerialAdapter(args.port, args.baud)
            config = processing_config_for("serial", args.imu_counts_per_g, args.imu_conversion_source)
        if not math.isfinite(args.speed) or not 0 < args.speed <= 1000:
            raise ValueError("--speed must be a number above 0 and at most 1000")
        if args.loop and args.backup is None:
            raise ValueError("--loop only applies to --backup")
        backend = Backend(args.data_dir, exclusive=True)
    except DataDirInUse as exc:
        print(f"vg-backend: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"vg-backend: {exc}", file=sys.stderr)
        return 2
    if args.backup is not None:
        from .backup import start_backup_replay

        try:
            start_backup_replay(backend, args.backup, name="wearable", speed=args.speed)
        except (BackupError, OSError, ValueError, KeyError) as exc:
            backend.shutdown()
            print(f"vg-backend: cannot start the backup: {exc}", file=sys.stderr)
            return 2
        if args.loop:
            backend.loop_backup("wearable")
        print(f"vg-backend: REPLAY FALLBACK from {args.backup} (labelled replay, not live)"
              + (", looping" if args.loop else ", plays once: POST /api/runtimes/wearable/restart to replay"),
              file=sys.stderr)
    elif adapter is not None:
        backend.add_runtime("wearable", adapter, config)

    wildcard = args.host in ("0.0.0.0", "::", "")

    def bracket(h: str) -> str:  # browsers send IPv6 literals as [addr]
        return f"[{h}]" if h.count(":") >= 2 and not h.startswith("[") else h  # a bare IPv6 literal
    allowed = None if wildcard else tuple({*LOCAL_HOSTS, bracket(args.host.lower()),
                                           *(bracket(h.lower()) for h in args.allow_host)})
    if wildcard:
        print("vg-backend: WARNING listening on all interfaces; the API has no authentication and the "
              "Host check is off (cross-site requests are still refused)", file=sys.stderr)
    app = create_app(backend, on_shutdown=backend.shutdown, allowed_hosts=allowed)
    print(f"vg-backend: http://{args.host}:{args.http_port}  data={backend.data_dir}  "
          f"source={'backup replay' if args.backup else args.device}", file=sys.stderr)
    # A client that never reads must not keep the server (and the open session) alive on Ctrl-C.
    uvicorn.run(app, host=args.host, port=args.http_port, log_level=args.log_level,
                timeout_graceful_shutdown=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
