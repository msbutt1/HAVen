"""Hardware-facing commands for ``vg-station``: ``ports``, ``selftest`` and ``live``.

``--simulate`` swaps the serial port for the in-memory simulated controller (real-time
pacing), so the whole path can be rehearsed without any hardware. Simulated runs are
labelled simulated. Against a real port, results are labelled live; that path has not yet
run against the real controller.
"""

from __future__ import annotations

import sys
import time

from .adapters import AdapterError, SimulatedStationAdapter, SimulatedSubject
from .config import StationConfig
from .engine import StationEngine
from .link import LinkStationAdapter, SimulatedController
from .live import run_live
from .selftest import selftest
from .serial_transport import SerialTransport, list_serial_ports
from .store import ResultStore


def open_link(port: str | None, baud: int, simulate: bool, seed: int = 1):
    """Returns (adapter, controller_or_None, close). Raises if no port is given for a real run."""
    clock = time.monotonic
    if simulate:
        sim = SimulatedStationAdapter(clock, SimulatedSubject(35.0, seed=seed),
                                      detail={"scenario": "live_dry_run", "seed": seed})
        controller = SimulatedController(clock, sim)
        adapter = LinkStationAdapter(controller, clock, time.sleep, detail={"port": "simulated"})
        return adapter, controller, (lambda: None)
    if not port:
        raise ValueError("--port is required (for example COM5); use `vg-station ports` to list them, "
                         "or --simulate for a dry run")
    tx = SerialTransport(port, baud, open_wait_s=1.5)
    adapter = LinkStationAdapter(tx, clock, time.sleep, detail={"port": port, "baud": baud})
    return adapter, None, tx.close


def cmd_ports(args) -> int:
    ports = list_serial_ports()
    if not ports:
        print("no serial ports found (is the ESP32 plugged in, and its USB driver installed?)")
        return 1
    for device, desc in ports:
        print(f"{device}\t{desc}")
    return 0


def cmd_selftest(args) -> int:
    try:
        adapter, controller, close = open_link(args.port, args.baud, args.simulate)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(f"station self-test ({'SIMULATED controller' if args.simulate else args.port})")
    prompt = (lambda resp: controller.sim.inject_response(resp, 0.5)) if controller else None
    try:
        checks = selftest(adapter, time.monotonic, time.sleep, motor=not args.no_motor, prompt=prompt,
                          button_wait_s=args.button_wait)
    finally:
        try:
            adapter.stop_stimulus()  # leave the motor off whatever happened
        except Exception:
            pass
        close()
    failed = [c.name for c in checks if not c.ok]
    print("ALL CHECKS PASSED" if not failed else f"FAILED: {', '.join(failed)}")
    return 0 if not failed else 1


def _print_events(events: list[dict]) -> None:
    for e in events:
        t = e["type"]
        if t == "trial_started":
            what = "catch (no stimulus)" if e["kind"] == "catch" else f"motor setting {e['stimulus_setting']}"
            print(f"  trial {e['index']:>2}: {what}")
        elif t == "trial_completed":
            tr = e["trial"]
            print(f"           -> {tr['response'] or tr['response_status']}")
        elif t == "reversal":
            print(f"           reversal {e['count']}/{e['required']} at {e['level']}")
        elif t == "response_ignored":
            print(f"           (ignored a {e['reason']} response)")


def cmd_live(args) -> int:
    try:
        adapter, controller, close = open_link(args.port, args.baud, args.simulate, args.seed)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    engine = None
    try:
        store = ResultStore(args.results_dir)
        info = adapter.request_hello()
        base = StationConfig.preview() if args.mode == "preview" else StationConfig.full()
        rng = info["setting_range"]
        cfg = base.for_device_range(rng["min"], rng["max"])
        engine = StationEngine(adapter, cfg, clock=time.monotonic, session_id=args.session_id,
                               seed=args.seed, sink=store)
        fw = info["firmware"]
        print(f"{args.mode.upper()} test [{engine.provenance['source'].upper()}] "
              f"controller {fw['name']} {fw['version']}, motor settings {rng['min']}-{rng['max']} "
              f"(start {cfg.start_setting}, step {cfg.step}). Ctrl+C aborts.")
        result = run_live(engine, on_events=_print_events)
    except (AdapterError, ValueError, OSError) as exc:  # bad --session-id, unusable range, link down
        print(f"error: {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            adapter.stop_stimulus()  # leave the motor off whatever happened
        except Exception:
            pass
        close()
    th = result["threshold"]
    print(f"\n{result['result_label']}\nstate {result['state']}, outcome {result['outcome']}; threshold "
          + (f"{th['value']} motor setting" if th else "none"))
    print("limitations: " + ", ".join(x["code"] for x in result["quality"]["limitations"]))
    if not engine.saved:
        print(f"NOT SAVED  {engine.save_error}", file=sys.stderr)
        return 1
    print(f"saved {store.path_for(result['session_id'])}" + ("  (already existed)" if engine.already_saved else ""))
    return 0 if result["state"] in ("completed", "aborted") else 1
