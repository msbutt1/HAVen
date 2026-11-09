"""vg-bridge: live Wi-Fi bridge between the wearable and the backend/dashboard.

    wearable --TCP 8765 (contract v1 NDJSON)--> vg-bridge --> Processor (signals)
                                                    |--> derived NDJSON: stdout / --out
                                                    |--> derived NDJSON fan-out: TCP 8767
                                                    '--> raw recording: --record FILE

- Discovery: answers the wearable's UDP broadcast "VGDISCOVER 1 <device_id>"
  on 8766 with "VGBRIDGE 1 <tcp_port>", so the laptop's (changing) hotspot IP
  never needs to be configured on the device.
- Every line is validated with contract.parse_line; invalid frames are
  reported (feed_invalid_frame), never repaired.
- One Processor per device_id, host clock + poll() every 50 ms, so silence
  becomes "stale" within ~0.3 s.
- Commands from this terminal (+ Enter): relaxed | squeeze | cancel (EMG
  calibration) and newshift (start a new tool-time session).
- Accelerometer conversion: the firmware configures +-8 g; 4096 LSB/g is the
  documented sensitivity for that range (TDK PS-MPU-9250A-01). Override with
  --accel-lsb-per-g if the firmware range changes.

Reference ingestion for development and the demo. The production backend
(Abduljabbar) may embed the same Processor the same way.
"""

from __future__ import annotations

import argparse
import json
import selectors
import socket
import sys
import time
from dataclasses import replace

from . import contract
from .processing import ProcessingConfig, Processor, StreamConfig, to_ndjson_line

G = 9.80665


def live_config(lsb_per_g: float) -> ProcessingConfig:
    conv = {a: (G / lsb_per_g, 0.0) for a in ("ax", "ay", "az")}
    src = (f"MPU-6500 accelerometer configured +-8 g by firmware; {lsb_per_g:g} LSB/g per "
           "TDK PS-MPU-9250A-01 sensitivity table (identity 0x70 from secondary sources)")
    base = ProcessingConfig()
    # Wi-Fi (phone hotspot) delivers in bursts with 250-500 ms pauses (measured on
    # board W): a 1 s stale limit still flags a real outage within ~1 s without
    # resetting the streams on every hiccup.
    # EMG: board W flags clipping per 50-sample batch, so one peak touching the ADC
    # ceiling during a maximum squeeze marks 50 samples (measured 2026-11-08: ~0.7 %
    # of samples at the rail, 5-7 % flagged). Allow brief peak clipping in a capture;
    # a signal stuck at the rail still fails. Records still report "degraded".
    cal = replace(base.calibration, max_clipped_fraction=0.10)
    return replace(base, clock="host", stale_after_s=1.0, calibration=cal,
                   imu=StreamConfig(window_s=0.5, conversion=conv, conversion_source=src))


class Bridge:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = live_config(args.accel_lsb_per_g)
        self.sel = selectors.DefaultSelector()
        self.procs: dict[str, Processor] = {}
        self.buf: dict[socket.socket, bytearray] = {}
        self.peer: dict[socket.socket, str] = {}
        self.subscribers: list[socket.socket] = []
        self.out = open(args.out, "a", buffering=1) if args.out else None
        self.rec = open(args.record, "ab", buffering=0) if args.record else None
        self.labels = open(args.labels, "a", buffering=1) if args.labels else None
        self.last_t_us: dict[str, int] = {}
        self.latest: dict[str, dict] = {}
        self.stats = {"records": 0, "invalid": 0, "bytes": 0}
        self.last_summary = 0.0

    # ---- sockets ---------------------------------------------------------
    def start(self) -> None:
        a = self.args
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(("0.0.0.0", a.discovery_port))
        self.sel.register(udp, selectors.EVENT_READ, self.on_discover)
        for port, handler in ((a.port, self.on_device_accept), (a.serve_port, self.on_sub_accept)):
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", port))
            srv.listen(8)
            srv.setblocking(False)
            self.sel.register(srv, selectors.EVENT_READ, handler)
        if not a.no_stdin and sys.stdin.isatty() or a.stdin_commands:
            self.sel.register(sys.stdin, selectors.EVENT_READ, self.on_stdin)
        log(f"vg-bridge: devices -> TCP {a.port}, discovery UDP {a.discovery_port}, "
            f"derived fan-out TCP {a.serve_port}. Commands: relaxed | squeeze | cancel | newshift")

    def on_discover(self, sock: socket.socket) -> None:
        data, addr = sock.recvfrom(256)
        if data.startswith(b"VGDISCOVER 1 "):
            sock.sendto(f"VGBRIDGE 1 {self.args.port}".encode(), addr)
            if self.args.verbose:
                log(f"discovery from {addr[0]}: {data[13:].decode(errors='replace')}")

    def on_device_accept(self, srv: socket.socket) -> None:
        conn, addr = srv.accept()
        conn.setblocking(False)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf[conn] = bytearray()
        self.peer[conn] = addr[0]
        self.sel.register(conn, selectors.EVENT_READ, self.on_device_data)
        log(f"device connected from {addr[0]}")

    def on_sub_accept(self, srv: socket.socket) -> None:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.subscribers.append(conn)
        self.buf[conn] = bytearray()
        self.sel.register(conn, selectors.EVENT_READ, self.on_sub_data)
        log(f"derived-record subscriber connected from {addr[0]}")

    def on_sub_data(self, conn: socket.socket) -> None:
        """Subscribers (e.g. vg-live) may send the same commands as the terminal."""
        try:
            data = conn.recv(4096)
        except OSError:
            data = b""
        if not data:
            self.sel.unregister(conn)
            if conn in self.subscribers:
                self.subscribers.remove(conn)
            self.buf.pop(conn, None)
            conn.close()
            return
        b = self.buf[conn]
        b += data
        while b"\n" in b:
            line, _, rest = bytes(b).partition(b"\n")
            b[:] = rest
            self.command(line.decode(errors="replace").strip().lower())

    def on_device_data(self, conn: socket.socket) -> None:
        try:
            data = conn.recv(65536)
        except OSError:
            data = b""
        if not data:
            log(f"device {self.peer.get(conn)} disconnected")
            self.sel.unregister(conn)
            conn.close()
            self.buf.pop(conn, None)
            return
        self.stats["bytes"] += len(data)
        if self.rec:
            self.rec.write(data)
        b = self.buf[conn]
        b += data
        while True:
            i = b.find(b"\n")
            if i < 0:
                if len(b) > contract.MAX_LINE_BYTES:
                    self.invalid([contract.Issue("line_too_long", "", "frame exceeds limit")])
                    b.clear()
                break
            line = bytes(b[: i + 1])
            del b[: i + 1]
            self.on_line(line)

    def on_stdin(self, f) -> None:
        self.command(f.readline().strip().lower())

    def command(self, cmd: str) -> None:
        if not cmd:
            return
        if cmd.startswith("label:"):
            self.label(cmd[6:].strip())
            return
        tool_cmds = ("toolcal:still", "toolcal:movement", "toolcal:tool", "toolcal:cancel",
                     "toolcal:reset")
        if cmd not in ("relaxed", "squeeze", "cancel", "newshift") + tool_cmds:
            log("commands: relaxed | squeeze | cancel | newshift | toolcal:still|movement|tool|cancel|reset")
            return
        if not self.procs:
            log("no device yet")
            return
        if cmd in tool_cmds[:3]:
            self.label("toolcal-" + cmd[8:])          # mark the raw recording too
        for dev, proc in self.procs.items():
            if cmd.startswith("toolcal:"):
                ph = cmd[8:]
                outs = (proc.tool_calibration_cancel() if ph == "cancel"
                        else proc.tool_calibration_reset() if ph == "reset"
                        else proc.tool_calibration_start(ph))
            elif cmd == "newshift":
                outs = proc.tool_use_reset_session()
            elif cmd == "cancel":
                outs = proc.calibration_cancel()
            else:
                outs = proc.calibration_start(cmd)
            for o in outs:
                self.publish(o)
            log(f"{dev}: {cmd}")

    def label(self, name: str) -> None:
        """Mark the recording: the label applies from now until the next label.
        Stored with the latest device time per device, so analysis can cut the
        raw recording by label (tools: threshold tuning with real tools)."""
        safe = "".join(c for c in name if c.isalnum() or c in "-_ ")[:40] or "unlabelled"
        entry = {"label": safe, "wall_time": time.time(), "device_t_us": dict(self.last_t_us)}
        if self.labels:
            self.labels.write(json.dumps(entry) + "\n")
        log(f"LABEL {safe}  (device t = {self.last_t_us})")
        note = {"type": "bridge_label", "label": safe, "device_t_us": dict(self.last_t_us)}
        for s in list(self.subscribers):
            try:
                s.sendall((json.dumps(note) + "\n").encode())
            except OSError:
                pass

    # ---- processing ------------------------------------------------------
    def invalid(self, issues) -> None:
        self.stats["invalid"] += 1
        for proc in self.procs.values():
            for o in proc.feed_invalid_frame(issues, "wifi-bridge"):
                self.publish(o)
        if self.args.verbose:
            log(f"invalid frame: {issues[0]}")

    def on_line(self, line: bytes) -> None:
        rec, issues = contract.parse_line(line)
        if rec is None or issues:
            self.invalid(issues)
            return
        self.stats["records"] += 1
        dev = rec["device_id"]
        t = rec.get("timing", {})
        tu = t.get("t0_us") if t.get("mode") == "uniform" else (t.get("t_us") or [rec.get("t_us")])[0]
        if tu is not None:
            self.last_t_us[dev] = tu
        proc = self.procs.get(dev)
        if proc is None:
            proc = self.procs[dev] = Processor(self.cfg)
            log(f"new device {dev} (provenance {rec['provenance']['source']})")
        for o in proc.feed(rec, rx_time_s=time.monotonic()):
            self.publish(o)

    def publish(self, o: dict) -> None:
        line = to_ndjson_line(o)
        if self.out:
            self.out.write(line)
        elif not self.args.quiet_stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        dead = []
        for s in self.subscribers:
            try:
                s.sendall(line.encode())
            except OSError:
                dead.append(s)
        for s in dead:
            self.subscribers.remove(s)
            try:
                self.sel.unregister(s)
            except (KeyError, ValueError):
                pass
            self.buf.pop(s, None)
            s.close()
        key = o["type"] if o["type"] != "stream_features" else f"features:{o['stream']}"
        self.latest[key] = o

    def poll(self) -> None:
        now = time.monotonic()
        for proc in self.procs.values():
            for o in proc.poll(now):
                self.publish(o)
        if now - self.last_summary >= 1.0:
            self.last_summary = now
            self.summary()

    def summary(self) -> None:
        if not self.procs:
            return
        emg = self.latest.get("features:emg", {}).get("features", {}).get("emg_activation", {})
        vib = self.latest.get("features:imu", {}).get("features", {}).get("vibration", {})
        sc = self.latest.get("experimental_score", {})
        tu = self.latest.get("tool_use", {})
        env = (emg.get("envelope") or {}).get("value") or {}
        act = (emg.get("normalized") or {}).get("value") or {}
        phys = vib.get("physical") or {}
        cal = (emg.get("calibration") or {}).get("state")

        def f(v, fmt):
            return "-" if v is None else format(v, fmt)
        log(f"[{self.stats['records']} rec, {self.stats['invalid']} invalid, "
            f"{self.stats['bytes'] / 1024:.0f} KiB] EMG env {f(env.get('emg0'), '.1f')} "
            f"({(emg.get('envelope') or {}).get('status', '-')}) | activation "
            f"{f(act.get('emg0'), '.2f')} cal={cal or '-'} | vib {f(phys.get('combined_rms'), '.3f')} "
            f"m/s2 ({phys.get('status', vib.get('status', '-'))}) | TOOL {tu.get('state', '-')} "
            f"band {f(tu.get('tool_band_rms_ms2'), '.2f')} @ {f(tu.get('dominant_hz'), '.0f')} Hz, "
            f"arm {f(tu.get('motion_band_rms_ms2'), '.2f')} | tool time "
            f"{f(tu.get('session_tool_s'), '.1f')} s, bouts {tu.get('bouts', '-')}"
            + (f" WARN {','.join(tu['warnings'])}" if tu.get("warnings") else "")
            + f" | score {sc.get('status', '-')}")

    def run(self) -> None:
        self.start()
        try:
            while True:
                for key, _ in self.sel.select(timeout=0.05):
                    key.data(key.fileobj)
                self.poll()
        except KeyboardInterrupt:
            pass
        finally:
            for proc in self.procs.values():
                for o in proc.finish():
                    self.publish(o)


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vg-bridge", description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8765, help="device TCP port (default 8765)")
    p.add_argument("--discovery-port", type=int, default=8766)
    p.add_argument("--serve-port", type=int, default=8767,
                   help="derived-record NDJSON fan-out for backend/dashboard (default 8767)")
    p.add_argument("--out", help="append derived NDJSON here instead of stdout")
    p.add_argument("--record", help="append the raw device telemetry here (backup recording)")
    p.add_argument("--labels", help="append recording labels (label:<name> commands) here")
    p.add_argument("--accel-lsb-per-g", type=float, default=4096.0)
    p.add_argument("--quiet-stdout", action="store_true", help="do not print derived records")
    p.add_argument("--no-stdin", action="store_true", help="ignore terminal commands")
    p.add_argument("--stdin-commands", action="store_true", help="read commands even if stdin is not a tty")
    p.add_argument("-v", "--verbose", action="store_true")
    Bridge(p.parse_args(argv)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
