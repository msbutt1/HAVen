"""A SIMULATED Wi-Fi wearable, to rehearse the Wi-Fi link without the board.

It does what board W does on the laptop's network (firmware/INTEGRATION_FOR_ABDULJABBAR.md): broadcasts
"VGDISCOVER 1 <device_id>" on UDP 8766, waits for "VGBRIDGE 1 <port>", connects to that TCP port and streams
contract v1 NDJSON. The records come from the signals simulator, so they are labelled ``simulated`` and can
never be mistaken for the real sleeve. Each pass of the scenario is a new boot (a new ``boot_id``).

    uv run vg-backend fake-wifi --scenario tool_vs_arm      # tool vs normal arm use, for tool detection
"""

from __future__ import annotations

import socket
import threading
import time

from .adapters import AdapterDisconnected, EndOfStream, SimAdapter


class FakeWifiWearable:
    def __init__(self, scenario: str = "tool_vs_arm", seed: int = 1, duration_s: float = 60.0, speed: float = 1.0,
                 discovery_host: str = "127.0.0.1", discovery_port: int = 8766,
                 stop_after_s: float | None = None, retry_s: float = 1.0):
        self.sim = SimAdapter(scenario=scenario, seed=seed, duration_s=duration_s, speed=speed, loop=True)
        self.scenario = scenario
        self.discovery = (discovery_host, discovery_port)
        self.stop_after_s = stop_after_s
        self.retry_s = retry_s
        self.device_id = "sim-wearable-01"
        self.sent = 0
        self.connections = 0
        self.connected_to: tuple[str, int] | None = None
        self.state = "starting"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-wifi-wearable", daemon=True)

    def start(self) -> "FakeWifiWearable":
        self.sim.open()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(3)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _discover(self) -> tuple[str, int] | None:
        """Broadcast (or send to the given host) and wait for the laptop's answer."""
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.settimeout(0.5)
        try:
            udp.sendto(f"VGDISCOVER 1 {self.device_id}".encode(), self.discovery)
            data, addr = udp.recvfrom(256)
        except OSError:
            return None
        finally:
            udp.close()
        parts = data.decode(errors="replace").split()
        if len(parts) == 3 and parts[0] == "VGBRIDGE" and parts[1] == "1" and parts[2].isdigit():
            return addr[0], int(parts[2])
        return None

    def _run(self) -> None:
        started = time.monotonic()
        while not self._stop.is_set():
            self.state = "discovering"
            target = self._discover()
            if target is None:
                self._stop.wait(self.retry_s)
                continue
            try:
                conn = socket.create_connection(target, timeout=2)
            except OSError:
                self._stop.wait(self.retry_s)
                continue
            self.connections += 1
            self.connected_to = target
            self.state = "streaming"
            try:
                while not self._stop.is_set():
                    if self.stop_after_s is not None and time.monotonic() - started >= self.stop_after_s:
                        self.state = "power pulled"   # like pulling the wearable's power: silence, then the socket goes
                        self._stop.set()
                        break
                    try:
                        line = self.sim.read_line(0.2)
                    except (EndOfStream, AdapterDisconnected):
                        self.sim.close()
                        self.sim.open()
                        continue
                    if line is None:
                        continue
                    conn.sendall(line if line.endswith(b"\n") else line + b"\n")
                    self.sent += 1
            except OSError:
                self.state = "connection lost"
                self._stop.wait(self.retry_s)   # then discover and connect again, like the firmware
            finally:
                conn.close()
        self.state = self.state if self.state == "power pulled" else "stopped"
