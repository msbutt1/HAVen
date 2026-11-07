"""Controller link: the REAL adapter path, plus a simulated controller to test it without hardware.

PROPOSED line protocol between the laptop and the station controller (one JSON
object per line, UTF-8, NDJSON). Salaar owns the firmware side; nothing here
has run against hardware. See ``contract/STATION_CONTRACT.md``.

Host -> controller (``vibeguard.station_command``):
    hello            {command_id}                                   (ask the controller to identify itself)
    deliver_stimulus {command_id, trial_id, setting, duration_ms}   (setting 0 = motor off)
    stop_stimulus    {command_id}                                   (motor off NOW)

Controller -> host (``vibeguard.station_link``):
    hello             {command_id?, firmware: {name, version}, setting_range: {min, max}}
                      (sent on boot and in answer to ``hello``; the range is the motor settings
                      the controller accepts, 0 = off. A hello with no request means it rebooted.)
    ack               {command_id, status: accepted | rejected | completed, reason?}
    stimulus_started  {trial_id, device_t_us}
    stimulus_ended    {trial_id, device_t_us}
    response          {response: felt | not_felt, device_t_us, trial_id?}
    error             {message}

``stop_stimulus`` is answered with ``ack`` status ``completed`` once the motor is
confirmed off. Event times are stamped on the host clock when received; the
device time is kept for diagnostics only (link latency is therefore part of the
measured response latency).
"""

from __future__ import annotations

import json
from typing import Callable, Protocol

from .adapters import AdapterError, AdapterEvent, SimulatedStationAdapter

COMMAND_SCHEMA = "vibeguard.station_command"
LINK_SCHEMA = "vibeguard.station_link"
VERSION = 1
MAX_LINE_CHARS = 4096  # longest line parsed; real messages are under 400 characters


class LineTransport(Protocol):
    """Whatever carries the lines (USB serial later; an in-memory fake now)."""

    def write_line(self, line: str) -> None: ...

    def read_lines(self) -> list[str]: ...  # non-blocking; may return []


def encode(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n"


class LinkStationAdapter:
    """Station adapter speaking the controller line protocol over any ``LineTransport``."""

    def __init__(self, transport: LineTransport, clock: Callable[[], float],
                 sleep: Callable[[float], None], *, ack_timeout_s: float = 1.0,
                 stop_ack_timeout_s: float = 0.5, detail: dict | None = None):
        self._tx = transport
        self._clock = clock
        self._sleep = sleep
        self.ack_timeout_s = ack_timeout_s
        self.stop_ack_timeout_s = stop_ack_timeout_s
        self._detail = dict(detail or {})
        self._cmd_no = 0
        self._awaiting_ack: dict[str, float] = {}  # command_id -> time sent
        self._buffer: list[AdapterEvent] = []
        self.malformed_lines = 0  # garbage on the link is counted, never used
        self.device_info: dict | None = None  # from the controller's hello
        self._handshake_done = False
        self._hello_ids: set[str] = set()  # replies to these are ours, not a reboot

    @property
    def provenance(self) -> dict:
        # Fails closed: "live" only when the transport explicitly says it is real hardware
        # (``is_simulated = False``). Anything else, including a transport that says nothing, is
        # labelled simulated, so a fake can never present itself as live.
        real = getattr(self._tx, "is_simulated", True) is False
        prov = {"source": "live" if real else "simulated"}
        detail = dict(self._detail)
        if self.device_info:
            fw = self.device_info["firmware"]
            detail["firmware"] = f"{fw['name']} {fw['version']}"
        if detail:
            prov["detail"] = detail
        return prov

    # -- commands ---------------------------------------------------------------

    def _send(self, cmd: dict) -> str:
        self._cmd_no += 1
        cid = f"c{self._cmd_no:05d}"
        msg = {"schema": COMMAND_SCHEMA, "version": VERSION, "command_id": cid, **cmd}
        try:
            self._tx.write_line(encode(msg))
        except OSError as exc:
            raise AdapterError(f"link write failed: {exc}") from exc
        return cid

    def deliver_stimulus(self, trial_id: str, setting: int, duration_ms: int) -> None:
        if self.device_info and setting != 0:
            rng = self.device_info["setting_range"]
            if not rng["min"] <= setting <= rng["max"]:
                raise AdapterError(f"setting {setting} is outside the controller's range "
                                   f"{rng['min']}-{rng['max']}")
        cid = self._send({"type": "deliver_stimulus", "trial_id": trial_id,
                          "setting": int(setting), "duration_ms": int(duration_ms)})
        self._awaiting_ack[cid] = self._clock()

    def request_hello(self, timeout_s: float = 5.0, retry_s: float = 1.0) -> dict:
        """Handshake: learn the controller's firmware and motor-setting range (blocks briefly).

        A boot-time hello already in flight also satisfies it. Raises ``AdapterError`` if the
        controller does not answer or reports an unusable range.
        """
        # Resend while waiting: an ESP32 often resets when the port opens and misses the first one.
        # Its boot-time hello (no command_id) also counts.
        self._hello_ids.add(self._send({"type": "hello"}))
        now = self._clock()
        deadline, next_try = now + timeout_s, now + retry_s
        while True:
            answer = None
            for msg in self._read():
                if msg.get("type") == "hello" and answer is None:
                    answer = msg
                else:  # keep everything else in the batch (a button press right after the hello)
                    self._buffer.extend(self._to_events([msg]))
            if answer is not None:
                self.device_info = self._parse_hello(answer)
                self._handshake_done = True
                return self.device_info
            now = self._clock()
            if now >= deadline:
                raise AdapterError("controller did not answer hello (check port, baud rate and firmware)")
            if now >= next_try:
                self._hello_ids.add(self._send({"type": "hello"}))
                next_try = now + retry_s
            self._sleep(0.01)

    @staticmethod
    def _parse_hello(msg: dict) -> dict:
        fw, rng = msg.get("firmware"), msg.get("setting_range")
        ok = (isinstance(fw, dict) and isinstance(fw.get("name"), str) and isinstance(fw.get("version"), str)
              and isinstance(rng, dict) and all(isinstance(rng.get(k), int) and not isinstance(rng.get(k), bool)
                                                for k in ("min", "max")))
        if not ok:
            raise AdapterError("controller hello is malformed")
        if not 1 <= rng["min"] < rng["max"]:
            raise AdapterError(f"controller reports an unusable setting range {rng['min']}-{rng['max']}")
        return {"firmware": {"name": fw["name"], "version": fw["version"]},
                "setting_range": {"min": rng["min"], "max": rng["max"]}}

    def stop_stimulus(self) -> None:
        """Send stop and wait (briefly) for the motor-off confirmation."""
        cid = self._send({"type": "stop_stimulus"})
        deadline = self._clock() + self.stop_ack_timeout_s
        while True:
            for msg in self._read():
                if msg.get("type") == "ack" and msg.get("command_id") == cid:
                    if msg.get("status") == "completed":
                        return
                    raise AdapterError(f"stop rejected: {msg.get('reason') or msg.get('status')}")
                self._buffer.extend(self._to_events([msg]))
            if self._clock() >= deadline:
                raise AdapterError("stop not acknowledged by the controller")
            self._sleep(0.005)

    # -- events -----------------------------------------------------------------

    def _read(self) -> list[dict]:
        try:
            lines = self._tx.read_lines()
        except OSError as exc:
            raise AdapterError(f"link read failed: {exc}") from exc
        out = []
        for raw in lines:
            if len(raw) > MAX_LINE_CHARS:  # no valid message is anywhere near this long
                self.malformed_lines += 1
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, RecursionError):  # e.g. 100000 nested brackets
                self.malformed_lines += 1
                continue
            if (not isinstance(msg, dict) or msg.get("schema") != LINK_SCHEMA
                    or msg.get("version") != VERSION or not isinstance(msg.get("type"), str)):
                self.malformed_lines += 1
                continue
            out.append(msg)
        return out

    @staticmethod
    def _fields_ok(m: dict) -> bool:
        """Controller fields must have the right types; anything else is garbage, not a crash."""
        for key in ("command_id", "trial_id", "response", "reason", "message", "status"):
            if m.get(key) is not None and not isinstance(m[key], str):
                return False
        return True

    def _to_events(self, msgs: list[dict]) -> list[AdapterEvent]:
        now = self._clock()
        events = []
        for m in msgs:
            kind = m["type"]
            if not self._fields_ok(m):
                self.malformed_lines += 1
                continue
            if kind == "ack":
                if m.get("status") not in ("accepted", "rejected", "completed"):
                    self.malformed_lines += 1  # an ack with no usable status acknowledges nothing
                    continue
                self._awaiting_ack.pop(m.get("command_id"), None)
                if m.get("status") == "rejected":
                    events.append(AdapterEvent("fault", now, detail=f"command rejected: {m.get('reason')}"))
            elif kind in ("stimulus_started", "stimulus_ended"):
                events.append(AdapterEvent(kind, now, trial_id=m.get("trial_id")))
            elif kind == "response":
                events.append(AdapterEvent("response", now, response=m.get("response"),
                                           source="button", trial_id=m.get("trial_id")))
            elif kind == "hello":
                # Only reached when nobody asked: after the handshake this means the controller
                # rebooted (the motor is off, the trial is lost).
                if self._handshake_done and m.get("command_id") not in self._hello_ids:
                    events.append(AdapterEvent("fault", now, detail="controller restarted during the session"))
            elif kind == "error":
                events.append(AdapterEvent("fault", now, detail=f"controller error: {m.get('message')}"))
        return events

    def poll(self) -> list[AdapterEvent]:
        events, self._buffer = self._buffer, []
        events += self._to_events(self._read())
        now = self._clock()
        for cid, sent in list(self._awaiting_ack.items()):
            if now - sent > self.ack_timeout_s:
                del self._awaiting_ack[cid]
                events.append(AdapterEvent("fault", now, detail="stimulus command not acknowledged"))
        return events


class SimulatedController:
    """In-memory stand-in for the controller firmware, speaking the link protocol.

    Wraps ``SimulatedStationAdapter`` so scripted/synthetic responders drive it.
    Use as the ``transport`` of a ``LinkStationAdapter`` to test the real adapter
    path without hardware. Everything it produces is simulated.
    """

    is_simulated = True  # read by LinkStationAdapter so results are labelled simulated

    def __init__(self, clock: Callable[[], float], sim: SimulatedStationAdapter, *,
                 setting_range: tuple[int, int] = (1, 100)):
        self._clock = clock
        self.sim = sim
        self.setting_range = setting_range
        self._out: list[str] = []
        self.received: list[dict] = []
        self.reject_commands = False
        self.silent = False  # never acknowledge (simulates a dead controller)

    def write_line(self, line: str) -> None:
        msg = json.loads(line)
        self.received.append(msg)
        if self.silent:
            return
        cid = msg["command_id"]
        if msg["type"] == "hello":
            lo, hi = self.setting_range
            self._out.append(encode({"schema": LINK_SCHEMA, "version": VERSION, "type": "hello",
                                     "command_id": cid,
                                     "firmware": {"name": "simulated-station-controller", "version": "0"},
                                     "setting_range": {"min": lo, "max": hi}}))
        elif msg["type"] == "deliver_stimulus":
            if self.reject_commands:
                self._out.append(encode({"schema": LINK_SCHEMA, "version": VERSION, "type": "ack",
                                         "command_id": cid, "status": "rejected", "reason": "simulated rejection"}))
                return
            self._out.append(encode({"schema": LINK_SCHEMA, "version": VERSION, "type": "ack",
                                     "command_id": cid, "status": "accepted"}))
            self.sim.deliver_stimulus(msg["trial_id"], msg["setting"], msg["duration_ms"])
        elif msg["type"] == "stop_stimulus":
            self.sim.stop_stimulus()
            self._out.append(encode({"schema": LINK_SCHEMA, "version": VERSION, "type": "ack",
                                     "command_id": cid, "status": "completed"}))

    def read_lines(self) -> list[str]:
        for ev in self.sim.poll():
            body = {"trial_id": ev.trial_id, "device_t_us": int(ev.t_s * 1_000_000)}
            if ev.kind == "response":
                body["response"] = ev.response
            self._out.append(encode({"schema": LINK_SCHEMA, "version": VERSION, "type": ev.kind, **body}))
        out, self._out = self._out, []
        return out

    def next_event_time(self) -> float | None:
        return self.sim.next_event_time()
