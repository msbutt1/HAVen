"""Hardware bring-up tools: list ports, check a real serial link, rehearse without hardware.

    vg-backend ports                                   # what is plugged in
    vg-backend serial-check --port '/dev/cu.usbmodem*' --baud 921600 --seconds 30 --save rec.ndjson
    vg-backend serial-check --port ... --try-bauds 115200,460800,921600   # find the baud rate
    vg-backend fake-device --scenario demo             # a pseudo-terminal that behaves like the firmware

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
from .preflight import FAIL, PASS, WARN, Check, Report

#: vendor ids of common microcontroller boards / USB-serial bridges (a hint, not a guarantee)
KNOWN_MCU_VENDORS = {
    0x2341: "Arduino", 0x2A03: "Arduino", 0x1A86: "WCH CH340/CH9102", 0x10C4: "Silicon Labs CP210x",
    0x0403: "FTDI", 0x303A: "Espressif", 0x239A: "Adafruit", 0x2E8A: "Raspberry Pi", 0x16C0: "Teensy/PJRC",
    0x0483: "STMicroelectronics", 0x1366: "SEGGER J-Link", 0x04D8: "Microchip", 0x1915: "Nordic",
}
STANDARD_BAUDS = (115200, 230400, 460800, 921600, 1000000, 2000000)


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
    notes: str | None = None
    raw: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return {k: v for k, v in self.raw.items()}


def load_profile(path: str | Path) -> HardwareProfile:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProfileError(f"cannot read {p}: {exc}") from exc
    except ValueError as exc:
        raise ProfileError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{p} must be a JSON object")
    known = {"name", "port", "baud", "emg", "imu", "notes", "_comment"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ProfileError(f"unknown field(s) {unknown}: a typo here would silently change what the backend assumes")
    emg, imu = data.get("emg", {}), data.get("imu", {})
    emg, imu = ({} if emg is None else emg), ({} if imu is None else imu)
    if not isinstance(emg, dict) or not isinstance(imu, dict):
        raise ProfileError("'emg' and 'imu' must be objects")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("'name' is required (it is stored with every recording)")
    baud = data.get("baud")
    if baud is not None and (isinstance(baud, bool) or not isinstance(baud, int) or not 1200 <= baud <= 4_000_000):
        raise ProfileError("'baud' must be an integer between 1200 and 4000000")
    output = emg.get("output", "raw")
    if output == "envelope":
        raise ProfileError("the EMG module outputs an ENVELOPE, but the processing pipeline assumes RAW EMG "
                           "(it band-passes and takes its own RMS). Stop and adapt the pipeline with Salaar "
                           "before connecting it; see signals/HANDOFF.md section 7")
    if output != "raw":
        raise ProfileError("emg.output must be 'raw' (or 'envelope', which is refused)")
    mains = emg.get("mains_hz")
    if mains is not None and mains not in (50, 60):
        raise ProfileError("emg.mains_hz must be 50 or 60")
    cpg = imu.get("counts_per_g")
    if cpg is not None:
        if isinstance(cpg, bool) or not isinstance(cpg, (int, float)) or not math.isfinite(cpg) or cpg <= 0:
            raise ProfileError("imu.counts_per_g must be a positive number")
        src = imu.get("conversion_source")
        if not isinstance(src, str) or len(src.strip()) < 8:
            raise ProfileError("imu.counts_per_g needs imu.conversion_source: where the sensitivity comes from "
                               "(datasheet page and the configured range), so m/s^2 values are never uncited")
    return HardwareProfile(
        name=name.strip(), port=data.get("port"), baud=baud, emg_output=output,
        emg_channels=emg.get("channels"), mains_hz=mains, imu_model=imu.get("model"),
        imu_counts_per_g=cpg, imu_range_g=imu.get("range_g"), imu_conversion_source=imu.get("conversion_source"),
        notes=data.get("notes"), raw=data)


# ---------------------------------------------------------------------------------- serial check

@dataclass
class _Stream:
    batches: int = 0
    samples: int = 0
    nominal_hz: float | None = None
    first_index: int | None = None
    last_index_end: int | None = None
    t_first_us: float | None = None
    t_last_end_us: float | None = None
    arrivals: list[float] = field(default_factory=list)
    gaps_us: list[float] = field(default_factory=list)
    values: list[list[float]] = field(default_factory=list)  # per channel, capped
    channels: list[str] = field(default_factory=list)
    raw_limits: dict | None = None
    clipped_samples: int = 0


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


def serial_check(port: str, baud: int = 921600, seconds: float = 20.0, save: str | Path | None = None,
                 mains_hz: int | None = None, adapter_factory: Callable[[str, int], SerialAdapter] = SerialAdapter,
                 progress: Callable[[str], None] | None = None) -> SerialCheckResult:
    """Read a real serial link for ``seconds`` and report what the backend would see."""
    rep = Report()
    say = progress or (lambda _m: None)
    adapter = adapter_factory(port, baud)
    try:
        adapter.open()
    except AdapterDisconnected as exc:
        busy = "busy" in str(exc).lower() or "resource" in str(exc).lower() or "permission" in str(exc).lower()
        rep.add(FAIL, "port opens", str(exc),
                "close anything else using it (Arduino IDE serial monitor, PlatformIO monitor, another vg-backend), "
                "check the cable and run: uv run vg-backend ports" if busy else
                "check the cable (some are power-only) and the name: uv run vg-backend ports")
        return SerialCheckResult(port, baud, seconds, rep, {})
    rep.add(PASS, "port opens", f"{adapter.port} at {baud} baud")

    out = Path(save).open("wb") if save else None
    proc = Processor(ProcessingConfig(clock="host"))
    streams: dict[str, _Stream] = {"emg": _Stream(), "imu": _Stream()}
    invalid, invalid_examples = Counter(), {}
    flags: Counter = Counter()
    status_states: Counter = Counter()
    sensors_seen: dict[str, set] = {"emg": set(), "imu": set()}
    hello: dict | None = None
    seq_last: dict[str, int] = {}
    seq_missing = seq_dup = reboots = 0
    dropped_before = 0
    bytes_total = lines = valid = longest = 0
    first_valid_at = first_byte_at = last_valid_at = None
    non_frame = 0
    events: Counter = Counter()
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
                code = issues[0].code if issues else "invalid"
                invalid[code] += 1
                invalid_examples.setdefault(code, line[:70])
                if not line.lstrip().startswith(b"{"):
                    non_frame += 1
                continue
            valid += 1
            last_valid_at = now - t0
            if first_valid_at is None:
                first_valid_at = now - t0
            key = rec["boot_id"]
            if key in seq_last:
                if rec["seq"] > seq_last[key] + 1:
                    seq_missing += rec["seq"] - seq_last[key] - 1
                elif rec["seq"] <= seq_last[key]:
                    seq_dup += 1
                    continue
            elif seq_last:
                reboots += 1
            seq_last[key] = rec["seq"]
            for out_rec in proc.feed(rec, rx_time_s=now):
                if out_rec["type"] == "stream_event":
                    events[out_rec["event"]] += 1
            kind = rec["type"]
            if kind == "hello":
                hello = rec
            elif kind == "status":
                status_states[rec["state"]] += 1
                for sname, st in rec["sensors"].items():
                    sensors_seen.setdefault(sname, set()).add(st)
            elif kind in ("emg_batch", "imu_batch"):
                st = streams["emg" if kind == "emg_batch" else "imu"]
                st.batches += 1
                st.samples += len(rec["samples"])
                st.nominal_hz = rec["sample_rate_hz"]
                st.channels = rec["channels"]
                first, last, _ = _timing_span(rec)
                idx = rec["first_sample_index"]
                if st.first_index is None:
                    st.first_index, st.t_first_us = idx, first
                else:
                    st.gaps_us.append(first - (st.t_last_end_us or first))
                st.last_index_end = idx + len(rec["samples"])
                st.t_last_end_us = last
                st.arrivals.append(now)
                for f in rec["quality"]["flags"]:
                    flags[f"{kind[:3]}:{f}"] += 1
                dropped_before += rec["quality"].get("dropped_samples_before", 0)
                if hello:
                    st.raw_limits = (hello["streams"].get(kind[:3]) or {}).get("raw_limits")
                if len(st.values) < len(rec["channels"]):
                    st.values = [[] for _ in rec["channels"]]
                for row in rec["samples"]:
                    for ci, v in enumerate(row):
                        if v is not None and len(st.values[ci]) < 20000:
                            st.values[ci].append(float(v))
                            lim = st.raw_limits
                            if lim and lim.get("min") is not None and (v <= lim["min"] or v >= lim["max"]):
                                st.clipped_samples += 1
    finally:
        adapter.close()
        if out:
            out.close()
    elapsed = max(time.monotonic() - t0, 1e-9)

    # ---- verdicts ---------------------------------------------------------------------------
    stats: dict[str, Any] = {"seconds": round(elapsed, 2), "lines": lines, "valid": valid, "bytes": bytes_total,
                             "bytes_per_s": round(bytes_total / elapsed), "invalid": dict(invalid),
                             "seq_missing": seq_missing, "seq_duplicates": seq_dup, "reboots": reboots,
                             "dropped_samples": dropped_before, "events": dict(events), "flags": dict(flags)}
    if bytes_total == 0:
        rep.add(FAIL, "data arrives", f"no bytes in {elapsed:.0f} s",
                "the firmware is not sending: check it is running and the TX pin/USB CDC is enabled; "
                "if the board resets on connect, wait for it; a wrong port name also looks like this")
        return SerialCheckResult(port, baud, seconds, rep, stats)
    rep.add(PASS, "data arrives", f"{bytes_total} bytes, {lines} lines, first after {first_byte_at:.2f} s")
    if valid == 0:
        hint = ("this looks like a WRONG BAUD RATE: the bytes are not text lines. Try: --try-bauds "
                + ",".join(str(b) for b in STANDARD_BAUDS[:4]) if non_frame > lines * 0.5 else
                "the firmware sends something other than contract v1 NDJSON; see signals/contract/TELEMETRY_CONTRACT.md. "
                f"First rejected line: {next(iter(invalid_examples.values()), b'')!r}")
        rep.add(FAIL, "contract frames", f"0 of {lines} lines are valid ({dict(invalid.most_common(3))})", hint)
        return SerialCheckResult(port, baud, seconds, rep, stats)
    stalled_for = elapsed - last_valid_at if last_valid_at is not None else 0.0
    stats["stalled_for_s"] = round(stalled_for, 2)
    if stalled_for > max(2.0, 0.15 * elapsed):
        rep.add(FAIL, "data keeps flowing", f"no valid frame for the last {stalled_for:.1f} s of the {elapsed:.0f} s check "
                f"(last at {last_valid_at:.1f} s)", "the link is up but the firmware stopped sending: a hang, a watchdog "
                "wait, or the USB bridge stuck. The backend shows this as a silent device")
    frac = valid / lines
    if frac >= 0.995:
        rep.add(PASS, "contract frames", f"{valid}/{lines} valid, first valid after {first_valid_at:.2f} s")
    elif frac >= 0.95:
        rep.add(WARN, "contract frames", f"{lines - valid} of {lines} lines rejected: {dict(invalid.most_common(3))}",
                f"e.g. {next(iter(invalid_examples.values()), b'')!r}; check the firmware output for stray prints")
    else:
        rep.add(FAIL, "contract frames", f"only {valid}/{lines} valid: {dict(invalid.most_common(3))}",
                "a high error rate usually means a wrong or marginal baud rate, or debug text on the same port")
    if longest > contract.MAX_LINE_BYTES * 0.5:
        rep.add(WARN, "frame size", f"longest line {longest} bytes (limit {contract.MAX_LINE_BYTES})", "send smaller batches")

    if hello is None:
        rep.add(WARN, "hello received", "no hello record seen", "send a hello on boot and again when the host connects")
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

    for name, st in streams.items():
        if not st.batches:
            rep.add(WARN if name == "imu" else FAIL, f"{name} data", f"no {name} batches in {elapsed:.0f} s",
                    "start that sensor in the firmware" if name == "imu" else
                    "the EMG stream is required for calibration and the activation display")
            continue
        span_us = (st.t_last_end_us - st.t_first_us)
        total = st.last_index_end - st.first_index
        measured = (total - 1) / (span_us / 1e6) if span_us > 0 else float("nan")
        nominal = st.nominal_hz
        dev = abs(measured - nominal) / nominal if nominal and math.isfinite(measured) else float("nan")
        arr = np.diff(st.arrivals) if len(st.arrivals) > 2 else np.array([])
        jitter_ms = float(np.std(arr) * 1000) if arr.size else float("nan")
        worst_gap = max(st.gaps_us, key=abs, default=0.0)
        stats[name] = {"batches": st.batches, "samples": st.samples, "nominal_hz": nominal,
                       "measured_hz": round(measured, 2), "deviation_pct": round(dev * 100, 2),
                       "batch_arrival_jitter_ms": round(jitter_ms, 2), "worst_device_gap_us": round(worst_gap),
                       "clipped_samples": st.clipped_samples}
        if dev <= 0.01:
            rep.add(PASS, f"{name} sample rate", f"measured {measured:.1f} Hz vs nominal {nominal:g} Hz ({dev * 100:.2f}% off)")
        elif dev <= 0.05:
            rep.add(WARN, f"{name} sample rate", f"measured {measured:.1f} Hz vs nominal {nominal:g} Hz ({dev * 100:.1f}% off)",
                    "report the measured rate to Salaar; filters use the declared rate (the backend measures it from timestamps)")
        else:
            rep.add(FAIL, f"{name} sample rate", f"measured {measured:.1f} Hz vs nominal {nominal:g} Hz ({dev * 100:.0f}% off)",
                    "the timestamps or the configured rate are wrong; filters would be mis-tuned. Check micros() wrap handling "
                    "and the ADC/IMU output data rate setting")
        if st.clipped_samples:
            rep.add(WARN, f"{name} clipping", f"{st.clipped_samples} samples at the ADC limits",
                    "reduce the gain/range or check the input; clipped EMG corrupts the envelope")
        # raw-signal sanity for EMG
        if name == "emg" and st.values and len(st.values[0]) > 400:
            x = np.asarray(st.values[0])
            stats["emg"].update({"mean": round(float(x.mean()), 2), "std": round(float(x.std()), 2),
                                 "min": float(x.min()), "max": float(x.max())})
            xc = x - x.mean()
            ac1 = float(np.dot(xc[:-1], xc[1:]) / (np.dot(xc, xc) + 1e-12))
            stats["emg"]["lag1_autocorrelation"] = round(ac1, 3)
            if x.std() < 0.5:
                rep.add(WARN, "emg looks like a signal", f"std {x.std():.2f} counts: flat",
                        "check electrode contact, the reference electrode and that the module is powered")
            elif ac1 > 0.98 and x.min() >= 0:
                rep.add(FAIL, "emg is raw", f"lag-1 autocorrelation {ac1:.3f} and one-sided: this looks like a "
                        "smoothed ENVELOPE, not raw EMG", "if the module outputs an envelope, STOP: the pipeline assumes raw "
                        "EMG (signals/HANDOFF.md section 7). Ask Collins for the module's output type")
            else:
                rep.add(PASS, "emg is raw", f"mean {x.mean():.0f}, std {x.std():.1f}, lag-1 autocorrelation {ac1:.2f} "
                        "(raw EMG is noisy, not smooth)")
            # mains hum
            rate = measured if math.isfinite(measured) else nominal
            hz_candidates = [mains_hz] if mains_hz else [50, 60]
            if rate and rate > 2.5 * max(hz_candidates):
                spec = np.abs(np.fft.rfft(xc * np.hanning(xc.size))) ** 2
                freqs = np.fft.rfftfreq(xc.size, 1 / rate)
                total_p = spec[(freqs > 5)].sum() + 1e-12
                for hz in hz_candidates:
                    band = spec[(freqs > hz - 1.0) & (freqs < hz + 1.0)].sum()
                    share = float(band / total_p)
                    stats["emg"][f"mains_{hz}hz_power_share"] = round(share, 3)
                    if share > 0.3:
                        rep.add(WARN, "mains hum", f"{share * 100:.0f}% of EMG power is at {hz} Hz",
                                "check grounding/reference electrode, cable routing and keep clear of mains leads")
        # raw IMU summary
        if name == "imu" and st.values:
            means = {c: round(float(np.mean(v)), 1) for c, v in zip(st.channels, st.values) if v}
            rms = {c: round(float(np.std(v)), 1) for c, v in zip(st.channels, st.values) if v}
            stats["imu"].update({"mean_counts": means, "std_counts": rms})
    if seq_missing or dropped_before:
        rep.add(WARN, "no records lost", f"{seq_missing} record(s) missing (seq gaps), {dropped_before} sample(s) dropped on the device",
                "link or buffer problem: raise the baud rate, shrink batches, or use native USB")
    else:
        rep.add(PASS, "no records lost", f"0 seq gaps, 0 dropped samples in {elapsed:.0f} s")
    if seq_dup:
        rep.add(WARN, "duplicate records", f"{seq_dup} repeated sequence numbers", "the firmware re-sent or reordered records")
    if reboots:
        rep.add(WARN, "device rebooted", f"{reboots} new boot_id during the check", "brown-out or watchdog reset: check power")
    bad_flags = {k: v for k, v in flags.items() if k.split(":")[1] in ("overrun", "stale", "sensor_disconnected", "timing_irregular", "missing_values")}
    if bad_flags:
        rep.add(WARN, "device-reported problems", str(dict(bad_flags)), "the firmware itself saw these; fix them at the source")
    util = bytes_total / elapsed / (baud / 10)
    stats["link_utilisation"] = round(util, 3)
    if util > 0.8:
        rep.add(FAIL, "link bandwidth", f"{util * 100:.0f}% of the {baud} baud link used", "it will drop data: raise the baud rate, use native USB, or send binary")
    elif util > 0.5:
        rep.add(WARN, "link bandwidth", f"{util * 100:.0f}% of the {baud} baud link used", "little headroom for bursts")
    else:
        rep.add(PASS, "link bandwidth", f"{util * 100:.0f}% of the {baud} baud link used ({bytes_total / elapsed / 1000:.1f} kB/s)")
    if status_states and "error" in status_states:
        rep.add(WARN, "device state", f"device reported state 'error' {status_states['error']} time(s)", "see the firmware's status message")
    return SerialCheckResult(port, baud, seconds, rep, stats)


def find_baud(port: str, bauds: list[int], probe_s: float = 3.0,
              adapter_factory: Callable[[str, int], SerialAdapter] = SerialAdapter,
              progress: Callable[[str], None] | None = None) -> list[dict]:
    """Try each baud rate briefly and score it by the share of valid contract lines."""
    results = []
    for baud in bauds:
        ad = adapter_factory(port, baud)
        try:
            ad.open()
        except AdapterDisconnected as exc:
            results.append({"baud": baud, "lines": 0, "valid": 0, "error": str(exc)})
            continue
        lines = valid = 0
        end = time.monotonic() + probe_s
        try:
            while time.monotonic() < end:
                line = ad.read_line(0.2)
                if line is None:
                    continue
                lines += 1
                rec, issues = contract.parse_line(line)
                if rec is not None and not issues:
                    valid += 1
        except AdapterDisconnected as exc:
            results.append({"baud": baud, "lines": lines, "valid": valid, "error": str(exc)})
            continue
        finally:
            ad.close()
        results.append({"baud": baud, "lines": lines, "valid": valid, "share": round(valid / lines, 3) if lines else 0.0})
        if progress:
            progress(f"  {baud:>8} baud: {valid}/{lines} valid lines")
    return results


# ---------------------------------------------------------------------------------- fake device

class FakeDevice:
    """A pseudo-terminal that behaves like the wearable's firmware, in real time.

    ``path`` is a port the backend (or serial-check) can open. Frames come from the simulator
    (SIMULATED, labelled so). Faults let you rehearse what real hardware will do wrong."""

    def __init__(self, scenario: str = "demo", seed: int = 1, duration_s: float = 30.0, speed: float = 1.0,
                 baud_limit: int | None = None, mode: str = "frames", hang_after_s: float | None = None,
                 unplug_after_s: float | None = None, loop: bool = True, drop_hello: bool = False,
                 rate_error: float = 0.0):
        import pty
        import tty
        if not hasattr(os, "openpty"):
            raise OSError("pseudo-terminals are not available on this platform")
        if mode not in ("frames", "garbage", "silent"):
            raise ValueError("mode must be frames, garbage or silent")
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self._slave_fd_open = True
        self.scenario, self.seed, self.duration_s, self.speed = scenario, seed, duration_s, speed
        self.baud_limit, self.mode, self.loop, self.drop_hello = baud_limit, mode, loop, drop_hello
        self.hang_after_s, self.unplug_after_s, self.rate_error = hang_after_s, unplug_after_s, rate_error
        self.bytes_written = 0
        self.frames_written = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-device", daemon=True)

    def start(self) -> "FakeDevice":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(3)
        self._close_master()

    def _close_master(self) -> None:
        for fd in (self.master,):
            try:
                os.close(fd)
            except OSError:
                pass
        self.master = -1
        if self._slave_fd_open:
            try:
                os.close(self.slave)
            except OSError:
                pass
            self._slave_fd_open = False

    def _write(self, data: bytes) -> bool:
        view = memoryview(data)
        while view and not self._stop.is_set():
            try:
                n = os.write(self.master, view)
            except BlockingIOError:
                time.sleep(0.001)
                continue
            except OSError:
                return False  # the host closed the port: wait for it to come back? no: end
            view = view[n:]
            self.bytes_written += n
            if self.baud_limit:
                time.sleep(n / (self.baud_limit / 10))
        return True

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
                t = rec["timing"]
                if t["mode"] == "uniform":  # the firmware's clock is off: every interval stretches
                    scale = 1.0 + self.rate_error
                    t["t0_us"] = int(t["t0_us"] * scale)
                    t["interval_us"] = t["interval_us"] * scale
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
                if self.mode == "silent":
                    if not self._wait_for_host(b"\n"):
                        return
                    self._hold(None)
                    run += 1
                    continue
                if self.mode == "garbage":
                    import random
                    rng = random.Random(self.seed + run)
                    if not self._wait_for_host(bytes(rng.randrange(256) for _ in range(64))):
                        return
                    while not self._stop.is_set():
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
                t0 = time.monotonic() - first[0] / self.speed  # the host connected: this boot starts now
                t_conn = time.monotonic()
                self.frames_written += 1
                alive = True
                for due_s, frame in gen:
                    if self._stop.is_set():
                        return
                    since = time.monotonic() - t_conn
                    if self.unplug_after_s is not None and since >= self.unplug_after_s:
                        return
                    if self.hang_after_s is not None and since >= self.hang_after_s:
                        self._hold(self.unplug_after_s and self.unplug_after_s - since)  # link up, no data
                        return
                    wait = t0 + due_s / self.speed - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    if not self._write(frame):
                        alive = False  # the host closed the port
                        break
                    self.frames_written += 1
                if alive and not self.loop:
                    return
                run += 1  # the host left or the run ended: the next connection is a new boot
        finally:
            if self.unplug_after_s is not None:
                self._close_master()  # the cable is pulled: the host's reads fail

    def _wait_for_host(self, first: bytes) -> bool:
        """Block until a host has opened the port (a write succeeds); False if stopped."""
        while not self._stop.is_set():
            try:
                n = os.write(self.master, first)
                self.bytes_written += n
                return True
            except (OSError, BlockingIOError):
                time.sleep(0.05)
        return False

    def _hold(self, seconds: float | None) -> None:
        end = None if seconds is None else time.monotonic() + max(seconds, 0)
        while not self._stop.wait(0.1):
            if end is not None and time.monotonic() >= end:
                return
            try:  # leave when the host goes away
                os.write(self.master, b"")
            except OSError:
                return
