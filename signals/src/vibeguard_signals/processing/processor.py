"""Streaming host-side processor: contract records in, derived records out.

Transport-free: callers hand over decoded records (dicts) and get lists of
output dicts back. Serialization lives in ``serialize.py``; NDJSON reading in
the CLI. Algorithms live in ``features.py``; storage in ``window.py``.

One ``Processor`` handles one device. Records from another ``device_id`` are
dropped with a ``foreign_device`` event; create one processor per device.

See ``signals/PROCESSING.md`` for the output format and every rule below.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

from .. import contract
from . import features
from .config import ProcessingConfig, StreamConfig
from .window import SampleWindow, WindowSample

OUTPUT_SCHEMA = "vibeguard.features"
OUTPUT_VERSION = 1
TIMEBASE = "device_monotonic_us"
STREAMS = ("emg", "imu")


@dataclass
class ProcessingSummary:
    records_in: int = 0
    records_accepted: int = 0
    events: Counter = field(default_factory=Counter)
    feature_status: dict[str, Counter] = field(
        default_factory=lambda: {s: Counter() for s in STREAMS})
    buffer_capacity: dict[str, int] = field(default_factory=dict)
    buffer_max_occupancy: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "records_in": self.records_in,
            "records_accepted": self.records_accepted,
            "events": dict(sorted(self.events.items())),
            "feature_status": {s: dict(sorted(c.items())) for s, c in self.feature_status.items()},
            "buffer_capacity": dict(self.buffer_capacity),
            "buffer_max_occupancy": dict(self.buffer_max_occupancy),
        }


class _Stream:
    """Per-stream continuity, window, and availability state."""

    def __init__(self, name: str, cfg: StreamConfig):
        self.name = name
        self.cfg = cfg
        self.window: SampleWindow | None = None
        self.full_reset()

    def full_reset(self) -> None:
        """Forget everything (boot change)."""
        self.next_index: int | None = None
        self.last_t: float | None = None
        self.last_rx: float | None = None
        self.rate: float | None = None
        self.rate_source = "unknown"
        self.channels: list[str] | None = None
        self.units: dict[str, str] | None = None
        self.device_state = "unknown"
        self.stale = False
        self.rate_mismatch = False
        self.batch_flags: deque[tuple[float, tuple[str, ...]]] = deque()
        if self.window is not None:
            self.window.clear()
        self.reset("no_data")

    def reset(self, reason: str) -> None:
        """Invalidate the window; keep continuity tracking."""
        if self.window is not None:
            self.window.clear()
        self.batch_flags.clear()
        self.segment_start: float | None = None
        self.unavailable_reason = reason

    def configure_rate(self, rate: float, source: str) -> bool:
        """Set the expected rate; (re)build the bounded window. Returns True if changed."""
        if self.rate == rate and self.window is not None:
            return False
        self.rate, self.rate_source = rate, source
        cap = math.ceil(self.cfg.window_s * rate * self.cfg.buffer_rate_ceiling_factor) \
            + contract.MAX_SAMPLES_PER_BATCH
        self.window = SampleWindow(self.cfg.window_s * 1e6, cap)
        return True

    @property
    def dt_us(self) -> float:
        return 1e6 / self.rate  # type: ignore[operator]


class Processor:
    def __init__(self, config: ProcessingConfig | None = None):
        self.config = config or ProcessingConfig()
        self.config.validate()
        self.summary = ProcessingSummary()
        self.device_id: str | None = None
        self.boot_id: str | None = None
        self._retired: deque[str] = deque(maxlen=self.config.retired_boot_memory)
        self._streams = {s: _Stream(s, self.config.stream(s)) for s in STREAMS}
        self._start_boot_state()

    # ------------------------------------------------------------------ API

    def feed(self, record: Any, rx_time_s: float | None = None) -> list[dict]:
        """Process one decoded record. ``rx_time_s`` (host monotonic) is required
        in host-clock mode and ignored in device-clock mode."""
        self.summary.records_in += 1
        if self.config.clock == "host" and rx_time_s is None:
            raise ValueError("host clock mode requires rx_time_s for every record")
        out: list[dict] = []
        issues = contract.validate_record(record)
        if issues:
            return self._event(out, "invalid_record", None, None, None,
                               {"codes": sorted({i.code for i in issues}),
                                "first_issue": str(issues[0])})

        if self.device_id is None:
            self.device_id = record["device_id"]
        elif record["device_id"] != self.device_id:
            return self._event(out, "foreign_device", None, None, record["seq"],
                               {"device_id": record["device_id"]})

        boot = record["boot_id"]
        if boot != self.boot_id:
            if boot in self._retired:
                return self._event(out, "retired_boot_record", None, None, record["seq"],
                                   {"record_boot_id": boot})
            previous = self.boot_id
            if previous is not None:
                self._retired.append(previous)
            self.boot_id = boot
            self._start_boot_state()
            self._prov = _min_provenance(record["provenance"])
            self._event(out, "boot_change", None, None, record["seq"],
                        {"previous_boot_id": previous, "hello_received": record["type"] == "hello"})

        # Every rejection happens before any state is mutated: a duplicate or late
        # record must not change provenance, windows, or sequence state.
        seq = record["seq"]
        if seq in self._recent_set:
            return self._event(out, "duplicate_record", None, None, seq, {})
        if self._seq_last is not None and seq < self._seq_last:
            return self._event(out, "late_record", None, None, seq, {"last_seq": self._seq_last})

        if self._seq_last is not None and seq > self._seq_last + 1:
            self._event(out, "seq_gap", None, None, seq,
                        {"missing_records": seq - self._seq_last - 1})
        prov = _min_provenance(record["provenance"])
        if prov != self._prov:
            self._event(out, "provenance_changed", None, None, seq,
                        {"previous": self._prov, "new": prov})
            self._prov = prov
            for st in self._streams.values():
                self._invalidate(out, st, "provenance_changed", seq)
        self._remember_seq(seq)
        self.summary.records_accepted += 1

        rtype = record["type"]
        if rtype == "hello":
            self._declared = record["streams"]
        elif rtype == "status":
            self._on_status(out, record)
        else:
            self._on_batch(out, "emg" if rtype == "emg_batch" else "imu", record, rx_time_s)
        self._check_stale(out, rx_time_s)
        return out

    def feed_invalid_frame(self, issues: list[contract.Issue], where: str | None = None) -> list[dict]:
        """Report a frame that could not even be decoded (transport-layer failure)."""
        self.summary.records_in += 1
        return self._event([], "invalid_record", None, None, None,
                           {"codes": sorted({i.code for i in issues}),
                            "first_issue": str(issues[0]), "where": where})

    def poll(self, now_s: float) -> list[dict]:
        """Host-clock mode: call periodically so silence becomes visible as staleness."""
        if self.config.clock != "host":
            raise ValueError("poll() is only meaningful in host clock mode")
        out: list[dict] = []
        self._check_stale(out, now_s)
        return out

    def finish(self) -> list[dict]:
        """End of input: no stream may keep looking current."""
        out: list[dict] = []
        for st in self._streams.values():
            if st.last_t is not None:
                self._invalidate(out, st, "end_of_input", self._seq_last)
        self._event(out, "end_of_input", None, None, self._seq_last, {})
        for name, st in self._streams.items():
            if st.window is not None:
                self.summary.buffer_capacity[name] = st.window.capacity
                self.summary.buffer_max_occupancy[name] = max(
                    self.summary.buffer_max_occupancy.get(name, 0), st.window.max_occupancy)
        return out

    def buffer_sizes(self) -> dict[str, tuple[int, int]]:
        """(current length, capacity) per stream."""
        return {n: (len(s.window), s.window.capacity) if s.window else (0, 0)
                for n, s in self._streams.items()}

    # ------------------------------------------------------------ internals

    def _start_boot_state(self) -> None:
        self._seq_last: int | None = None
        self._recent: deque[int] = deque(maxlen=self.config.recent_seq_memory)
        self._recent_set: set[int] = set()
        self._declared: dict = {}
        self._prov: dict | None = None
        for name, st in self._streams.items():
            if st.window is not None:
                self.summary.buffer_capacity[name] = st.window.capacity
                self.summary.buffer_max_occupancy[name] = max(
                    self.summary.buffer_max_occupancy.get(name, 0), st.window.max_occupancy)
            st.full_reset()

    def _remember_seq(self, seq: int) -> None:
        if len(self._recent) == self._recent.maxlen:
            self._recent_set.discard(self._recent[0])
        self._recent.append(seq)
        self._recent_set.add(seq)
        self._seq_last = seq if self._seq_last is None else max(self._seq_last, seq)

    def _base(self, rtype: str) -> dict:
        return {"schema": OUTPUT_SCHEMA, "version": OUTPUT_VERSION, "type": rtype,
                "device_id": self.device_id, "boot_id": self.boot_id,
                "provenance": self._prov, "timebase": TIMEBASE}

    def _event(self, out: list[dict], kind: str, stream: str | None, t_us: float | None,
               seq: int | None, detail: dict) -> list[dict]:
        rec = self._base("stream_event")
        rec.update({"event": kind, "stream": stream,
                    "t_us": None if t_us is None else round(t_us),
                    "source_seq": seq, "detail": detail})
        self.summary.events[kind] += 1
        out.append(rec)
        return out

    def _invalidate(self, out: list[dict], st: _Stream, reason: str, seq: int | None,
                    t_us: float | None = None) -> None:
        st.reset(reason)
        out.append(self._features(st, st.last_t if t_us is None else t_us, seq))

    def _on_status(self, out: list[dict], rec: dict) -> None:
        for name, state in rec["sensors"].items():
            st = self._streams[name]
            previous, st.device_state = st.device_state, state
            if state == previous or (previous == "unknown" and state == "ok"):
                continue
            self._event(out, "device_sensor_state", name, rec["t_us"], rec["seq"], {"state": state})
            if state in ("disconnected", "stale"):
                self._invalidate(out, st, f"device_reports_{state}", rec["seq"], rec["t_us"])

    def _rate_for(self, st: _Stream, rec: dict) -> tuple[float, str]:
        if st.cfg.expected_rate_hz is not None:
            return st.cfg.expected_rate_hz, "config"
        declared = self._declared.get(st.name, {}).get("nominal_rate_hz")
        if declared is not None:
            return declared, "device_hello"
        return rec["sample_rate_hz"], "batch_declared"

    def _limits_for(self, st: _Stream) -> tuple[tuple[int, int] | None, str]:
        if st.cfg.raw_limits is not None:
            return st.cfg.raw_limits, "config"
        declared = self._declared.get(st.name, {}).get("raw_limits")
        if st.cfg.use_device_raw_limits and declared:
            return (declared["min"], declared["max"]), "device_hello"
        return None, "unknown"

    def _on_batch(self, out: list[dict], name: str, rec: dict, rx: float | None) -> None:
        st = self._streams[name]
        seq = rec["seq"]
        rate, source = self._rate_for(st, rec)
        if st.configure_rate(rate, source) and st.next_index is not None:
            self._event(out, "rate_changed", name, None, seq, {"rate_hz": rate, "source": source})
            st.reset("rate_changed")

        timing = rec["timing"]
        n = len(rec["samples"])
        if timing["mode"] == "uniform":
            times = [timing["t0_us"] + i * timing["interval_us"] for i in range(n)]
            timing_hz = 1e6 / timing["interval_us"]
        else:
            times = [float(t) for t in timing["t_us"]]
            timing_hz = (n - 1) * 1e6 / (times[-1] - times[0]) if n >= 2 else None

        # Rate mismatch is an availability condition, re-evaluated on every batch:
        # the expected rate must agree with both the batch's declared rate and the
        # rate implied by its timestamps. Mismatched samples never enter a window.
        tol = contract.TIMING_TOLERANCE * rate
        mismatch = abs(rec["sample_rate_hz"] - rate) > tol or \
            (timing_hz is not None and abs(timing_hz - rate) > tol)
        if mismatch and not st.rate_mismatch:
            st.rate_mismatch = True
            self._event(out, "rate_mismatch", name, times[0], seq,
                        {"expected_hz": rate, "expected_source": source,
                         "batch_declared_hz": rec["sample_rate_hz"],
                         "batch_timing_hz": None if timing_hz is None else round(timing_hz, 4)})
        elif not mismatch and st.rate_mismatch:
            st.rate_mismatch = False
            self._event(out, "rate_mismatch_cleared", name, times[0], seq,
                        {"expected_hz": rate, "batch_declared_hz": rec["sample_rate_hz"]})
        first = rec["first_sample_index"]
        flags = tuple(rec["quality"]["flags"])

        if st.channels is not None and (rec["channels"] != st.channels or rec["units"] != st.units):
            self._event(out, "channels_changed", name, times[0], seq,
                        {"channels": rec["channels"], "units": rec["units"]})
            st.reset("channels_changed")
        st.channels, st.units = list(rec["channels"]), dict(rec["units"])

        if st.next_index is not None:
            if first < st.next_index:
                self._event(out, "sample_overlap", name, times[0], seq,
                            {"first_sample_index": first, "expected_index": st.next_index})
                return
            if first > st.next_index:
                missing = first - st.next_index
                dev = rec["quality"].get("dropped_samples_before", 0)
                cause = ("device_overrun" if dev == missing else
                         "transport_or_unreported" if dev == 0 else "device_overrun_and_transport")
                self._event(out, "sample_gap", name, times[0], seq,
                            {"missing_samples": missing, "device_reported_dropped": dev,
                             "cause": cause})
                st.reset("gap")
            elif not st.rate_mismatch:  # timing against the expected rate is meaningless while mismatched
                step = times[0] - st.last_t  # type: ignore[operator]
                if abs(step - st.dt_us) > self.config.continuity_tolerance * st.dt_us:
                    self._event(out, "timing_discontinuity", name, times[0], seq,
                                {"step_us": round(step, 3), "expected_us": round(st.dt_us, 3)})
                    st.reset("timing_discontinuity")
        st.next_index = first + n
        st.last_t = times[-1]
        st.last_rx = rx
        if st.stale:
            st.stale = False
            self._event(out, "stream_recovered", name, times[0], seq, {})

        if st.rate_mismatch:
            st.reset("rate_mismatch")
            out.append(self._features(st, times[-1], seq))
            return
        if st.device_state in ("disconnected", "stale"):
            # Device-reported sensor state is sticky until the device reports "ok".
            st.reset(f"device_reports_{st.device_state}")
            out.append(self._features(st, times[-1], seq))
            return
        if "sensor_disconnected" in flags:
            st.reset("sensor_disconnected")
            out.append(self._features(st, times[-1], seq))
            return
        if "stale" in flags:
            st.reset("sensor_stale")
            out.append(self._features(st, times[-1], seq))
            return

        limits, _ = self._limits_for(st)
        overflowed = False
        for t, row in zip(times, rec["samples"]):
            if limits is None:
                clipped = None
            else:
                clipped = any(v is not None and (v <= limits[0] or v >= limits[1]) for v in row)
            if st.segment_start is None and any(v is not None for v in row):
                st.segment_start = t
            if st.segment_start is not None:
                overflowed |= st.window.append(WindowSample(t, tuple(row), clipped))  # type: ignore[union-attr]
        st.window.evict_older_than(times[-1])  # type: ignore[union-attr]
        st.batch_flags.append((times[-1], flags))
        while st.batch_flags and st.batch_flags[0][0] <= times[-1] - st.cfg.window_s * 1e6:
            st.batch_flags.popleft()
        if overflowed:
            self._event(out, "buffer_overflow", name, times[-1], seq,
                        {"capacity": st.window.capacity,  # type: ignore[union-attr]
                         "total_discarded": st.window.overflow_count})  # type: ignore[union-attr]
        out.append(self._features(st, times[-1], seq))

    def _check_stale(self, out: list[dict], rx: float | None) -> None:
        if self.config.clock == "host":
            now, attr = rx, "last_rx"
        else:
            times = [s.last_t for s in self._streams.values() if s.last_t is not None]
            now, attr = (max(times) if times else None), "last_t"
        if now is None:
            return
        limit = self.config.stale_after_s * (1e6 if attr == "last_t" else 1.0)
        for st in self._streams.values():
            last = getattr(st, attr)
            if last is None or st.stale:
                continue
            if now - last > limit:
                st.stale = True
                self._event(out, "stream_stale", st.name, st.last_t, self._seq_last,
                            {"silent_for_s": round((now - last) / (1e6 if attr == "last_t" else 1), 6),
                             "clock": self.config.clock})
                self._invalidate(out, st, "stale", self._seq_last)

    def _features(self, st: _Stream, t_end: float | None, seq: int | None) -> dict:
        cfg = st.cfg
        samples = list(st.window) if st.window is not None else []
        limits, limits_source = self._limits_for(st)
        reasons: list[str] = []
        window: dict[str, Any] = {"length_s": cfg.window_s, "samples": len(samples),
                                  "start_us": round(samples[0].t_us) if samples else None,
                                  "end_us": None if t_end is None else round(t_end),
                                  "nominal_rate_hz": st.rate, "rate_source": st.rate_source,
                                  "measured_rate_hz": features.measured_rate_hz(samples)}
        complete = sum(all(v is not None for v in s.values) for s in samples)
        clipped = sum(1 for s in samples if s.clipped)
        device_flags = sorted({f for _, fl in st.batch_flags for f in fl})

        if st.segment_start is None or t_end is None or st.stale:
            status = "unavailable"
            reasons.append("stale" if st.stale else st.unavailable_reason)
            expected = coverage = None
        else:
            span_s = (t_end - st.segment_start) / 1e6 + 1 / st.rate  # type: ignore[operator]
            expected = st.rate * min(cfg.window_s, span_s)  # type: ignore[operator]
            coverage = min(1.0, complete / expected) if expected else None
            if span_s < cfg.effective_warmup_s:
                status = "warming_up"
                reasons.append("warming_up")
                if st.unavailable_reason != "no_data":
                    reasons.append(st.unavailable_reason)  # why the window restarted
            elif coverage is None or coverage < cfg.min_coverage:
                status = "unavailable"
                reasons.append("insufficient_coverage")
            else:
                status = "ok"
                if complete < len(samples):
                    reasons.append("missing_values")
                if clipped:
                    reasons.append("clipped")
                if "clipped" in device_flags and not clipped:
                    # Honour the device's own clipping report even when host-side
                    # limit checks pass (or are unavailable).
                    reasons.append("device_flagged_clipping")
                if reasons:
                    status = "degraded"
        window["expected_samples"] = None if expected is None else round(expected, 2)
        window["complete_samples"] = complete
        window["coverage"] = None if coverage is None else round(coverage, 4)

        usable = status in ("ok", "degraded")
        stats = features.window_stats(samples, st.channels or []) if usable else None
        rec = self._base("stream_features")
        rec.update({
            "stream": st.name,
            "t_us": window["end_us"],
            "source_seq": seq,
            "status": status,
            "reasons": reasons,
            "window": window,
            "quality": {
                "clipped_samples": clipped if limits is not None else None,
                "clipping_detection": {"config": "config_limits", "device_hello": "device_declared_limits",
                                       "unknown": "unavailable_unknown_limits"}[limits_source],
                "device_flags": device_flags,
            },
            "features": {
                "window_stats": {
                    "status": status,
                    "units": dict(st.units) if st.units else None,
                    "mean": stats["mean"] if stats else None,
                    "ac_rms": stats["ac_rms"] if stats else None,
                },
            },
        })
        self.summary.feature_status[st.name][status] += 1
        return rec


def _min_provenance(prov: dict) -> dict:
    return {k: prov[k] for k in ("source", "recording_id", "original_source") if k in prov}
