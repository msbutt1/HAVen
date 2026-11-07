"""Hardware bring-up tools: list ports, check a real serial link, rehearse without hardware.

    vg-backend ports                                   # what is plugged in
    vg-backend serial-check --port '/dev/cu.usbmodem*' --baud 921600 --seconds 30 --save rec.ndjson
    vg-backend serial-check --port ... --try-bauds 115200,460800,921600   # find the baud rate
    vg-backend fake-device                             # a pseudo-terminal that behaves like the firmware

``serial-check`` answers the HANDOFF section 7 questions from a real link: is it sending contract
frames, is the measured sample rate the nominal one, are records or samples being lost, is the EMG
module sending raw EMG (not an envelope), is there mains hum, and does the link have the bandwidth.
It runs the same validator and processor the backend uses, so a PASS means the backend will accept it.
Nothing is assumed: an unknown hardware fact is reported as unresolved, never defaulted.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
from vibeguard_signals import contract
from vibeguard_signals.processing import ProcessingConfig, Processor

from .adapters import AdapterDisconnected, SerialAdapter
from .preflight import FAIL, PASS, WARN, Report

#: vendor ids of common microcontroller boards / USB-serial bridges (a hint, not a guarantee)
KNOWN_MCU_VENDORS = {
    0x2341: "Arduino", 0x2A03: "Arduino", 0x1A86: "WCH CH340/CH9102", 0x10C4: "Silicon Labs CP210x",
    0x0403: "FTDI", 0x303A: "Espressif", 0x239A: "Adafruit", 0x2E8A: "Raspberry Pi", 0x16C0: "Teensy/PJRC",
    0x0483: "STMicroelectronics", 0x1366: "SEGGER J-Link", 0x04D8: "Microchip", 0x1915: "Nordic",
}
STANDARD_BAUDS = (115200, 230400, 460800, 921600, 1000000, 2000000)
#: sample-rate verdict thresholds, shared by serial-check and preflight so they never disagree
RATE_PASS, RATE_WARN = 0.01, 0.05
NATIVE_USB = re.compile(r"(usbmodem|ttyACM)", re.I)
MAX_ANALYSED_SAMPLES = 20000


# ---------------------------------------------------------------------------------- ports

def list_ports() -> list[dict]:
    """Serial ports with what the OS knows about them; ``likely_board`` is a hint from the vendor id."""
    from serial.tools import list_ports as lp

    out = []
    for p in sorted(lp.comports(), key=lambda x: x.device):
        vendor = KNOWN_MCU_VENDORS.get(p.vid) if p.vid is not None else None
        out.append({"device": p.device, "description": p.description, "manufacturer": p.manufacturer,
                    "product": p.product, "serial_number": p.serial_number,
                    "vid": None if p.vid is None else f"0x{p.vid:04X}",
                    "pid": None if p.pid is None else f"0x{p.pid:04X}",
                    "likely_board": vendor, "is_usb": p.vid is not None})
    return out


def render_ports(ports: list[dict]) -> str:
    if not ports:
        return "no serial ports found. Plug the board in with a data cable (some cables are power-only)."
    lines = []
    for p in ports:
        tag = f"  <- looks like {p['likely_board']}" if p["likely_board"] else ("  (USB)" if p["is_usb"] else "")
        usb = f" [{p['vid']}:{p['pid']}]" if p["vid"] else ""
        lines.append(f"  {p['device']:<34}{p['description']}{usb}{tag}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------- profile

class ProfileError(ValueError):
    """The hardware profile is missing something or contradicts the backend's assumptions."""


@dataclass(frozen=True)
class HardwareProfile:
    """The hardware facts Collins and Salaar confirm, in one file (see hardware.example.json)."""
    name: str
    port: str | None = None
    baud: int | None = None
    emg_output: str = "raw"             # raw | envelope: the pipeline assumes raw EMG
    emg_channels: int | None = None
    mains_hz: int | None = None         # 50 or 60
    imu_model: str | None = None
    imu_counts_per_g: float | None = None
    imu_range_g: float | None = None
    imu_conversion_source: str | None = None
    dtr: bool | None = None             # False: do not assert DTR at open (auto-reset boards)
    rts: bool | None = None
    notes: str | None = None
    raw: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return {k: v for k, v in self.raw.items()}


TOP_KEYS = {"name", "port", "baud", "emg", "imu", "serial", "notes", "_comment"}
EMG_KEYS = {"output", "channels", "mains_hz"}
IMU_KEYS = {"model", "range_g", "counts_per_g", "conversion_source"}
SERIAL_KEYS = {"dtr", "rts"}
COUNTS_PER_G_RANGE = (1.0, 1e6)
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def validate_baud(baud: Any, where: str = "baud") -> int:
    if isinstance(baud, bool) or not isinstance(baud, int) or not 1200 <= baud <= 4_000_000:
        raise ProfileError(f"{where} must be an integer between 1200 and 4000000")
    return baud


def validate_counts_per_g(cpg: Any, where: str = "imu.counts_per_g") -> float:
    if isinstance(cpg, bool) or not isinstance(cpg, (int, float)):
        raise ProfileError(f"{where} must be a positive number")
    try:
        v = float(cpg)
    except OverflowError:
        raise ProfileError(f"{where} is absurdly large") from None
    if not math.isfinite(v) or v <= 0:
        raise ProfileError(f"{where} must be a positive number")
    if not COUNTS_PER_G_RANGE[0] <= v <= COUNTS_PER_G_RANGE[1]:
        raise ProfileError(f"{where} {v:g} is outside the plausible range {COUNTS_PER_G_RANGE[0]:g} to "
                           f"{COUNTS_PER_G_RANGE[1]:g} counts per g: check the datasheet value and the range setting")
    return v


def _text(value: Any, where: str, maxlen: int, minlen: int = 1, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{where} must be text")
    t = value.strip()
    if len(t) < minlen:
        raise ProfileError(f"{where} must not be empty" if minlen == 1 else f"{where} must be at least {minlen} characters")
    if len(t) > maxlen:
        raise ProfileError(f"{where} is longer than {maxlen} characters")
    if (_CTRL.search(t) if multiline else re.search(r"[\x00-\x1f\x7f]", t)):
        raise ProfileError(f"{where} contains control characters")
    return t


def load_profile(path: str | Path) -> HardwareProfile:
    p = Path(path)
    try:
        if p.stat().st_size > 65536:
            raise ProfileError(f"{p} is larger than 64 KB: it should be a short list of facts")
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ProfileError(f"cannot read {p}: {exc}") from exc
    except ValueError as exc:
        if isinstance(exc, ProfileError):
            raise
        raise ProfileError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{p} must be a JSON object")
    unknown = sorted(set(data) - TOP_KEYS)
    if unknown:
        raise ProfileError(f"unknown field(s) {unknown}: a typo here would silently change what the backend assumes")
    emg, imu, ser = data.get("emg", {}), data.get("imu", {}), data.get("serial", {})
    emg, imu, ser = ({} if x is None else x for x in (emg, imu, ser))
    if not all(isinstance(x, dict) for x in (emg, imu, ser)):
        raise ProfileError("'emg', 'imu' and 'serial' must be objects")
    for label, blk, allowed in (("emg", emg, EMG_KEYS), ("imu", imu, IMU_KEYS), ("serial", ser, SERIAL_KEYS)):
        extra = sorted(set(blk) - allowed)
        if extra:
            raise ProfileError(f"unknown field(s) {extra} in '{label}' (allowed: {sorted(allowed)}): a typo here would "
                               "silently change what the backend assumes")
    if "name" not in data:
        raise ProfileError("'name' is required (it is stored with every recording)")
    name = _text(data["name"], "'name'", 80)
    port = data.get("port")
    if port is not None:
        port = _text(port, "'port'", 200)
    baud = validate_baud(data["baud"]) if data.get("baud") is not None else None
    output = emg.get("output")
    if output == "envelope":
        raise ProfileError("the EMG module outputs an ENVELOPE, but the processing pipeline assumes RAW EMG "
                           "(it band-passes and takes its own RMS). Stop and adapt the pipeline with Salaar "
                           "before connecting it; see signals/HANDOFF.md section 7")
    if output is not None and output != "raw":
        raise ProfileError("emg.output must be 'raw' (or 'envelope', which is refused)")
    mains = emg.get("mains_hz")
    if mains is not None and (isinstance(mains, bool) or mains not in (50, 60)):
        raise ProfileError("emg.mains_hz must be 50 or 60")
    channels = emg.get("channels")
    if channels is not None and (isinstance(channels, bool) or not isinstance(channels, int) or not 1 <= channels <= 8):
        raise ProfileError("emg.channels must be an integer from 1 to 8")
    range_g = imu.get("range_g")
    if range_g is not None and (isinstance(range_g, bool) or not isinstance(range_g, (int, float))
                                or not math.isfinite(range_g) or not 0 < range_g <= 1000):
        raise ProfileError("imu.range_g must be a positive number (in g)")
    model = _text(imu["model"], "imu.model", 80) if imu.get("model") is not None else None
    cpg = imu.get("counts_per_g")
    src = imu.get("conversion_source")
    if cpg is not None:
        cpg = validate_counts_per_g(cpg)
        if not isinstance(src, str) or len(src.strip()) < 8:
            raise ProfileError("imu.counts_per_g needs imu.conversion_source: where the sensitivity comes from "
                               "(datasheet page and the configured range), so m/s^2 values are never uncited")
    if src is not None:
        src = _text(src, "imu.conversion_source", 300, minlen=8)
        if cpg is None:
            raise ProfileError("imu.conversion_source without imu.counts_per_g: it would cite a datasheet for no value")
    dtr, rts = ser.get("dtr"), ser.get("rts")
    for k, v in (("dtr", dtr), ("rts", rts)):
        if v is not None and not isinstance(v, bool):
            raise ProfileError(f"serial.{k} must be true or false")
    notes = _text(data["notes"], "'notes'", 2000, minlen=0, multiline=True) if data.get("notes") is not None else None
    if output is None:
        raise ProfileError("emg.output is required: say \"raw\" only once the EMG module is confirmed to output raw "
                           "EMG (the pipeline assumes it; `serial-check` tests it). Nothing is assumed")
    return HardwareProfile(
        name=name, port=port, baud=baud, emg_output=output, emg_channels=channels, mains_hz=mains,
        imu_model=model, imu_counts_per_g=cpg, imu_range_g=range_g, imu_conversion_source=src,
        dtr=dtr, rts=rts, notes=notes, raw=data)


# ---------------------------------------------------------------------------------- serial check

@dataclass
class _Seg:
    """One uninterrupted stretch of one stream (one boot): rates are only valid within a segment."""
    batches: int = 0
    samples: int = 0
    nominal_hz: float | None = None
    first_index: int | None = None
    last_index_end: int | None = None
    t_first_us: float | None = None
    t_last_end_us: float | None = None
    first_arrival: float | None = None
    last_arrival: float | None = None
    samples_after_first: int = 0
    index_gap_samples: int = 0
    channels: list[str] = field(default_factory=list)


@dataclass
class _StreamAcc:
    """Everything kept for one stream across the whole check."""
    segs: list[_Seg] = field(default_factory=lambda: [_Seg()])
    values: list[list[float]] = field(default_factory=list)  # first MAX_ANALYSED_SAMPLES valid values per channel
    raw_limits: dict | None = None
    total_values: int = 0
    null_values: int = 0
    vmin: float = math.inf
    vmax: float = -math.inf
    n_min: int = 0
    n_max: int = 0
    channels: list[str] = field(default_factory=list)

    @property
    def seg(self) -> _Seg:
        return self.segs[-1]

    def new_segment(self) -> None:
        if self.seg.batches:
            self.segs.append(_Seg())


@dataclass
class SerialCheckResult:
    port: str
    baud: int
    seconds: float
    report: Report
    stats: dict


def _timing_span(rec: dict) -> tuple[float, float, float]:
    """(first sample time, last sample time, interval) in device microseconds for one batch."""
    t, n = rec["timing"], len(rec["samples"])
    if t["mode"] == "uniform":
        return t["t0_us"], t["t0_us"] + (n - 1) * t["interval_us"], t["interval_us"]
    ts = t["t_us"]
    return ts[0], ts[-1], (ts[-1] - ts[0]) / max(n - 1, 1)


def _classify_open_error(msg: str) -> str:
    m = msg.lower()
    if "permission" in m or "errno 13" in m:
        return ("no permission to open the port. Linux: add yourself to the serial group "
                "(sudo usermod -aG dialout $USER, then log out and in); macOS: nothing else should need rights")
    if "exclusively lock" in m or "busy" in m or "resource temporarily" in m or "errno 35" in m or "errno 11" in m:
        return ("another program has the port: close the Arduino/PlatformIO serial monitor, another vg-backend, "
                "or another serial-check, then rerun")
    if "inappropriate ioctl" in m or "invalid argument" in m or "errno 25" in m or "errno 22" in m:
        return ("the port refused this baud rate or is not a real serial port. A pseudo-port (fake-device) only "
                "accepts some rates: try 115200 or 230400")
    return "check the cable (some are power-only) and the name: uv run vg-backend ports"


def serial_check(port: str, baud: int = 921600, seconds: float = 20.0, save: str | Path | None = None,
                 mains_hz: int | None = None, adapter_factory: Callable[[str, int], SerialAdapter] | None = None,
                 progress: Callable[[str], None] | None = None, dtr: bool | None = None, rts: bool | None = None,
                 profile: HardwareProfile | None = None) -> SerialCheckResult:
    """Read a real serial link for ``seconds`` and report what the backend would see."""
    rep = Report()
    say = progress or (lambda _m: None)
    if profile is not None:
        mains_hz = mains_hz or profile.mains_hz
        dtr = profile.dtr if dtr is None else dtr
        rts = profile.rts if rts is None else rts
    factory = adapter_factory or (lambda p, b: SerialAdapter(p, b, dtr=dtr, rts=rts))

    out = None
    if save:  # open the capture file first: a bad path must not leave the serial port open
        try:
            out = Path(save).open("wb")
        except OSError as exc:
            rep.add(FAIL, "save file", f"cannot write {save}: {exc}", "pick a writable folder for --save")
            return SerialCheckResult(port, baud, seconds, rep, {})
    adapter = factory(port, baud)
    try:
        adapter.open()
    except AdapterDisconnected as exc:
        if out:
            out.close()
        rep.add(FAIL, "port opens", str(exc), _classify_open_error(str(exc)))
        if "no port matches" in str(exc):
            rep.add(WARN, "ports found", ", ".join(p["device"] for p in list_ports()) or "none",
                    "use one of these names, or fix the glob")
        return SerialCheckResult(port, baud, seconds, rep, {})
    rep.add(PASS, "port opens", f"{adapter.port} at {baud} baud")
    cands = getattr(adapter, "candidates", [])
    if len(cands) > 1:
        rep.add(WARN, "port is ambiguous", f"'{port}' matched {len(cands)} ports; using {adapter.port} "
                f"(others: {', '.join(cands[1:])})", "name the exact port, or unplug the other device")

    proc = Processor(ProcessingConfig(clock="host"))
    acc: dict[str, _StreamAcc] = {"emg": _StreamAcc(), "imu": _StreamAcc()}
    invalid, invalid_examples = Counter(), {}
    flags: Counter = Counter()
    status_states: Counter = Counter()
    provenance: Counter = Counter()
    hello: dict | None = None
    seq_last: dict[str, int] = {}
    seq_missing = seq_dup = reboots = unannounced = sample_gap_total = dropped_before = 0
    bytes_total = lines = valid = longest = binary_lines = json_lines = 0
    first_valid_at = first_byte_at = last_valid_at = None
    junk_since_valid = 0  # lines received since the last valid frame
    gaps: list[float] = []
    max_gap = (0.0, 0.0)  # (length, at)
    events: Counter = Counter()
    interrupted = False
    t0 = time.monotonic()
    end = t0 + seconds
    next_say = t0 + 5
    try:
        while time.monotonic() < end:
            try:
                line = adapter.read_line(0.2)
            except AdapterDisconnected as exc:
                rep.add(FAIL, "link stays up", f"lost the port after {time.monotonic() - t0:.1f} s ({exc})",
                        "check the cable, the connector and the board's power (brown-outs reset USB)")
                break
            now = time.monotonic()
            if line is None:
                if now >= next_say:
                    say(f"  ... {now - t0:4.0f} s, {lines} lines, {valid} valid")
                    next_say = now + 5
                continue
            if first_byte_at is None:
                first_byte_at = now - t0
            lines += 1
            bytes_total += len(line)
            longest = max(longest, len(line))
            if out:
                out.write(line if line.endswith(b"\n") else line + b"\n")
            try:
                rec, issues = contract.parse_line(line)
            except (RecursionError, ValueError, TypeError):
                rec, issues = None, [contract.Issue("malformed_json", "", "undecodable")]
            if rec is None or issues:
                junk_since_valid += 1
                code = issues[0].code if issues else "invalid"
                invalid[code] += 1
                invalid_examples.setdefault(code, line[:70])
                if any(b >= 0x80 or b == 0 for b in line[:200]):
                    binary_lines += 1
                elif line.lstrip()[:1] == b"{":
                    json_lines += 1
                continue
            valid += 1
            junk_since_valid = 0
            at = now - t0
            if last_valid_at is not None:
                g = at - last_valid_at
                if len(gaps) < 5000:
                    gaps.append(g)
                if g > max_gap[0]:
                    max_gap = (g, last_valid_at)
            last_valid_at = at
            if first_valid_at is None:
                first_valid_at = at
            provenance[rec["provenance"]["source"]] += 1
            key = rec["boot_id"]
            if key in seq_last:
                if rec["seq"] > seq_last[key] + 1:
                    seq_missing += rec["seq"] - seq_last[key] - 1
                elif rec["seq"] <= seq_last[key]:
                    if seq_last[key] >= 8 and rec["seq"] <= seq_last[key] // 2 and rec["type"] != "hello":
                        # seq fell sharply with the same boot_id: the firmware restarted without a new boot_id
                        unannounced += 1
                        reboots += 1
                        for a in acc.values():
                            a.new_segment()
                    else:
                        seq_dup += 1
                        continue
            elif seq_last:
                reboots += 1
                for a in acc.values():
                    a.new_segment()
            seq_last[key] = rec["seq"]
            for out_rec in proc.feed(rec, rx_time_s=now):
                if out_rec["type"] == "stream_event":
                    events[out_rec["event"]] += 1
            kind = rec["type"]
            if kind == "hello":
                hello = rec
                for sname in ("emg", "imu"):
                    acc[sname].raw_limits = (rec["streams"].get(sname) or {}).get("raw_limits")
            elif kind == "status":
                status_states[rec["state"]] += 1
            elif kind in ("emg_batch", "imu_batch"):
                a = acc["emg" if kind == "emg_batch" else "imu"]
                sg = a.seg
                first, last, _ = _timing_span(rec)
                idx = rec["first_sample_index"]
                n = len(rec["samples"])
                if sg.last_index_end is not None and idx < sg.last_index_end - 1000:
                    a.new_segment()  # the sample counter restarted: a new stretch (e.g. an unannounced reset)
                    sg = a.seg
                sg.batches += 1
                sg.samples += n
                sg.nominal_hz = rec["sample_rate_hz"]
                sg.channels = rec["channels"]
                a.channels = rec["channels"]
                if sg.first_index is None:
                    sg.first_index, sg.t_first_us, sg.first_arrival = idx, first, now
                else:
                    sg.samples_after_first += n
                    if idx > sg.last_index_end:
                        sg.index_gap_samples += idx - sg.last_index_end
                        sample_gap_total += idx - sg.last_index_end
                sg.last_index_end = idx + n
                sg.t_last_end_us = last
                sg.last_arrival = now
                for f in rec["quality"]["flags"]:
                    flags[f"{kind[:3]}:{f}"] += 1
                dropped_before += rec["quality"].get("dropped_samples_before", 0)
                if len(a.values) < len(rec["channels"]):
                    a.values = [[] for _ in rec["channels"]]
                for row in rec["samples"]:
                    for ci, v in enumerate(row):
                        a.total_values += 1
                        if v is None:
                            a.null_values += 1
                            continue
                        if v < a.vmin:
                            a.vmin, a.n_min = v, 0
                        if v > a.vmax:
                            a.vmax, a.n_max = v, 0
                        a.n_min += v == a.vmin
                        a.n_max += v == a.vmax
                        if len(a.values[ci]) < MAX_ANALYSED_SAMPLES:
                            a.values[ci].append(float(v))
    except KeyboardInterrupt:
        interrupted = True
    finally:
        adapter.close()
        if out:
            out.close()
    elapsed = max(time.monotonic() - t0, 1e-9)

    # ---- verdicts ---------------------------------------------------------------------------
    stats: dict[str, Any] = {"seconds": round(elapsed, 2), "lines": lines, "valid": valid, "bytes": bytes_total,
                             "bytes_per_s": round(bytes_total / elapsed), "invalid": dict(invalid),
                             "seq_missing": seq_missing, "seq_duplicates": seq_dup, "reboots": reboots,
                             "unannounced_reboots": unannounced, "dropped_samples": dropped_before,
                             "sample_index_gaps": sample_gap_total, "events": dict(events), "flags": dict(flags),
                             "provenance": dict(provenance), "interrupted": interrupted}
    if interrupted:
        rep.add(WARN, "check interrupted", f"stopped by Ctrl-C after {elapsed:.0f} s of {seconds:g} s",
                "the verdicts below cover only what was read")
    if bytes_total == 0:
        rep.add(FAIL, "data arrives", f"no bytes in {elapsed:.0f} s",
                "the firmware is not sending: check it is running and the TX pin/USB CDC is enabled. This tool only "
                "LISTENS (it sends nothing): a firmware that waits for a command, or for DTR (`while(!Serial)`), "
                "will look silent. Some boards reset on connect: wait a few seconds, or raise --seconds")
        return SerialCheckResult(port, baud, seconds, rep, stats)
    rep.add(PASS, "data arrives", f"{bytes_total} bytes, {lines} lines, first after {first_byte_at:.2f} s")
    rejected = lines - valid
    first_bad = next(iter(invalid_examples.values()), b"")
    pd = getattr(adapter, "partial_discarded", 0)
    stats["first_line_discarded"] = pd
    if valid == 0:
        if binary_lines > 0.5 * max(rejected, 1):
            hint = ("the bytes are not text: this looks like a WRONG BAUD RATE (or NUL padding). Try: --try-bauds "
                    + ",".join(str(b) for b in STANDARD_BAUDS[:4]))
        elif json_lines > 0.5 * max(rejected, 1):
            hint = ("the lines are JSON but break the contract: save a capture with --save and run "
                    "`uv run --project ../signals vg-validate` on it for the exact field")
        else:
            hint = ("the firmware sends text that is not contract v1 NDJSON (debug prints on the same port?); "
                    "see signals/contract/TELEMETRY_CONTRACT.md")
        rep.add(FAIL, "contract frames", f"0 of {lines} lines are valid ({dict(invalid.most_common(3))})",
                f"{hint}. First rejected line: {first_bad!r}")
        return SerialCheckResult(port, baud, seconds, rep, stats)
    frac = valid / lines
    if frac >= 0.995:
        rep.add(PASS, "contract frames", f"{valid}/{lines} valid, first valid after {first_valid_at:.2f} s")
    else:
        if binary_lines > 0.5 * rejected:
            why = "binary bytes: a wrong or marginal baud rate, or NUL padding"
        elif json_lines > 0.5 * rejected:
            why = ("the lines are JSON that break the contract (a firmware bug, not the baud rate): save with --save "
                   "and run vg-validate")
        else:
            why = "text that is not JSON: debug prints on the same port"
        rep.add(WARN if frac >= 0.95 else FAIL, "contract frames",
                f"{rejected} of {lines} lines rejected: {dict(invalid.most_common(3))}",
                f"{why}. First rejected line: {first_bad!r}")
    if longest > contract.MAX_LINE_BYTES * 0.5:
        rep.add(WARN, "frame size", f"longest line {longest} bytes (limit {contract.MAX_LINE_BYTES})", "send smaller batches")

    # provenance: the firmware must say live
    if provenance:
        srcs = dict(provenance)
        if set(srcs) == {"live"}:
            rep.add(PASS, "data labelled live", f"{valid} records, provenance live")
        elif "replay" in srcs:
            rep.add(FAIL, "data labelled live", f"provenance {srcs}",
                    "firmware must send provenance {\"source\":\"live\"}; 'replay' is only produced by the backend")
        else:
            rep.add(WARN, "data labelled live", f"provenance {srcs}",
                    "real firmware must send provenance {\"source\":\"live\"} (a 'simulated' label is for the "
                    "simulator, and the backend would label every recording simulated)")

    if hello is None:
        extra = ""
        if pd:
            extra = (f" The first line after connecting was discarded ({pd}x: it did not start with '{{'), so the "
                     "firmware prints boot text before its hello.")
        elif getattr(adapter, "hello_recovered", 0):
            extra = " (a hello glued behind boot text was recovered earlier)"
        rep.add(WARN, "hello received", "no hello record seen" + extra,
                "send a hello on boot, and again when the host connects or every 1-2 s until the host answers "
                "(a firmware cannot tell on a UART bridge that a host opened the port)")
    else:
        s = hello["streams"]
        unresolved = [f"{k}.{f}" for k, v in s.items() for f in ("nominal_rate_hz", "raw_limits", "sensor_model") if v.get(f) is None]
        hw = {k: v for k, v in (hello.get("hardware") or {}).items()}
        if unresolved:
            rep.add(WARN, "hardware facts known", f"unresolved in hello: {', '.join(unresolved)}",
                    "fill these in the firmware once the parts are confirmed (clipping can only be detected with raw_limits)")
        else:
            rep.add(PASS, "hardware facts known", f"{hello['firmware']['name']} {hello['firmware']['version']}, "
                    f"board {hw.get('board')}")
        stats["hello"] = {"firmware": hello["firmware"], "hardware": hw, "streams": {k: {
            "nominal_rate_hz": v.get("nominal_rate_hz"), "channels": v.get("channels"),
            "raw_limits": v.get("raw_limits"), "sensor_model": v.get("sensor_model")} for k, v in s.items()}}
        if profile is not None and profile.emg_channels is not None:
            got = len((s.get("emg") or {}).get("channels") or [])
            if got and got != profile.emg_channels:
                rep.add(WARN, "profile matches device", f"profile says {profile.emg_channels} EMG channel(s), the "
                        f"device's hello says {got}", "correct the profile or the firmware")

    for name, a in acc.items():
        seg = max(a.segs, key=lambda s: s.samples)
        if not seg.batches:
            rep.add(WARN if name == "imu" else FAIL, f"{name} data", f"no {name} batches in {elapsed:.0f} s",
                    "start that sensor in the firmware" if name == "imu" else
                    "the EMG stream is required for calibration and the activation display")
            continue
        nominal = seg.nominal_hz
        # all-null stream: a disconnected sensor must never look fine
        valid_frac = 1.0 - (a.null_values / a.total_values if a.total_values else 0.0)
        n_valid = a.total_values - a.null_values
        stats[name] = {"batches": sum(s.batches for s in a.segs), "samples": sum(s.samples for s in a.segs),
                       "segments": len(a.segs), "nominal_hz": nominal, "valid_fraction": round(valid_frac, 4),
                       "index_gap_samples": sum(s.index_gap_samples for s in a.segs)}
        if valid_frac < 0.9 or flags.get(f"{name}:sensor_disconnected"):
            rep.add(FAIL, f"{name} values", f"{a.null_values} of {a.total_values} values are missing "
                    f"({(1 - valid_frac) * 100:.0f}%)" + (", device reports sensor_disconnected" if flags.get(f"{name}:sensor_disconnected") else ""),
                    "the sensor is unplugged, unpowered or not initialised: check its wiring and supply")
        # sample rate: only within one uninterrupted segment, never across a reboot
        span_us = (seg.t_last_end_us - seg.t_first_us) if seg.t_first_us is not None else 0
        total = (seg.last_index_end - seg.first_index) if seg.first_index is not None else 0
        measured = (total - 1) / (span_us / 1e6) if span_us > 0 and total > 1 else None
        if len(a.segs) > 1:
            stats[name]["note"] = f"rate measured on the longest of {len(a.segs)} uninterrupted segments"
        if measured is None:
            rep.add(WARN, f"{name} sample rate", "too little data in one uninterrupted stretch to measure the rate",
                    "run a longer check" + (" (the device rebooted during it)" if len(a.segs) > 1 else ""))
        else:
            dev = abs(measured - nominal) / nominal if nominal else float("nan")
            stats[name].update({"measured_hz": round(measured, 2), "deviation_pct": round(dev * 100, 2)})
            if dev <= RATE_PASS:
                rep.add(PASS, f"{name} sample rate", f"measured {measured:.1f} Hz vs nominal {nominal:g} Hz ({dev * 100:.2f}% off)")
            elif dev <= RATE_WARN:
                rep.add(WARN, f"{name} sample rate", f"measured {measured:.1f} Hz vs nominal {nominal:g} Hz ({dev * 100:.1f}% off)",
                        "report the measured rate to Salaar; filters use the declared rate (the backend measures it from timestamps)")
            else:
                rep.add(FAIL, f"{name} sample rate", f"measured {measured:.1f} Hz vs nominal {nominal:g} Hz ({dev * 100:.0f}% off)",
                        "the timestamps or the configured rate are wrong; filters would be mis-tuned. Check micros() wrap handling "
                        "and the ADC/IMU output data rate setting"
                        + (" (the device also rebooted: fix the reset first)" if reboots else ""))
            # cross-check the device's clock against the host's: honest timestamps cannot disagree with throughput
            if seg.first_arrival is not None and seg.last_arrival and seg.last_arrival - seg.first_arrival >= 3:
                host_hz = seg.samples_after_first / (seg.last_arrival - seg.first_arrival)
                stats[name]["host_measured_hz"] = round(host_hz, 2)
                if abs(host_hz - measured) / measured > 0.03:
                    rep.add(WARN, f"{name} clock vs host", f"device timestamps imply {measured:.1f} Hz but samples arrive at "
                            f"{host_hz:.1f} Hz", "the timestamps may be synthesised (index x interval) rather than read from a "
                            "clock, or the link is dropping samples")
        # clipping: counted on every sample; the limits may arrive later than the samples
        lim = a.raw_limits
        if lim and lim.get("min") is not None and a.total_values:
            clipped = (a.n_min if a.vmin <= lim["min"] else 0) + (a.n_max if a.vmax >= lim["max"] else 0)
            frac_clip = clipped / max(n_valid, 1)
            stats[name]["clipped_samples"] = clipped
            if frac_clip > 0.05:
                rep.add(FAIL, f"{name} clipping", f"{clipped} samples ({frac_clip * 100:.0f}%) at the ADC limits",
                        "the input is saturated: reduce the gain or range, or check the input")
            elif frac_clip > 0.001:
                rep.add(WARN, f"{name} clipping", f"{clipped} samples ({frac_clip * 100:.2f}%) at the ADC limits",
                        "reduce the gain/range or check the input; clipped EMG corrupts the envelope")
        elif a.total_values and name == "emg":
            rep.add(WARN, "emg clipping", "not checkable: the device did not report raw_limits",
                    "send raw_limits (ADC min/max counts) in the hello")
        analysed = len(a.values[0]) if a.values else 0
        if name == "emg" and a.values and a.values[0]:
            for ci, vals in enumerate(a.values):
                label = "emg is raw" if ci == 0 else f"emg is raw ({a.channels[ci] if ci < len(a.channels) else ci})"
                if len(vals) < 400:
                    rep.add(WARN, label, f"only {len(vals)} valid samples: not enough to judge",
                            "fix the missing values above, or run a longer check")
                else:
                    _check_emg_signal(rep, stats, np.asarray(vals), measured or nominal, lim, mains_hz,
                                      len(vals), a.total_values // max(len(a.values), 1), label=label, first=(ci == 0))
        if name == "imu" and a.values:
            means = {c: round(float(np.mean(v)), 1) for c, v in zip(a.channels, a.values) if v}
            rms = {c: round(float(np.std(v)), 1) for c, v in zip(a.channels, a.values) if v}
            stats["imu"].update({"mean_counts": means, "std_counts": rms})

    lost_note = []
    if seq_missing:
        lost_note.append(f"{seq_missing} record(s) missing (seq gaps)")
    if dropped_before:
        lost_note.append(f"{dropped_before} sample(s) dropped on the device")
    if sample_gap_total:
        lost_note.append(f"{sample_gap_total} sample(s) missing from the sample index (not flagged by the device)")
    if lost_note:
        rep.add(WARN, "no records lost", ", ".join(lost_note),
                "link or buffer problem: raise the baud rate, shrink batches, or use native USB; the firmware should "
                "report drops in quality.dropped_samples_before")
    else:
        rep.add(PASS, "no records lost", f"0 seq gaps, 0 dropped samples in {elapsed:.0f} s")
    if seq_dup:
        rep.add(WARN, "duplicate records", f"{seq_dup} repeated sequence numbers", "the firmware re-sent or reordered records")
    if reboots:
        rep.add(WARN, "device rebooted", f"{reboots} restart(s) during the check" + (f" ({unannounced} without a new boot_id: "
                "boot_id must change on every boot)" if unannounced else ""), "brown-out or watchdog reset: check power")
    bad_flags = {k: v for k, v in flags.items() if k.split(":")[1] in ("overrun", "stale", "sensor_disconnected", "timing_irregular", "missing_values")}
    if bad_flags:
        rep.add(WARN, "device-reported problems", str(dict(bad_flags)), "the firmware itself saw these; fix them at the source")

    # data must keep flowing: absolute thresholds, so a long check cannot hide a long outage
    if last_valid_at is not None:
        threshold = max(2.0, min(5.0, 10 * float(np.median(gaps)))) if gaps else 2.0
        stalled_for = elapsed - last_valid_at
        stats.update({"stalled_for_s": round(stalled_for, 2), "longest_gap_s": round(max_gap[0], 2)})
        if stalled_for > threshold and not interrupted:
            if junk_since_valid:
                rep.add(FAIL, "data keeps flowing", f"no VALID frame for the last {stalled_for:.1f} s of the {elapsed:.0f} s "
                        f"check (last at {last_valid_at:.1f} s), but {junk_since_valid} malformed lines kept arriving",
                        "the firmware is still sending, but what it sends is no longer valid contract frames (NUL padding, "
                        f"a changed baud rate, debug text?). Last rejected line: {first_bad!r}")
            else:
                rep.add(FAIL, "data keeps flowing", f"no frame for the last {stalled_for:.1f} s of the {elapsed:.0f} s "
                        f"check (last at {last_valid_at:.1f} s)",
                        "the link is up but the firmware stopped sending: a hang, a watchdog wait, or the USB bridge "
                        "stuck. The backend shows this as a silent device")
        elif max_gap[0] > threshold:
            rep.add(WARN, "data keeps flowing", f"a {max_gap[0]:.1f} s silence at {max_gap[1]:.1f} s into the check",
                    "the stream paused and resumed: a reset, a watchdog, or a stalled USB bridge")
    # link bandwidth over the ACTIVE window (silence at the start or the end must not dilute it)
    active = max((last_valid_at or elapsed) - (first_byte_at or 0.0), 0.2)
    rate_bps = bytes_total / active
    util = rate_bps / (baud / 10)
    stats["link_utilisation"] = round(util, 3)
    stats["active_bytes_per_s"] = round(rate_bps)
    if NATIVE_USB.search(str(adapter.port or port)):
        rep.add(PASS, "link bandwidth", f"{rate_bps / 1000:.1f} kB/s while data flowed (native USB: the baud setting is "
                "ignored, so there is no utilisation verdict)")
    elif util > 0.8:
        rep.add(FAIL, "link bandwidth", f"{util * 100:.0f}% of the {baud} baud link used while data flowed (8N1 assumed)",
                "it will drop data: raise the baud rate, use native USB, or send binary")
    elif util > 0.5:
        rep.add(WARN, "link bandwidth", f"{util * 100:.0f}% of the {baud} baud link used while data flowed (8N1 assumed)",
                "little headroom for bursts")
    else:
        rep.add(PASS, "link bandwidth", f"{util * 100:.0f}% of the {baud} baud link used ({rate_bps / 1000:.1f} kB/s)")
    if status_states and "error" in status_states:
        rep.add(WARN, "device state", f"device reported state 'error' {status_states['error']} time(s)", "see the firmware's status message")
    return SerialCheckResult(port, baud, seconds, rep, stats)


def _check_emg_signal(rep: Report, stats: dict, x: np.ndarray, rate: float, lim: dict | None,
                      mains_hz: int | None, analysed: int, total: int, label: str = "emg is raw",
                      first: bool = True) -> None:
    """Is this raw EMG? Spectral test on the high-passed signal, plus a one-sidedness test. Real raw EMG
    is oversampled and can have baseline wander, so autocorrelation alone cannot tell it from an envelope."""
    e = stats["emg"] if first else stats["emg"].setdefault("other_channels", {}).setdefault(label, {})
    e.update({"mean": round(float(x.mean()), 2), "std": round(float(x.std()), 2),
              "min": float(x.min()), "max": float(x.max()), "analysed_samples": analysed})
    note = f" (analysed the first {analysed} of {total} samples)" if total > analysed else ""
    if x.std() < 0.5:
        rep.add(WARN, "emg looks like a signal", f"std {x.std():.2f} counts: flat",
                "check electrode contact, the reference electrode and that the module is powered")
        return
    xc = x - x.mean()
    win = max(int(0.5 * rate), 3)
    if xc.size > 2 * win:
        csum = np.cumsum(np.insert(xc, 0, 0.0))
        trend = (csum[win:] - csum[:-win]) / win  # moving mean over ~0.5 s: removes baseline wander below ~2 Hz
        hp = xc[win // 2: win // 2 + trend.size] - trend
    else:
        hp = xc
    spec = np.abs(np.fft.rfft(hp * np.hanning(hp.size))) ** 2
    freqs = np.fft.rfftfreq(hp.size, 1.0 / rate)
    base = spec[freqs > 2].sum() + 1e-12
    hf_share = float(spec[freqs > 20].sum() / base) if rate > 80 else None
    std = float(xc.std())
    skew = float(np.mean(xc ** 3) / (std ** 3 + 1e-12))
    e.update({"power_share_above_20hz": None if hf_share is None else round(hf_share, 3), "skewness": round(skew, 2)})
    mid_off = None
    if lim and lim.get("min") is not None:
        span = lim["max"] - lim["min"]
        mid_off = abs(float(np.median(x)) - (lim["min"] + lim["max"]) / 2) / span if span else None
        e["median_offset_from_mid_scale"] = None if mid_off is None else round(mid_off, 3)
    if hf_share is None:
        rep.add(WARN, label, f"sample rate {rate:.0f} Hz is too low to judge raw versus envelope{note}",
                "EMG is normally sampled at 500 Hz or more")
    elif hf_share < 0.05:
        rep.add(FAIL, label, f"only {hf_share * 100:.1f}% of the signal power is above 20 Hz: this looks like a "
                f"smoothed ENVELOPE, not raw EMG{note}",
                "if the module outputs an envelope, STOP: the pipeline assumes raw EMG (signals/HANDOFF.md section 7). "
                "Ask Collins for the module's output type")
    elif skew > 0.8 or (mid_off is not None and mid_off > 0.3):
        rep.add(WARN, label, f"broadband, but one-sided (skewness {skew:.1f}"
                + (f", median {mid_off * 100:.0f}% of the range from mid-scale" if mid_off is not None else "") + f"): "
                f"possibly a RECTIFIED output{note}", "raw EMG is symmetric around its bias; confirm the module's output "
                "type with Collins")
    else:
        rep.add(PASS, label, f"mean {x.mean():.0f}, std {x.std():.1f}, {hf_share * 100:.0f}% of power above 20 Hz{note}")
    if not first:
        return  # hum is judged on the first channel
    # mains hum, fundamental and harmonics
    hz_candidates = [mains_hz] if mains_hz else [50, 60]
    if rate < 2.5 * max(hz_candidates):
        rep.add(PASS, "mains hum", f"not evaluated: sample rate {rate:.0f} Hz is too low")
        return
    total_p = spec[freqs > 5].sum() + 1e-12
    worst = None
    for hz in hz_candidates:
        share = 0.0
        ks = [k for k in range(1, 8) if k * hz < 0.45 * rate]
        for k in ks:
            share += float(spec[(freqs > k * hz - 1.0) & (freqs < k * hz + 1.0)].sum() / total_p)
        e[f"mains_{hz}hz_power_share"] = round(share, 3)
        if worst is None or share > worst[1]:
            worst = (hz, share, ks)
    if worst and worst[1] > 0.3:
        rep.add(WARN, "mains hum", f"{worst[1] * 100:.0f}% of EMG power is at {worst[0]} Hz and its harmonics",
                "check grounding/reference electrode, cable routing and keep clear of mains leads")
    else:
        rep.add(PASS, "mains hum", "no significant power at the mains frequency or its harmonics")


def find_baud(port: str, bauds: list[int], probe_s: float = 3.0, probe_max_s: float = 10.0,
              adapter_factory: Callable[[str, int], SerialAdapter] | None = None,
              progress: Callable[[str], None] | None = None, dtr: bool | None = None,
              rts: bool | None = None) -> list[dict]:
    """Try each baud rate and score it by the share of valid contract lines. A board that resets on
    open gets up to ``probe_max_s`` to start talking: the window stays open while no byte has arrived."""
    say = progress or (lambda _m: None)
    factory = adapter_factory or (lambda p, b: SerialAdapter(p, b, dtr=dtr, rts=rts))
    results = []
    for baud in bauds:
        ad = factory(port, baud)
        try:
            ad.open()
        except AdapterDisconnected as exc:
            results.append({"baud": baud, "lines": 0, "valid": 0, "error": str(exc)})
            say(f"  {baud:>8} baud: could not open ({exc})")
            continue
        lines = valid = 0
        t0 = time.monotonic()
        try:
            while True:
                elapsed = time.monotonic() - t0
                if elapsed >= (probe_s if lines else probe_max_s):
                    break
                line = ad.read_line(0.2)
                if line is None:
                    continue
                lines += 1
                rec, issues = contract.parse_line(line)
                if rec is not None and not issues:
                    valid += 1
        except AdapterDisconnected as exc:
            results.append({"baud": baud, "lines": lines, "valid": valid, "error": str(exc)})
            say(f"  {baud:>8} baud: lost the port ({exc})")
            continue
        finally:
            ad.close()
        results.append({"baud": baud, "lines": lines, "valid": valid, "share": round(valid / lines, 3) if lines else 0.0})
        say(f"  {baud:>8} baud: {valid}/{lines} valid lines")
    return results


# ---------------------------------------------------------------------------------- fake device

class FakeDevice:
    """A pseudo-terminal that behaves like the wearable's firmware, in real time.

    ``path`` is a port the backend (or serial-check) can open. Frames come from the simulator
    (SIMULATED, labelled so). Like most USB-serial boards it starts a fresh boot, hello first, each time
    a host opens the port. Faults let you rehearse what real hardware will do wrong. ``hang_after_s`` and
    ``unplug_after_s`` count wall time from the FIRST connection of a host."""

    def __init__(self, scenario: str = "happy_path", seed: int = 1, duration_s: float = 30.0, speed: float = 1.0,
                 baud_limit: int | None = None, mode: str = "frames", hang_after_s: float | None = None,
                 unplug_after_s: float | None = None, loop: bool = True, drop_hello: bool = False,
                 rate_error: float = 0.0):
        import pty
        import tty
        from vibeguard_signals.simulator import SCENARIOS
        if not hasattr(os, "openpty"):
            raise OSError("pseudo-terminals are not available on this platform")
        if mode not in ("frames", "garbage", "silent"):
            raise ValueError("mode must be frames, garbage or silent")
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; choose from {sorted(SCENARIOS)}")
        for name, v in (("speed", speed), ("duration", duration_s)):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
                raise ValueError(f"{name} must be a number above 0")
        if baud_limit is not None and (not isinstance(baud_limit, int) or baud_limit <= 0):
            raise ValueError("baud_limit must be a positive integer")
        for name, v in (("hang_after", hang_after_s), ("unplug_after", unplug_after_s)):
            if v is not None and (not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0):
                raise ValueError(f"{name} must be a number of seconds, 0 or more")
        if not isinstance(rate_error, (int, float)) or not math.isfinite(rate_error) or abs(rate_error) > 0.10:
            raise ValueError("rate_error must be a number with magnitude at most 0.10 (the contract rejects a "
                             "timestamp rate more than 10% off the declared rate)")
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)  # a host that is not reading must not hang us (or stop())
        self.path = os.ttyname(self.slave)
        self._slave_fd_open = True
        self.scenario, self.seed, self.duration_s, self.speed = scenario, seed, duration_s, speed
        self.baud_limit, self.mode, self.loop, self.drop_hello = baud_limit, mode, loop, drop_hello
        self.hang_after_s, self.unplug_after_s, self.rate_error = hang_after_s, unplug_after_s, rate_error
        self.bytes_written = 0
        self.frames_written = 0
        self.boots = 0
        self.ended_because: str | None = None
        self._t_first_conn: float | None = None
        self._stop = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="fake-device", daemon=True)

    def start(self) -> "FakeDevice":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(3)
        self._close_master()

    def _close_master(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.master)
        except OSError:
            pass
        self.master = -1
        if self._slave_fd_open:
            try:
                os.close(self.slave)
            except OSError:
                pass
            self._slave_fd_open = False

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _write(self, data: bytes) -> bool:
        """Write all of ``data``; False if the host went away. Never blocks beyond a stop request."""
        import select
        view = memoryview(data)
        while view and not self._stop.is_set():
            try:
                n = os.write(self.master, view)
            except BlockingIOError:
                select.select([], [self.master], [], 0.1)  # the host is slow: wait, but stay stoppable
                continue
            except OSError:
                return False
            view = view[n:]
            self.bytes_written += n
            if self.baud_limit:
                time.sleep(n / (self.baud_limit / 10))
        return not view

    def _run_frames(self, run: int):
        """One boot of the simulated firmware: (seconds since boot, frame) in order."""
        from vibeguard_signals.simulator import SimConfig, Simulation
        sim = Simulation(SimConfig(scenario=self.scenario, seed=self.seed, duration_s=self.duration_s),
                         run_id=f"fake{run}")
        for r in sim.records():
            if r.lost:
                continue
            rec = r.record
            if self.drop_hello and rec["type"] == "hello":
                continue
            if self.rate_error and rec["type"] in ("emg_batch", "imu_batch"):
                scale = 1.0 + self.rate_error  # the firmware's clock is off: every device time stretches
                t = rec["timing"]
                if t["mode"] == "uniform":
                    t["t0_us"] = int(t["t0_us"] * scale)
                    t["interval_us"] = t["interval_us"] * scale
                else:
                    t["t_us"] = [int(v * scale) for v in t["t_us"]]
            yield r.sim_time_us / 1e6, contract.serialize_record(rec).encode()

    def _run(self) -> None:
        """Like a board that resets when the serial port is opened (most USB-serial boards do):
        nothing is sent until a host opens the port, then a fresh boot starts with its hello."""
        if self._slave_fd_open:  # only the host may hold the slave, so we can tell when it connects
            try:
                os.close(self.slave)
            except OSError:
                pass
            self._slave_fd_open = False
        run = 0
        try:
            while not self._stop.is_set():
                self.boots = run + 1
                if self.mode == "silent":
                    if not self._wait_for_host(b"\n"):
                        return
                    reason = self._hold(self._remaining_unplug())
                    if reason == "timeout":
                        self.ended_because = "unplugged (--unplug-after)"
                        return
                    run += 1
                    continue
                if self.mode == "garbage":
                    import random
                    rng = random.Random(self.seed + run)
                    if not self._wait_for_host(bytes(rng.randrange(256) for _ in range(64))):
                        return
                    while not self._stop.is_set():
                        if self._unplug_due():
                            self.ended_because = "unplugged (--unplug-after)"
                            return
                        if not self._write(bytes(rng.randrange(256) for _ in range(64))):
                            break
                        time.sleep(0.02)
                    run += 1
                    continue
                gen = self._run_frames(run)
                first = next(gen, None)
                if first is None:
                    return
                if not self._wait_for_host(first[1]):
                    return
                t0 = time.monotonic() - first[0] / self.speed  # a host connected: this boot starts now
                self.frames_written += 1
                alive = True
                for due_s, frame in gen:
                    if self._stop.is_set():
                        return
                    if self._unplug_due():
                        self.ended_because = "unplugged (--unplug-after)"
                        return
                    if self._hang_due():
                        reason = self._hold(self._remaining_unplug())  # link up, no data
                        if reason == "timeout":
                            self.ended_because = "unplugged (--unplug-after)"
                            return
                        alive = False  # the host left: the next connection is a fresh boot (the hang does not repeat)
                        self.hang_after_s = None
                        break
                    wait = t0 + due_s / self.speed - time.monotonic()
                    if wait > 0:
                        self._stop.wait(wait)
                    if not self._write(frame):
                        alive = False  # the host closed the port
                        break
                    self.frames_written += 1
                if alive and not self.loop:
                    self.ended_because = "finished (--once)"
                    return
                run += 1  # the host left or the run ended: the next connection is a new boot
        except Exception as exc:  # noqa: BLE001 - say why the device died instead of dying silently
            self.ended_because = f"error: {type(exc).__name__}: {exc}"
        finally:
            if self.ended_because and "unplugged" in self.ended_because:
                self._close_master()  # the cable is pulled: the host's reads fail

    def _since_first_conn(self) -> float:
        return 0.0 if self._t_first_conn is None else time.monotonic() - self._t_first_conn

    def _unplug_due(self) -> bool:
        return self.unplug_after_s is not None and self._t_first_conn is not None and \
            self._since_first_conn() >= self.unplug_after_s

    def _hang_due(self) -> bool:
        return self.hang_after_s is not None and self._t_first_conn is not None and \
            self._since_first_conn() >= self.hang_after_s

    def _remaining_unplug(self) -> float | None:
        return None if self.unplug_after_s is None else max(self.unplug_after_s - self._since_first_conn(), 0.0)

    def _wait_for_host(self, first: bytes) -> bool:
        """Block until a host has opened the port; then start talking, like a board coming out of reset.
        Returns False if stopped."""
        while not self._stop.is_set():
            try:
                os.write(self.master, b"")  # a zero-byte write only succeeds while a host has the port open
            except (OSError, BlockingIOError):
                self._stop.wait(0.05)
                continue
            if self._t_first_conn is None:
                self._t_first_conn = time.monotonic()  # fault timers count from the first connection
            # A real board needs time to come out of reset (and a program's own open() flushes pending input),
            # so it never speaks the instant the port opens.
            self._stop.wait(0.1)
            return self._write(first)
        return False

    def _hold(self, seconds: float | None) -> str:
        """Link up, no data. Returns why it ended: 'timeout', 'host_left' or 'stop'."""
        end = None if seconds is None else time.monotonic() + max(seconds, 0)
        while not self._stop.wait(0.1):
            if end is not None and time.monotonic() >= end:
                return "timeout"
            try:  # leave when the host goes away
                os.write(self.master, b"")
            except OSError:
                return "host_left"
        return "stop"
