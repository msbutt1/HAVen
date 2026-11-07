"""VibeGuard device telemetry contract, version 1 -- PROPOSED.

This module is the single authoritative validator for the device-level
telemetry contract described in ``signals/contract/TELEMETRY_CONTRACT.md``.
It validates individual records (one JSON object per NDJSON line). Stream
continuity (sequence gaps, sample-index jumps, boot changes, staleness) is
defined in the specification and handled by consumers, not here.

This contract covers device -> host telemetry only. It is not the backend or
frontend API contract (owned by Abduljabbar) and not station behaviour (owned
by Fahad).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

SCHEMA = "vibeguard.device_telemetry"
SUPPORTED_VERSIONS = frozenset({1})
CONTRACT_STATUS = "proposed"

# Bounds (see specification section "Limits").
MAX_LINE_BYTES = 65_536  # including the trailing newline
MAX_SAMPLES_PER_BATCH = 256
MAX_CHANNELS = 8
MAX_TEXT_LEN = 200
MAX_SAFE_INT = 2**53 - 1  # largest integer every JSON consumer handles exactly
MAX_COUNT_MAGNITUDE = 2**31 - 1
MAX_SAMPLE_RATE_HZ = 20_000.0
TIMING_TOLERANCE = 0.10  # allowed relative deviation of interval vs nominal rate
MAX_HARDWARE_KEYS = 16

RECORD_TYPES = ("hello", "status", "emg_batch", "imu_batch")
SOURCES = ("live", "simulated", "replay")
DEVICE_STATES = ("booting", "ready", "streaming", "error")
SENSOR_STATES = ("ok", "disconnected", "stale", "unknown")
STREAMS = ("emg", "imu")
QUALITY_FLAGS = (
    "clipped",
    "missing_values",
    "sensor_disconnected",
    "overrun",
    "stale",
    "timing_irregular",
    "warmup",
)

ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
EMG_CHANNEL_RE = re.compile(r"^emg[0-7]$")
IMU_CHANNEL_KIND = {
    "ax": "accel", "ay": "accel", "az": "accel",
    "gx": "gyro", "gy": "gyro", "gz": "gyro",
}
# unit -> requires conversion metadata (True for physical units)
UNITS_BY_KIND = {
    "emg": {"adc_counts": False, "mV": True},
    "accel": {"raw_counts": False, "m/s^2": True},
    "gyro": {"raw_counts": False, "deg/s": True},
}
COUNT_UNITS = frozenset({"adc_counts", "raw_counts"})

COMMON_FIELDS = frozenset(
    {"schema", "version", "type", "device_id", "boot_id", "seq", "provenance", "ext"}
)
TYPE_FIELDS = {
    "hello": frozenset({"t_us", "firmware", "streams", "hardware"}),
    "status": frozenset({"t_us", "state", "sensors", "counters", "message"}),
    "emg_batch": frozenset(
        {"sample_rate_hz", "first_sample_index", "timing", "channels", "units",
         "conversion", "samples", "quality"}
    ),
}
TYPE_FIELDS["imu_batch"] = TYPE_FIELDS["emg_batch"]
REQUIRED_TYPE_FIELDS = {
    "hello": ("t_us", "firmware", "streams"),
    "status": ("t_us", "state", "sensors"),
    "emg_batch": ("sample_rate_hz", "first_sample_index", "timing", "channels",
                  "units", "samples", "quality"),
}
REQUIRED_TYPE_FIELDS["imu_batch"] = REQUIRED_TYPE_FIELDS["emg_batch"]

# Stable issue codes. Consumers may switch on these.
ISSUE_CODES = (
    "line_too_long", "not_utf8", "empty_line", "malformed_json", "duplicate_key",
    "nonfinite_value", "not_object", "wrong_schema", "unsupported_version",
    "unknown_type", "missing_field", "unknown_field", "invalid_value",
    "limit_exceeded", "shape_mismatch", "timing_inconsistent",
    "missing_conversion", "unexpected_conversion", "quality_inconsistent",
    "provenance_inconsistent",
)


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path or '<record>'}: {self.message}"


class ContractViolation(ValueError):
    """Raised by :func:`require_valid` and :func:`serialize_record`."""

    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues))


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

class _ParseError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message


def _reject_constant(name: str) -> Any:
    raise _ParseError("nonfinite_value", f"non-finite JSON constant {name!r} is not allowed")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _ParseError("nonfinite_value", f"number {text!r} overflows to a non-finite value")
    return value


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _ParseError("duplicate_key", f"duplicate object key {key!r}")
        out[key] = value
    return out


def parse_document(text: str) -> tuple[Any | None, list[Issue]]:
    """Strictly parse one JSON document (NaN/Infinity/duplicate keys rejected)."""
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            object_pairs_hook=_no_duplicates,
        )
    except _ParseError as exc:
        return None, [Issue(exc.code, "", exc.message)]
    except json.JSONDecodeError as exc:
        return None, [Issue("malformed_json", "", str(exc))]
    return value, []


def parse_line(line: bytes | str) -> tuple[dict[str, Any] | None, list[Issue]]:
    """Parse and validate one NDJSON frame. Returns (record, issues).

    ``record`` is None when the frame could not be parsed as a JSON object.
    A record is valid if and only if ``issues`` is empty.
    """
    raw = line.encode("utf-8") if isinstance(line, str) else line
    if len(raw) > MAX_LINE_BYTES:
        return None, [Issue("line_too_long", "",
                            f"frame is {len(raw)} bytes; limit is {MAX_LINE_BYTES}")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [Issue("not_utf8", "", str(exc))]
    text = text.rstrip("\r\n")
    if not text.strip():
        return None, [Issue("empty_line", "", "blank frame")]
    if "\n" in text or "\r" in text:
        return None, [Issue("malformed_json", "", "frame contains an embedded newline")]
    value, issues = parse_document(text)
    if issues:
        return None, issues
    if not isinstance(value, dict):
        return None, [Issue("not_object", "", "top-level value must be a JSON object")]
    return value, validate_record(value)


def iter_ndjson(lines: Iterable[bytes | str]) -> Iterator[tuple[int, dict[str, Any] | None, list[Issue]]]:
    """Yield (line_number, record, issues) for each frame of an NDJSON stream."""
    for lineno, line in enumerate(lines, start=1):
        yield (lineno, *parse_line(line))


def serialize_record(record: dict[str, Any]) -> str:
    """Validate and serialize a record as one compact NDJSON frame (with newline).

    Raises :class:`ContractViolation` if the record is invalid or too large.
    """
    issues = validate_record(record)
    if issues:
        raise ContractViolation(issues)
    frame = json.dumps(record, separators=(",", ":"), allow_nan=False, ensure_ascii=False) + "\n"
    size = len(frame.encode("utf-8"))
    if size > MAX_LINE_BYTES:
        raise ContractViolation([Issue("line_too_long", "",
                                       f"frame is {size} bytes; limit is {MAX_LINE_BYTES}")])
    return frame


def require_valid(record: dict[str, Any]) -> dict[str, Any]:
    issues = validate_record(record)
    if issues:
        raise ContractViolation(issues)
    return record


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class _Checker:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def add(self, code: str, path: str, message: str) -> None:
        self.issues.append(Issue(code, path, message))

    # -- generic helpers ----------------------------------------------------

    def fields(self, obj: dict[str, Any], path: str, allowed: Iterable[str],
               required: Iterable[str]) -> None:
        allowed = set(allowed)
        for key in required:
            if key not in obj:
                self.add("missing_field", f"{path}{key}", "required field is missing")
        for key in obj:
            if key not in allowed:
                self.add("unknown_field", f"{path}{key}",
                         "field is not part of contract v1 (use 'ext' for experiments)")

    def obj(self, value: Any, path: str) -> bool:
        if not isinstance(value, dict):
            self.add("invalid_value", path, "must be an object")
            return False
        return True

    def ident(self, value: Any, path: str) -> None:
        if not isinstance(value, str) or not ID_RE.match(value):
            self.add("invalid_value", path, "must be 1-64 chars of [A-Za-z0-9._:-]")

    def text(self, value: Any, path: str, nullable: bool = False) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, str) or not value or len(value) > MAX_TEXT_LEN:
            self.add("invalid_value", path,
                     f"must be a non-empty string of at most {MAX_TEXT_LEN} chars"
                     + (" or null" if nullable else ""))

    def uint(self, value: Any, path: str, maximum: int = MAX_SAFE_INT) -> bool:
        if not _is_int(value) or not 0 <= value <= maximum:
            self.add("invalid_value", path, f"must be an integer in [0, {maximum}]")
            return False
        return True

    def enum(self, value: Any, path: str, choices: Iterable[str]) -> bool:
        choices = tuple(choices)
        if value not in choices or not isinstance(value, str):
            self.add("invalid_value", path, f"must be one of {list(choices)}")
            return False
        return True

    def rate(self, value: Any, path: str, nullable: bool = False) -> bool:
        if value is None and nullable:
            return False
        if not _is_num(value) or not 0 < value <= MAX_SAMPLE_RATE_HZ:
            self.add("invalid_value", path,
                     f"must be a number in (0, {MAX_SAMPLE_RATE_HZ:g}] Hz"
                     + (" or null (unknown)" if nullable else ""))
            return False
        return True

    # -- channels / units / conversion --------------------------------------

    def channels_units(self, stream: str, channels: Any, units: Any, conversion: Any,
                       path: str) -> list[str] | None:
        """Validate channel list, unit map and conversion. Returns per-channel units."""
        if not isinstance(channels, list) or not channels:
            self.add("invalid_value", f"{path}channels", "must be a non-empty list")
            return None
        if len(channels) > MAX_CHANNELS:
            self.add("limit_exceeded", f"{path}channels",
                     f"{len(channels)} channels; limit is {MAX_CHANNELS}")
            return None
        kinds: list[str] = []
        for i, ch in enumerate(channels):
            p = f"{path}channels[{i}]"
            if stream == "emg":
                if not isinstance(ch, str) or not EMG_CHANNEL_RE.match(ch):
                    self.add("invalid_value", p, "EMG channel must be emg0..emg7")
                    return None
                kinds.append("emg")
            else:
                if ch not in IMU_CHANNEL_KIND:
                    self.add("invalid_value", p, f"IMU channel must be one of {list(IMU_CHANNEL_KIND)}")
                    return None
                kinds.append(IMU_CHANNEL_KIND[ch])
        if len(set(channels)) != len(channels):
            self.add("invalid_value", f"{path}channels", "channel names must be unique")
            return None

        if not self.obj(units, f"{path}units"):
            return None
        if set(units) != set(channels):
            self.add("shape_mismatch", f"{path}units", "keys must exactly match 'channels'")
            return None
        unit_list: list[str] = []
        physical: list[str] = []
        for ch, kind in zip(channels, kinds):
            unit = units[ch]
            allowed = UNITS_BY_KIND[kind]
            if unit not in allowed:
                self.add("invalid_value", f"{path}units.{ch}",
                         f"{kind} unit must be one of {list(allowed)}")
                return None
            unit_list.append(unit)
            if allowed[unit]:
                physical.append(ch)

        if conversion is None:
            if physical:
                self.add("missing_conversion", f"{path}conversion",
                         f"physical units on {physical} require conversion metadata; "
                         "send raw counts until the conversion is known")
            return unit_list
        if not self.obj(conversion, f"{path}conversion"):
            return unit_list
        for ch in physical:
            if ch not in conversion:
                self.add("missing_conversion", f"{path}conversion.{ch}",
                         "physical-unit channel has no conversion entry")
        for ch, entry in conversion.items():
            p = f"{path}conversion.{ch}"
            if ch not in physical:
                self.add("unexpected_conversion", p,
                         "conversion given for a channel that is not in physical units")
                continue
            if not self.obj(entry, p):
                continue
            self.fields(entry, f"{p}.", {"scale", "offset", "source"}, ("scale", "source"))
            scale = entry.get("scale")
            if "scale" in entry and (not _is_num(scale) or scale == 0):
                self.add("invalid_value", f"{p}.scale", "must be a finite non-zero number")
            if "offset" in entry and not _is_num(entry["offset"]):
                self.add("invalid_value", f"{p}.offset", "must be a finite number")
            if "source" in entry:
                self.text(entry["source"], f"{p}.source")
        return unit_list

    # -- record types -------------------------------------------------------

    def provenance(self, prov: Any) -> None:
        if not self.obj(prov, "provenance"):
            return
        self.fields(prov, "provenance.", {"source", "recording_id", "original_source", "detail"},
                    ("source",))
        source = prov.get("source")
        if "source" in prov and not self.enum(source, "provenance.source", SOURCES):
            return
        if "recording_id" in prov:
            self.ident(prov["recording_id"], "provenance.recording_id")
        if "detail" in prov and not isinstance(prov["detail"], dict):
            self.add("invalid_value", "provenance.detail", "must be an object")
        if source == "replay":
            if "recording_id" not in prov:
                self.add("missing_field", "provenance.recording_id",
                         "replayed records must keep the original recording identity")
            if "original_source" not in prov:
                self.add("missing_field", "provenance.original_source",
                         "replayed records must state whether the recording was live or simulated")
            else:
                self.enum(prov["original_source"], "provenance.original_source",
                          ("live", "simulated"))
        elif "original_source" in prov:
            self.add("provenance_inconsistent", "provenance.original_source",
                     "only allowed when source is 'replay'")

    def hello(self, rec: dict[str, Any]) -> None:
        if "t_us" in rec:
            self.uint(rec["t_us"], "t_us")
        fw = rec.get("firmware")
        if "firmware" in rec and self.obj(fw, "firmware"):
            self.fields(fw, "firmware.", {"name", "version"}, ("name", "version"))
            for k in ("name", "version"):
                if k in fw:
                    self.text(fw[k], f"firmware.{k}")
        streams = rec.get("streams")
        if "streams" in rec and self.obj(streams, "streams"):
            if not streams:
                self.add("invalid_value", "streams", "must declare at least one stream")
            for name, decl in streams.items():
                p = f"streams.{name}"
                if name not in STREAMS:
                    self.add("unknown_field", p, f"stream must be one of {list(STREAMS)}")
                    continue
                if not self.obj(decl, p):
                    continue
                self.fields(decl, f"{p}.",
                            {"nominal_rate_hz", "channels", "units", "conversion",
                             "raw_limits", "sensor_model"},
                            ("nominal_rate_hz", "channels", "units", "raw_limits", "sensor_model"))
                if "nominal_rate_hz" in decl:
                    self.rate(decl["nominal_rate_hz"], f"{p}.nominal_rate_hz", nullable=True)
                if "channels" in decl and "units" in decl:
                    self.channels_units(name, decl["channels"], decl["units"],
                                        decl.get("conversion"), f"{p}.")
                if "sensor_model" in decl:
                    self.text(decl["sensor_model"], f"{p}.sensor_model", nullable=True)
                limits = decl.get("raw_limits")
                if limits is not None and self.obj(limits, f"{p}.raw_limits"):
                    self.fields(limits, f"{p}.raw_limits.", {"min", "max"}, ("min", "max"))
                    lo, hi = limits.get("min"), limits.get("max")
                    if not (_is_int(lo) and _is_int(hi) and lo < hi):
                        self.add("invalid_value", f"{p}.raw_limits",
                                 "min and max must be integers with min < max")
        hw = rec.get("hardware")
        if "hardware" in rec and self.obj(hw, "hardware"):
            if len(hw) > MAX_HARDWARE_KEYS:
                self.add("limit_exceeded", "hardware", f"at most {MAX_HARDWARE_KEYS} keys")
            for k, v in hw.items():
                self.ident(k, f"hardware.{k} (key)")
                self.text(v, f"hardware.{k}", nullable=True)

    def status(self, rec: dict[str, Any]) -> None:
        if "t_us" in rec:
            self.uint(rec["t_us"], "t_us")
        if "state" in rec:
            self.enum(rec["state"], "state", DEVICE_STATES)
        sensors = rec.get("sensors")
        if "sensors" in rec and self.obj(sensors, "sensors"):
            for k, v in sensors.items():
                if k not in STREAMS:
                    self.add("unknown_field", f"sensors.{k}", f"must be one of {list(STREAMS)}")
                else:
                    self.enum(v, f"sensors.{k}", SENSOR_STATES)
        counters = rec.get("counters")
        if "counters" in rec and self.obj(counters, "counters"):
            if len(counters) > MAX_HARDWARE_KEYS:
                self.add("limit_exceeded", "counters", f"at most {MAX_HARDWARE_KEYS} keys")
            for k, v in counters.items():
                self.ident(k, f"counters.{k} (key)")
                self.uint(v, f"counters.{k}")
        if "message" in rec:
            self.text(rec["message"], "message")

    def batch(self, rec: dict[str, Any], stream: str) -> None:
        if "sample_rate_hz" in rec:
            rate_ok = self.rate(rec["sample_rate_hz"], "sample_rate_hz")
        else:
            rate_ok = False
        if "first_sample_index" in rec:
            self.uint(rec["first_sample_index"], "first_sample_index")

        units = None
        if "channels" in rec and "units" in rec:
            units = self.channels_units(stream, rec["channels"], rec["units"],
                                        rec.get("conversion"), "")

        samples = rec.get("samples")
        n_rows = None
        missing = 0
        total = 0
        if "samples" in rec:
            if not isinstance(samples, list) or not samples:
                self.add("invalid_value", "samples", "must be a non-empty list of rows")
            elif len(samples) > MAX_SAMPLES_PER_BATCH:
                self.add("limit_exceeded", "samples",
                         f"{len(samples)} rows; limit is {MAX_SAMPLES_PER_BATCH}")
            else:
                n_rows = len(samples)
                if units is not None:
                    missing, total = self._samples(samples, units)

        if "timing" in rec and n_rows is not None:
            self._timing(rec["timing"], n_rows, rec.get("sample_rate_hz") if rate_ok else None)
        elif "timing" in rec:
            self.obj(rec["timing"], "timing")

        quality = rec.get("quality")
        if "quality" in rec and self.obj(quality, "quality"):
            self._quality(quality, missing, total, units is not None and n_rows is not None)

    def _samples(self, samples: list[Any], units: list[str]) -> tuple[int, int]:
        width = len(units)
        missing = 0
        total = 0
        for r, row in enumerate(samples):
            if not isinstance(row, list) or len(row) != width:
                self.add("shape_mismatch", f"samples[{r}]",
                         f"each row must be a list of {width} values (one per channel)")
                return missing, total
            for c, v in enumerate(row):
                total += 1
                if v is None:
                    missing += 1
                    continue
                p = f"samples[{r}][{c}]"
                if units[c] in COUNT_UNITS:
                    if not _is_int(v) or abs(v) > MAX_COUNT_MAGNITUDE:
                        self.add("invalid_value", p,
                                 f"{units[c]} must be an integer (or null if missing), got {json.dumps(v)}")
                        return missing, total
                elif not _is_num(v):
                    self.add("invalid_value", p, f"must be a finite number or null, got {json.dumps(v)}")
                    return missing, total
        return missing, total

    def _timing(self, timing: Any, n: int, rate: float | None) -> None:
        if not self.obj(timing, "timing"):
            return
        mode = timing.get("mode")
        if mode == "uniform":
            self.fields(timing, "timing.", {"mode", "t0_us", "interval_us"},
                        ("mode", "t0_us", "interval_us"))
            t0, dt = timing.get("t0_us"), timing.get("interval_us")
            ok = "t0_us" in timing and self.uint(t0, "timing.t0_us")
            if "interval_us" in timing and (not _is_num(dt) or dt <= 0):
                self.add("invalid_value", "timing.interval_us", "must be a finite number > 0")
                return
            if not ok or dt is None:
                return
            if t0 + (n - 1) * dt > MAX_SAFE_INT:
                self.add("timing_inconsistent", "timing", "batch end time exceeds 2^53-1 us")
            self._interval_vs_rate(dt, rate, "timing.interval_us")
        elif mode == "explicit":
            self.fields(timing, "timing.", {"mode", "t_us"}, ("mode", "t_us"))
            ts = timing.get("t_us")
            if "t_us" not in timing:
                return
            if not isinstance(ts, list) or not all(_is_int(t) and 0 <= t <= MAX_SAFE_INT for t in ts):
                self.add("invalid_value", "timing.t_us",
                         "must be a list of integers in [0, 2^53-1]")
                return
            if len(ts) != n:
                self.add("timing_inconsistent", "timing.t_us",
                         f"{len(ts)} timestamps for {n} sample rows")
                return
            for i in range(1, n):
                if ts[i] <= ts[i - 1]:
                    self.add("timing_inconsistent", f"timing.t_us[{i}]",
                             f"timestamps must strictly increase ({ts[i - 1]} -> {ts[i]})")
                    return
            if n >= 2:
                self._interval_vs_rate((ts[-1] - ts[0]) / (n - 1), rate, "timing.t_us")
        else:
            self.add("invalid_value", "timing.mode", "must be 'uniform' or 'explicit'")

    def _interval_vs_rate(self, dt: float, rate: float | None, path: str) -> None:
        if rate is None:
            return
        nominal = 1e6 / rate
        if abs(dt - nominal) > TIMING_TOLERANCE * nominal:
            self.add("timing_inconsistent", path,
                     f"interval {dt:g} us disagrees with sample_rate_hz {rate:g} "
                     f"(nominal {nominal:g} us, tolerance {TIMING_TOLERANCE:.0%})")

    def _quality(self, q: dict[str, Any], missing: int, total: int, checkable: bool) -> None:
        self.fields(q, "quality.", {"flags", "dropped_samples_before"}, ("flags",))
        flags = q.get("flags")
        if "flags" in q:
            if not isinstance(flags, list) or len(set(map(str, flags))) != len(flags):
                self.add("invalid_value", "quality.flags", "must be a list of unique flags")
                return
            for i, f in enumerate(flags):
                self.enum(f, f"quality.flags[{i}]", QUALITY_FLAGS)
        else:
            return
        dropped = q.get("dropped_samples_before", 0)
        if "dropped_samples_before" in q and not self.uint(dropped, "quality.dropped_samples_before"):
            dropped = 0
        if dropped and "overrun" not in flags:
            self.add("quality_inconsistent", "quality.flags",
                     "dropped_samples_before > 0 requires the 'overrun' flag")
        if not checkable:
            return
        if missing and "missing_values" not in flags:
            self.add("quality_inconsistent", "quality.flags",
                     f"{missing} null sample value(s) present without the 'missing_values' flag")
        if not missing and "missing_values" in flags:
            self.add("quality_inconsistent", "quality.flags",
                     "'missing_values' flagged but no sample value is null")
        if "sensor_disconnected" in flags and missing != total:
            self.add("quality_inconsistent", "samples",
                     "'sensor_disconnected' batches must carry null values, never "
                     "placeholder numbers such as 0")


def validate_record(record: Any) -> list[Issue]:
    """Validate one decoded record. Returns a list of issues; empty means valid."""
    c = _Checker()
    if not isinstance(record, dict):
        c.add("not_object", "", "record must be a JSON object")
        return c.issues

    if "schema" not in record:
        c.add("missing_field", "schema", "required field is missing")
        return c.issues
    if record["schema"] != SCHEMA:
        c.add("wrong_schema", "schema", f"expected {SCHEMA!r}")
        return c.issues
    if "version" not in record:
        c.add("missing_field", "version", "required field is missing")
        return c.issues
    version = record["version"]
    if not _is_int(version) or version not in SUPPORTED_VERSIONS:
        c.add("unsupported_version", "version",
              f"got {version!r}; this validator supports {sorted(SUPPORTED_VERSIONS)}")
        return c.issues
    rtype = record.get("type")
    if "type" not in record:
        c.add("missing_field", "type", "required field is missing")
        return c.issues
    if rtype not in RECORD_TYPES:
        c.add("unknown_type", "type", f"must be one of {list(RECORD_TYPES)}")
        return c.issues

    c.fields(record, "", COMMON_FIELDS | TYPE_FIELDS[rtype],
             ("device_id", "boot_id", "seq", "provenance") + REQUIRED_TYPE_FIELDS[rtype])
    if "device_id" in record:
        c.ident(record["device_id"], "device_id")
    if "boot_id" in record:
        c.ident(record["boot_id"], "boot_id")
    if "seq" in record:
        c.uint(record["seq"], "seq")
    if "provenance" in record:
        c.provenance(record["provenance"])
    if "ext" in record and not isinstance(record["ext"], dict):
        c.add("invalid_value", "ext", "must be an object")

    if rtype == "hello":
        c.hello(record)
    elif rtype == "status":
        c.status(record)
    else:
        c.batch(record, "emg" if rtype == "emg_batch" else "imu")
    return c.issues


def is_valid(record: Any) -> bool:
    return not validate_record(record)
