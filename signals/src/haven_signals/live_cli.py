"""vg-live: plain-text live view of the wearable (words and numbers only).

Connects to vg-bridge's derived-record stream (default 127.0.0.1:8767), shows
the latest values about 5 times per second, and sends commands to the bridge:

    r + Enter   start RELAXED capture   (arm fully relaxed, ~4 s)
    s + Enter   start SQUEEZE capture   (already squeezing at maximum, ~4 s)
    c + Enter   cancel a capture
    n + Enter   new shift (reset tool time)
    q + Enter   quit

"Grip effort" is the EMG envelope relative to this wearer's relaxed (0 %) and
maximum-squeeze (100 %) calibration. It is muscle activity, not grip force.
"""

from __future__ import annotations

import argparse
import json
import select
import socket
import sys
import time

CAPTURE_S = 3.5   # 3 s capture + 0.5 s discarded at the start (CalibrationConfig defaults)


def fmt(v, spec, dash="-"):
    return dash if v is None else format(v, spec)


class View:
    def __init__(self) -> None:
        self.latest: dict[str, dict] = {}
        self.cal_state = "uncalibrated"
        self.cal_reason = None
        self.cal_started = None
        self.messages: list[str] = []
        self.last_rx = None
        self.device = None

    def feed(self, o: dict) -> None:
        self.last_rx = time.monotonic()
        self.device = o.get("device_id", self.device)
        t = o["type"]
        key = f"features:{o['stream']}" if t == "stream_features" else t
        self.latest[key] = o
        if t == "stream_event":
            ev, d = o.get("event"), o.get("detail") or {}
            if ev == "calibration_state":
                self.cal_state, self.cal_reason = d.get("state"), d.get("reason")
                if str(self.cal_state).startswith("capturing"):
                    self.cal_started = time.monotonic()
                self.note(f"calibration -> {self.cal_state}" + (f" ({self.cal_reason})" if self.cal_reason else ""))
            elif ev in ("calibration_rejected", "tool_session_reset", "boot_change", "stream_stale",
                        "stream_recovered", "device_sensor_state"):
                self.note(f"{ev} {json.dumps(d)[:90]}")

    def note(self, msg: str) -> None:
        self.messages = (self.messages + [f"{time.strftime('%H:%M:%S')} {msg}"])[-6:]

    def render(self) -> str:
        emg = self.latest.get("features:emg", {}).get("features", {}).get("emg_activation", {})
        vib = self.latest.get("features:imu", {}).get("features", {}).get("vibration", {})
        tu = self.latest.get("tool_use", {})
        env = emg.get("envelope") or {}
        norm = emg.get("normalized") or {}
        cal = emg.get("calibration") or {}
        state = cal.get("state") or self.cal_state
        age = None if self.last_rx is None else time.monotonic() - self.last_rx
        lines = [
            "HAVEN LIVE  (plain text; values update ~5x/s)",
            f"device   {self.device or '-'}   data age {fmt(age, '.1f')} s"
            + ("   NO DATA" if age is None or age > 2 else ""),
            "",
            f"EMG envelope     {fmt((env.get('value') or {}).get('emg0'), '8.1f')} counts   "
            f"status {env.get('status', '-')} {','.join(env.get('reasons') or [])}",
            f"calibration      {state}"
            + (f"   reason: {cal.get('reason') or self.cal_reason}" if (cal.get('reason') or self.cal_reason) else ""),
        ]
        if str(state).startswith("capturing") and self.cal_started:
            left = max(0.0, CAPTURE_S - (time.monotonic() - self.cal_started))
            what = "STAY RELAXED" if "relaxed" in state else "KEEP SQUEEZING AT MAXIMUM"
            lines.append(f"   >>> {what}   {left:3.1f} s left")
        val = (norm.get("value") or {}).get("emg0")
        disp = (norm.get("display") or {}).get("emg0")
        if norm.get("status") in ("ok", "degraded") and val is not None:
            pct = 100.0 * val
            bar = "#" * int(round(max(0.0, min(1.0, disp or 0.0)) * 40))
            lines.append(f"GRIP EFFORT      {pct:6.1f} % of max calibration   [{bar:<40}]")
            if (norm.get("above_reference") or {}).get("emg0"):
                lines.append("                 (above the calibrated maximum)")
        else:
            lines.append(f"GRIP EFFORT      -   ({norm.get('status', 'not calibrated')}"
                         f" {','.join(norm.get('reasons') or [])})")
        phys = vib.get("physical") or {}
        lines += [
            "",
            f"vibration        {fmt(phys.get('combined_rms'), '6.3f')} m/s2   status {phys.get('status', '-')}",
            f"TOOL             {tu.get('state', '-')}   tool band {fmt(tu.get('tool_band_rms_ms2'), '.2f')} m/s2"
            f" @ {fmt(tu.get('dominant_hz'), '.0f')} Hz   arm {fmt(tu.get('motion_band_rms_ms2'), '.2f')} m/s2",
            f"tool time        {fmt(tu.get('session_tool_s'), '.1f')} s   bouts {tu.get('bouts', '-')}"
            f"   current bout {fmt(tu.get('bout_s'), '.1f')} s",
            f"warnings         {', '.join(tu.get('warnings') or []) or 'none'}",
            "",
            "commands: r=relaxed  s=squeeze  c=cancel  n=new shift  q=quit   (type + Enter)",
            "recent:",
        ] + [f"  {m}" for m in self.messages]
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vg-live", description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8767)
    p.add_argument("--web", action="store_true", help="serve the browser calibration page")
    p.add_argument("--web-port", type=int, default=8080)
    a = p.parse_args(argv)
    if a.web:
        return web_main(a)
    try:
        sock = socket.create_connection((a.host, a.port), timeout=3)
    except OSError as e:
        print(f"cannot connect to vg-bridge at {a.host}:{a.port}: {e} (is vg-bridge running?)")
        return 1
    sock.setblocking(False)
    view, buf = View(), b""
    keys = {"r": "relaxed", "s": "squeeze", "c": "cancel", "n": "newshift"}
    next_draw = 0.0
    while True:
        r, _, _ = select.select([sock, sys.stdin], [], [], 0.05)
        if sock in r:
            try:
                chunk = sock.recv(65536)
            except BlockingIOError:
                chunk = None
            if chunk == b"":
                print("\nbridge closed the connection")
                return 1
            chunk = chunk or b""
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    view.feed(json.loads(line))
                except (ValueError, KeyError):
                    pass
        if sys.stdin in r:
            cmd = sys.stdin.readline().strip().lower()
            if cmd == "q":
                return 0
            if cmd in keys:
                sock.sendall((keys[cmd] + "\n").encode())
                view.note(f"sent: {keys[cmd]}")
            elif cmd:
                view.note("unknown command (r, s, c, n, q)")
        if time.monotonic() >= next_draw:
            sys.stdout.write("\033[2J\033[H" + view.render() + "\n> ")
            sys.stdout.flush()
            next_draw = time.monotonic() + 0.2


def web_main(a) -> int:
    from .web_ui import Shared, start_web

    def send(cmd: str) -> None:
        try:
            sock = socket.create_connection((a.host, a.port), timeout=3)
            sock.sendall((cmd + "\n").encode())
            sock.close()
        except OSError:
            pass
    shared = Shared()
    start_web(shared, a.host, a.port, a.web_port, send)
    print(f"vg-live web page: http://127.0.0.1:{a.web_port}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
