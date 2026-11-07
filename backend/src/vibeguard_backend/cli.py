"""vg-backend: run and operate the VibeGuard local backend.

    vg-backend serve                                   # simulated wearable (default)
    vg-backend serve --device serial --port '/dev/cu.usbmodem*' --baud 921600
    vg-backend serve --device none                     # API only (station, history, replay)
    vg-backend serve --backup backup/demo-session      # labelled REPLAY fallback as the 'wearable'
    vg-backend preflight --backup backup/demo-session  # demo-day readiness checklist
    vg-backend export-backup SESSION_ID backup/my-run  # keep a known-good recording
    vg-backend seed-history --days 14                  # labelled SIMULATED history

Hardware bring-up (see HARDWARE_BRINGUP.md):
    vg-backend ports                                   # what is plugged in
    vg-backend serial-check --port '/dev/cu.usbmodem*' --baud 921600   # is the link good?
    vg-backend fake-device                             # a pseudo-serial port that acts like the firmware
    vg-backend serve --profile hardware.json           # port, baud, IMU conversion from one file

``--data-dir`` (or $VG_DATA_DIR) works before or after the subcommand.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
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
    s.add_argument("--profile", type=Path, default=None,
                   help="hardware profile JSON (port, baud, IMU sensitivity + source, EMG output); implies "
                        "--device serial; flags given on the command line override it")
    s.add_argument("--port", help="serial port or glob, e.g. '/dev/cu.usbmodem*'")
    s.add_argument("--baud", type=int, default=None, help="serial baud rate (default 921600)")
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

    sub.add_parser("ports", parents=[common], help="list serial ports and likely microcontroller boards")

    c = sub.add_parser("serial-check", parents=[common],
                       help="read a real serial link and report what the backend will see")
    c.add_argument("--port", required=True)
    c.add_argument("--baud", type=int, default=921600)
    c.add_argument("--seconds", type=float, default=20.0)
    c.add_argument("--save", type=Path, default=None, help="also save the raw bytes (NDJSON) for vg-process / vg-validate")
    c.add_argument("--mains-hz", type=int, choices=(50, 60), default=None)
    c.add_argument("--try-bauds", default=None, help="comma-separated baud rates to try first (finds the right one)")
    c.add_argument("--json", action="store_true", help="print the measurements as JSON too")

    d = sub.add_parser("fake-device", parents=[common],
                       help="a pseudo-serial port that behaves like the firmware (rehearse without hardware)")
    d.add_argument("--scenario", default="demo", help="simulator scenario (vg-sim --list)")
    d.add_argument("--seed", type=int, default=1)
    d.add_argument("--speed", type=float, default=1.0)
    d.add_argument("--baud-limit", type=int, default=None, help="throttle to this baud rate (e.g. 115200)")
    d.add_argument("--mode", choices=("frames", "garbage", "silent"), default="frames",
                   help="garbage = wrong-baud symptoms; silent = link up but no data")
    d.add_argument("--hang-after", type=float, default=None, help="stop sending after N s (hung MCU)")
    d.add_argument("--unplug-after", type=float, default=None, help="pull the cable after N s")
    d.add_argument("--no-hello", action="store_true")
    d.add_argument("--rate-error", type=float, default=0.0, help="stretch the device clock (0.1 = 10%% slow)")
    d.add_argument("--once", action="store_true", help="play the run once instead of looping")

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

    if args.cmd == "ports":
        from .hardware import list_ports, render_ports

        print(render_ports(list_ports()))
        return 0

    if args.cmd == "fake-device":
        from .hardware import FakeDevice

        try:
            dev = FakeDevice(args.scenario, args.seed, speed=args.speed, baud_limit=args.baud_limit, mode=args.mode,
                             hang_after_s=args.hang_after, unplug_after_s=args.unplug_after, loop=not args.once,
                             drop_hello=args.no_hello, rate_error=args.rate_error).start()
        except (OSError, ValueError) as exc:
            print(f"vg-backend: {exc}", file=sys.stderr)
            return 2
        print(f"fake device (SIMULATED firmware, mode {args.mode}) on  {dev.path}\n"
              f"  try:  uv run vg-backend serial-check --port {dev.path} --baud 115200 --seconds 10\n"
              f"        uv run vg-backend serve --device serial --port {dev.path} --baud 115200\n"
              "Ctrl-C to stop.", flush=True)
        try:
            while dev._thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            dev.stop()
        return 0

    if args.cmd == "serial-check":
        from .hardware import find_baud, list_ports, serial_check

        if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 3600:
            print("vg-backend: --seconds must be between 1 and 3600", file=sys.stderr)
            return 2
        baud = args.baud
        if args.try_bauds:
            try:
                bauds = [int(x) for x in args.try_bauds.split(",") if x.strip()]
            except ValueError:
                print("vg-backend: --try-bauds must be comma-separated integers", file=sys.stderr)
                return 2
            print(f"trying {len(bauds)} baud rates on {args.port} ({3} s each)...")
            results = find_baud(args.port, bauds, progress=print)
            good = [r for r in results if r.get("valid", 0) > 0]
            if not good:
                print("no baud rate produced valid frames. Run `vg-backend ports`, check the cable, and that the "
                      "firmware is sending contract v1 NDJSON.")
                return 1
            best = max(good, key=lambda r: (r.get("share", 0), r["valid"]))
            baud = best["baud"]
            print(f"best: {baud} baud ({best['valid']}/{best['lines']} valid)\n")
        print(f"checking {args.port} at {baud} baud for {args.seconds:g} s ...")
        res = serial_check(args.port, baud, args.seconds, args.save, args.mains_hz, progress=print)
        print(res.report.render(not_ready="NOT READY - fix the FAIL lines before connecting this to the backend"))
        if args.json:
            print(json.dumps(res.stats, indent=2, default=str))
        if args.save:
            print(f"\nraw capture saved to {args.save}: analyse it with  uv run --project ../signals vg-validate {args.save}")
        return 1 if res.report.failed else 0

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

    runtime_meta: dict = {}
    try:  # validate everything before the data directory is touched
        adapter = config = None
        if args.backup is not None:
            from .backup import BackupError, verify_backup
            try:
                verify_backup(args.backup)
            except BackupError as exc:
                raise ValueError(f"backup is not usable: {exc}") from exc
        elif args.device == "sim" and args.profile is None:
            adapter = SimAdapter(scenario=args.scenario, seed=args.seed, duration_s=args.duration,
                                 speed=args.speed, loop=True)
            config = processing_config_for("sim")
        elif args.device == "serial" or args.profile is not None:
            profile = None
            if args.profile is not None:
                from .hardware import HardwareProfile, ProfileError, load_profile
                try:
                    profile = load_profile(args.profile)
                except ProfileError as exc:
                    raise ValueError(f"hardware profile: {exc}") from exc
            port = args.port or (profile.port if profile else None)
            baud = args.baud or (profile.baud if profile and profile.baud else 921600)
            cpg = args.imu_counts_per_g or (profile.imu_counts_per_g if profile else None)
            src = args.imu_conversion_source or (profile.imu_conversion_source if profile else None)
            if not port:
                raise ValueError("--device serial needs --port (or a profile with a port)")
            adapter = SerialAdapter(port, baud)
            config = processing_config_for("serial", cpg, src)
            runtime_meta = {"hardware_profile": profile.describe(), "hardware_profile_name": profile.name} if profile else {}
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
        backend.add_runtime("wearable", adapter, config, meta=runtime_meta)

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
          f"source={'backup replay' if args.backup else (adapter.kind if adapter is not None else 'none')}",
          file=sys.stderr)
    # A client that never reads must not keep the server (and the open session) alive on Ctrl-C.
    uvicorn.run(app, host=args.host, port=args.http_port, log_level=args.log_level,
                timeout_graceful_shutdown=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
