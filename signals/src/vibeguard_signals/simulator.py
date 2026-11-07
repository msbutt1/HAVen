"""Deterministic simulated wearable -- SIMULATED DATA ONLY.

Emits device telemetry contract v1 records (``contract.py``) for named
scenarios. Signals are synthetic surrogates chosen to exercise the pipeline:
EMG is amplitude-scaled Gaussian noise around a synthetic ADC midpoint, the
IMU is a gravity vector plus optional sinusoidal vibration. Neither is a
physiological or sensor model, and nothing generated here says anything
about real sensor performance.

Three clocks are kept separate:

* device time  -- ``t0_us`` / ``t_us`` in records; microseconds since the
  current simulated boot, restarting at every reboot.
* simulation time -- microseconds since the start of the run, continuous
  across reboots. Used to place phases and faults; not in the records.
* wall time -- only used by the CLI to pace output (``--speed``/``--fast``).
  Pacing never changes record contents.

Quality flags such as ``sensor_disconnected`` and ``stale`` are emitted here
because the scenario injects that condition, standing in for what firmware
detection would report. The contract validator cannot infer these conditions
from sample values; it only checks that flags and values are consistent.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import random
import secrets
from dataclasses import asdict, dataclass, field
from typing import Iterator

from . import __version__, contract

GENERATOR = "vibeguard-sim"

# Simulator-internal constants. These describe the SYNTHETIC device only and
# are not assumptions about the real ADC, EMG front end, or IMU.
SIM_EMG_LIMITS = (0, 4095)
SIM_EMG_OFFSET = 2048
SIM_IMU_LIMITS = (-32768, 32767)
SIM_COUNTS_PER_G = 4096
EMG_RELAXED_SIGMA = 6.0  # counts, at activation 0
EMG_MAX_SIGMA = 400.0  # counts, at activation 1, gain 1
IMU_NOISE_SIGMA = 3.0  # counts per axis
VIBRATION_AXIS = (0.6, 0.3, 0.74)  # direction of synthetic vibration (normalised below)

HELLO_AT_US = 1_000
STATUS0_AT_US = 2_000
SAMPLES_START_US = 5_000
BOOT_GAP_US = 300_000
MAX_RUN_ID_LEN = 48  # "sim-" + run_id + "-b<n>" must fit the contract's 64-char ids  # simulation time with no device output during a reboot
MAX_DURATION_S = 3600.0
NYQUIST_MARGIN = 0.45  # synthetic tones must stay below this fraction of the sample rate

FAULT_KINDS = ("transport_loss", "overrun", "null_samples", "disconnect", "stale", "reboot")


@dataclass(frozen=True)
class Phase:
    """Signal parameters over a fraction [start, end) of the run."""
    start: float
    end: float
    activation: float = 0.05  # 0..1 synthetic activation level (simulator knob, not %MVC)
    emg_gain: float = 1.0  # >1 drives the synthetic ADC into clipping
    vib_g: float = 0.0  # synthetic vibration amplitude, in g of the simulator scale
    vib_hz: float = 60.0
    coupling_counts_per_g: float = 0.0  # synthetic vibration leaking into EMG
    gravity: tuple[float, float, float] = (0.0, 0.0, 1.0)


@dataclass(frozen=True)
class Fault:
    """Injected condition over a fraction [start, end) of the run."""
    kind: str
    start: float
    end: float
    stream: str | None = None  # "emg" / "imu" where applicable
    p: float = 0.0  # per-sample probability for null_samples


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    phases: tuple[Phase, ...]
    faults: tuple[Fault, ...] = ()


_TILT = (0.26, -0.10, 0.96)
_HAPPY = (
    Phase(0.0, 0.2, activation=0.05),
    Phase(0.2, 0.4, activation=0.8),
    Phase(0.4, 0.6, activation=0.05, vib_g=0.5),
    Phase(0.6, 0.8, activation=0.8, vib_g=0.5),
    Phase(0.8, 1.0, activation=0.05),
)

SCENARIOS: dict[str, Scenario] = {s.name: s for s in (
    Scenario("happy_path", "Clean run: relaxed, stronger activation, vibration, both, relaxed. No faults.",
             _HAPPY),
    Scenario("relaxed", "Relaxed synthetic EMG, stationary IMU.", (Phase(0.0, 1.0),)),
    Scenario("strong_activation", "Relaxed, then stronger synthetic activation, then relaxed.",
             (Phase(0.0, 0.3), Phase(0.3, 0.7, activation=0.8), Phase(0.7, 1.0))),
    Scenario("stationary_gravity", "Stationary IMU with a tilted gravity vector; no vibration.",
             (Phase(0.0, 1.0, gravity=_TILT),)),
    Scenario("vibration", "Controlled synthetic sinusoidal vibration switched on mid-run.",
             (Phase(0.0, 0.25), Phase(0.25, 0.75, vib_g=0.5), Phase(0.75, 1.0))),
    Scenario("vibration_emg_mixed",
             "Synthetic vibration leaking into EMG as an additive tone (synthetic interference, "
             "not a physiological or electrode model), with and without activation.",
             (Phase(0.0, 0.2),
              Phase(0.2, 0.5, vib_g=0.5, coupling_counts_per_g=150.0),
              Phase(0.5, 0.8, activation=0.7, vib_g=0.5, coupling_counts_per_g=150.0),
              Phase(0.8, 1.0))),
    Scenario("clipping", "Synthetic EMG overdriven into the simulated ADC limits.",
             (Phase(0.0, 0.3), Phase(0.3, 0.7, activation=1.0, emg_gain=8.0), Phase(0.7, 1.0))),
    Scenario("missing_batches", "Records lost in transport (sequence-number gap).",
             _HAPPY, (Fault("transport_loss", 0.4, 0.5),)),
    Scenario("invalid_samples",
             "Sensor-level faults: individual null samples, EMG disconnected, IMU stale.",
             _HAPPY,
             (Fault("null_samples", 0.2, 0.4, "emg", p=0.05),
              Fault("null_samples", 0.2, 0.4, "imu", p=0.05),
              Fault("disconnect", 0.5, 0.7, "emg"),
              Fault("stale", 0.75, 0.85, "imu"))),
    Scenario("reboot", "Device reboot mid-run: new boot_id, seq/sample index/device time restart.",
             _HAPPY, (Fault("reboot", 0.5, 0.5),)),
    Scenario("fault_mix", "Every fault type in one run (clipping, loss, overrun, nulls, "
             "disconnect, stale, reboot).",
             (Phase(0.0, 0.1), Phase(0.1, 0.2, activation=1.0, emg_gain=8.0)) + _HAPPY[1:],
             (Fault("transport_loss", 0.25, 0.30),
              Fault("overrun", 0.32, 0.34, "emg"),
              Fault("overrun", 0.36, 0.37, "imu"),
              Fault("null_samples", 0.40, 0.45, "emg", p=0.1),
              Fault("reboot", 0.55, 0.55),
              Fault("disconnect", 0.60, 0.70, "emg"),
              Fault("stale", 0.72, 0.80, "imu"),
              Fault("null_samples", 0.82, 0.86, "imu", p=0.1))),
)}


@dataclass(frozen=True)
class SimConfig:
    scenario: str = "happy_path"
    seed: int = 0
    duration_s: float = 10.0
    emg_rate_hz: float = 1000.0  # synthetic; real rate unknown until hardware
    imu_rate_hz: float = 400.0  # synthetic; real ODR unknown until hardware
    batch_ms: float = 50.0
    device_id: str = "sim-wearable-01"
    imu_jitter_frac: float = 0.005  # +/- fraction of the IMU interval
    status_interval_s: float = 1.0
    vibration_hz: float | None = None  # overrides the scenario's vibration frequency

    def validate(self) -> Scenario:
        if self.scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {self.scenario!r}; choose from {sorted(SCENARIOS)}")
        if not 0 < self.duration_s <= MAX_DURATION_S:
            raise ValueError(f"duration_s must be in (0, {MAX_DURATION_S:g}]")
        for name in ("emg_rate_hz", "imu_rate_hz"):
            rate = getattr(self, name)
            if not 0 < rate <= contract.MAX_SAMPLE_RATE_HZ:
                raise ValueError(f"{name} must be in (0, {contract.MAX_SAMPLE_RATE_HZ:g}]")
        if not 1 <= self.batch_ms <= 1000:
            raise ValueError("batch_ms must be in [1, 1000]")
        if not 0 <= self.imu_jitter_frac < 0.05:
            raise ValueError("imu_jitter_frac must be in [0, 0.05)")
        if self.status_interval_s <= 0:
            raise ValueError("status_interval_s must be > 0")
        if not contract.ID_RE.match(self.device_id):
            raise ValueError("device_id must match the contract id pattern")
        scen = SCENARIOS[self.scenario]
        for ph in scen.phases:
            hz = self.vibration_hz or ph.vib_hz
            if ph.vib_g > 0 and hz >= NYQUIST_MARGIN * self.imu_rate_hz:
                raise ValueError(f"vibration {hz:g} Hz must be below {NYQUIST_MARGIN:.0%} of "
                                 f"imu_rate_hz ({self.imu_rate_hz:g}) to avoid aliasing")
            if ph.coupling_counts_per_g > 0 and hz >= NYQUIST_MARGIN * self.emg_rate_hz:
                raise ValueError(f"vibration {hz:g} Hz must be below {NYQUIST_MARGIN:.0%} of "
                                 f"emg_rate_hz ({self.emg_rate_hz:g})")
        return scen


@dataclass
class StreamStats:
    samples_acquired: int = 0
    samples_dropped: int = 0
    null_values: int = 0
    batches: int = 0
    clipped_batches: int = 0
    disconnected_batches: int = 0
    stale_batches: int = 0


@dataclass
class RunSummary:
    recording_id: str
    boots: list[str] = field(default_factory=list)
    records_generated: int = 0
    records_lost: int = 0
    lost_by_type: dict[str, int] = field(default_factory=dict)
    emg: StreamStats = field(default_factory=StreamStats)
    imu: StreamStats = field(default_factory=StreamStats)

    @property
    def records_emitted(self) -> int:
        return self.records_generated - self.records_lost

    def to_dict(self) -> dict:
        d = asdict(self)
        d["records_emitted"] = self.records_emitted
        return d


@dataclass(frozen=True)
class SimRecord:
    sim_time_us: int  # simulation-time emission instant (for pacing only)
    record: dict
    lost: bool  # True: generated (seq consumed) but lost in simulated transport


@dataclass
class _Sample:
    index: int
    t_us: int
    values: list[int] | None
    dropped: bool
    disconnected: bool
    stale: bool


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float]:
    n = math.sqrt(sum(x * x for x in v))
    return (v[0] / n, v[1] / n, v[2] / n)


class Simulation:
    """Iterate :meth:`records` once; :attr:`summary` is complete afterwards."""

    def __init__(self, config: SimConfig, run_id: str | None = None):
        """``run_id`` fixes the run identity (recording_id / boot_ids) for fixtures.

        By default every Simulation gets a fresh random identity, as a real device
        gets a new boot_id per boot. Sample values and timestamps depend only on
        ``config`` (including the seed), never on the identity.
        """
        self.config = config
        self.scenario = config.validate()
        if run_id is None:
            run_id, self.identity = secrets.token_hex(6), "random_per_run"
        else:
            if not isinstance(run_id, str) or len(run_id) > MAX_RUN_ID_LEN \
                    or not contract.ID_RE.match(run_id):
                raise ValueError(f"run_id must be 1-{MAX_RUN_ID_LEN} chars of [A-Za-z0-9._:-]")
            self.identity = "fixed"
        self._dur_us = round(config.duration_s * 1e6)
        self._phase_starts = [ph.start * self._dur_us for ph in self.scenario.phases]
        ident = json.dumps({"config": asdict(config), "generator": GENERATOR,
                            "version": __version__}, sort_keys=True)
        self.config_digest = hashlib.sha256(ident.encode()).hexdigest()[:16]
        self.recording_id = f"sim-{run_id}"
        self.summary = RunSummary(self.recording_id)
        self._vib_axis = _unit(VIBRATION_AXIS)

    # -- timeline helpers -----------------------------------------------------

    def _phase(self, g_us: float) -> Phase:
        i = bisect.bisect_right(self._phase_starts, g_us) - 1
        return self.scenario.phases[max(0, i)]

    def _active(self, kind: str, g_us: float, stream: str | None = None) -> Fault | None:
        for f in self.scenario.faults:
            if f.kind == kind and (stream is None or f.stream == stream) \
                    and f.start * self._dur_us <= g_us < f.end * self._dur_us:
                return f
        return None

    def _boots(self) -> list[tuple[int, int]]:
        bounds, start = [], 0
        for r in sorted(f.start for f in self.scenario.faults if f.kind == "reboot"):
            at = round(r * self._dur_us)
            bounds.append((start, at))
            start = at + BOOT_GAP_US
        bounds.append((start, self._dur_us))
        return [(s, e) for s, e in bounds if e - s > SAMPLES_START_US]

    def _rng(self, boot: int, stream: str, purpose: str) -> random.Random:
        return random.Random(f"{self.config.seed}|{boot}|{stream}|{purpose}")

    def _vib(self, ph: Phase, g_us: float) -> float:
        hz = self.config.vibration_hz or ph.vib_hz
        return ph.vib_g * math.sin(2 * math.pi * hz * g_us / 1e6) if ph.vib_g else 0.0

    # -- sample generation ----------------------------------------------------

    def _samples(self, boot: int, b_start: int, b_end: int, stream: str) -> Iterator[_Sample]:
        cfg = self.config
        rate = cfg.emg_rate_hz if stream == "emg" else cfg.imu_rate_hz
        dt = 1e6 / rate
        values_rng = self._rng(boot, stream, "values")
        jitter_rng = self._rng(boot, stream, "jitter")
        null_rng = self._rng(boot, stream, "nulls")
        lo, hi = SIM_EMG_LIMITS if stream == "emg" else SIM_IMU_LIMITS
        last_good: list[int] | None = None
        jitter = cfg.imu_jitter_frac if stream == "imu" else 0.0
        # Offset by the maximum jitter so the earliest timestamp is never before
        # SAMPLES_START_US, at any supported rate (low rates have huge intervals).
        start = SAMPLES_START_US + jitter * dt
        i = 0
        while True:
            t = start + i * dt
            if b_start + t >= b_end:
                return
            if jitter:
                t += jitter_rng.uniform(-jitter, jitter) * dt
            g = b_start + t
            ph = self._phase(g)
            vib = self._vib(ph, g)
            # Always draw the same random numbers per sample, so fault windows
            # never shift the values of unaffected samples.
            if stream == "emg":
                sigma = (EMG_RELAXED_SIGMA + ph.activation * (EMG_MAX_SIGMA - EMG_RELAXED_SIGMA)) \
                    * ph.emg_gain
                raw = [SIM_EMG_OFFSET + values_rng.gauss(0.0, sigma) + ph.coupling_counts_per_g * vib]
            else:
                grav = _unit(ph.gravity)
                raw = [(grav[k] + vib * self._vib_axis[k]) * SIM_COUNTS_PER_G
                       + values_rng.gauss(0.0, IMU_NOISE_SIGMA) for k in range(3)]
            values = [min(hi, max(lo, round(v))) for v in raw]
            null_draw = null_rng.random()

            disconnected = stream == "emg" and self._active("disconnect", g, "emg") is not None
            stale = stream == "imu" and self._active("stale", g, "imu") is not None
            nf = self._active("null_samples", g, stream)
            out: list[int] | None = values
            if disconnected or (nf is not None and null_draw < nf.p):
                out = None
            elif stale and last_good is not None:
                out = list(last_good)  # sensor not updating: device re-reads the old value
            if out is not None and not stale:
                last_good = out
            yield _Sample(i, round(t), out,
                          self._active("overrun", g, stream) is not None, disconnected, stale)
            i += 1

    def _batches(self, samples: Iterator[_Sample], n_max: int):
        """Group samples into batches; split at drops, condition changes, and size."""
        cur: list[_Sample] = []
        cur_drop = pending = 0
        key = None
        for s in samples:
            if s.dropped:
                if cur:
                    yield cur, cur_drop
                    cur, cur_drop = [], 0
                pending += 1
                continue
            k = (s.disconnected, s.stale)
            if cur and (len(cur) >= n_max or k != key):
                yield cur, cur_drop
                cur, cur_drop = [], 0
            if not cur:
                cur_drop, pending, key = pending, 0, k
            cur.append(s)
        if cur:
            yield cur, cur_drop

    # -- record construction --------------------------------------------------

    def _base(self, rtype: str, boot_id: str) -> dict:
        return {"schema": contract.SCHEMA, "version": 1, "type": rtype,
                "device_id": self.config.device_id, "boot_id": boot_id, "seq": -1,
                "provenance": {"source": "simulated", "recording_id": self.recording_id}}

    def _run_metadata(self) -> dict:
        d = self._dur_us / 1e6
        return {
            "generator": GENERATOR, "generator_version": __version__,
            "seed": self.config.seed, "scenario": self.scenario.name,
            "config_digest": self.config_digest, "identity": self.identity,
            "description": self.scenario.description, "config": asdict(self.config),
            "declared_faults": [
                {"kind": f.kind, "stream": f.stream, "start_s": round(f.start * d, 6),
                 "end_s": round(f.end * d, 6), **({"p": f.p} if f.p else {})}
                for f in self.scenario.faults],
            "time_note": "fault/phase times are simulation seconds since run start, "
                         "not device time and not wall time",
            "signal_note": "synthetic surrogate signals; not physiological or sensor models",
        }

    def _hello(self, boot_id: str) -> dict:
        cfg = self.config
        rec = self._base("hello", boot_id)
        rec["provenance"]["detail"] = self._run_metadata()
        rec.update({
            "t_us": HELLO_AT_US,
            "firmware": {"name": GENERATOR, "version": __version__},
            "streams": {
                "emg": {"nominal_rate_hz": cfg.emg_rate_hz, "channels": ["emg0"],
                        "units": {"emg0": "adc_counts"},
                        "raw_limits": {"min": SIM_EMG_LIMITS[0], "max": SIM_EMG_LIMITS[1]},
                        "sensor_model": "synthetic EMG surrogate (simulator)"},
                "imu": {"nominal_rate_hz": cfg.imu_rate_hz, "channels": ["ax", "ay", "az"],
                        "units": {c: "raw_counts" for c in ("ax", "ay", "az")},
                        "raw_limits": {"min": SIM_IMU_LIMITS[0], "max": SIM_IMU_LIMITS[1]},
                        "sensor_model": "synthetic accelerometer surrogate (simulator)"}},
            "hardware": {"board": "none (simulated)"},
        })
        return rec

    def _batch(self, stream: str, boot_id: str, rows: list[_Sample], dropped: int) -> dict:
        cfg = self.config
        rate = cfg.emg_rate_hz if stream == "emg" else cfg.imu_rate_hz
        channels = ["emg0"] if stream == "emg" else ["ax", "ay", "az"]
        lo, hi = SIM_EMG_LIMITS if stream == "emg" else SIM_IMU_LIMITS
        samples = [s.values if s.values is not None else [None] * len(channels) for s in rows]
        if stream == "emg":
            timing = {"mode": "uniform", "t0_us": rows[0].t_us, "interval_us": 1e6 / rate}
        else:
            timing = {"mode": "explicit", "t_us": [s.t_us for s in rows]}
        flags = []
        stats = getattr(self.summary, stream)
        stats.batches += 1
        stats.samples_dropped += dropped
        if dropped:
            flags.append("overrun")
        nulls = sum(v is None for row in samples for v in row)
        stats.null_values += nulls
        if nulls:
            flags.append("missing_values")
        if rows[0].disconnected:
            flags.append("sensor_disconnected")
            stats.disconnected_batches += 1
        if rows[0].stale:
            flags.append("stale")
            stats.stale_batches += 1
        if any(v is not None and (v <= lo or v >= hi) for row in samples for v in row):
            flags.append("clipped")
            stats.clipped_batches += 1
        rec = self._base(f"{stream}_batch", boot_id)
        rec.update({"sample_rate_hz": rate, "first_sample_index": rows[0].index,
                    "timing": timing, "channels": channels,
                    "units": {c: ("adc_counts" if stream == "emg" else "raw_counts") for c in channels},
                    "samples": samples,
                    "quality": {"flags": flags, **({"dropped_samples_before": dropped} if dropped else {})}})
        return rec

    def _status(self, boot_id: str, b_start: int, t_us: int, drops: dict[str, list[int]]) -> dict:
        g = b_start + t_us
        rec = self._base("status", boot_id)
        rec.update({
            "t_us": t_us, "state": "streaming",
            "sensors": {"emg": "disconnected" if self._active("disconnect", g, "emg") else "ok",
                        "imu": "stale" if self._active("stale", g, "imu") else "ok"},
            "counters": {f"{s}_dropped_samples": bisect.bisect_right(drops[s], t_us)
                         for s in ("emg", "imu")},
        })
        return rec

    def _boot_records(self, boot: int, b_start: int, b_end: int) -> list[tuple[int, int, dict]]:
        boot_id = f"{self.recording_id}-b{boot}"
        self.summary.boots.append(boot_id)
        out: list[tuple[int, int, dict]] = [(b_start + HELLO_AT_US, 0, self._hello(boot_id))]
        drops: dict[str, list[int]] = {}
        for prio, stream in ((2, "emg"), (3, "imu")):
            rate = self.config.emg_rate_hz if stream == "emg" else self.config.imu_rate_hz
            dt = 1e6 / rate
            n_max = max(1, min(contract.MAX_SAMPLES_PER_BATCH, round(rate * self.config.batch_ms / 1000)))
            drops[stream] = []
            stats = getattr(self.summary, stream)

            def tracked(it, stream=stream, stats=stats):
                for s in it:
                    stats.samples_acquired += 1
                    if s.dropped:
                        drops[stream].append(s.t_us)
                    yield s

            for rows, dropped in self._batches(tracked(self._samples(boot, b_start, b_end, stream)), n_max):
                emit = b_start + round(rows[-1].t_us + dt)
                out.append((emit, prio, self._batch(stream, boot_id, rows, dropped)))

        # periodic status plus status at every disconnect/stale transition
        times = set()
        t = STATUS0_AT_US
        while b_start + t < b_end:
            times.add(t)
            t += round(self.config.status_interval_s * 1e6)
        for f in self.scenario.faults:
            if f.kind in ("disconnect", "stale"):
                for edge in (f.start, f.end):
                    tu = round(edge * self._dur_us) - b_start
                    if STATUS0_AT_US <= tu and b_start + tu < b_end:
                        times.add(tu)
        for tu in sorted(times):
            out.append((b_start + tu, 1, self._status(boot_id, b_start, tu, drops)))
        out.sort(key=lambda x: (x[0], x[1]))
        return out

    def records(self) -> Iterator[SimRecord]:
        for boot, (b_start, b_end) in enumerate(self._boots()):
            for seq, (emit, _prio, rec) in enumerate(self._boot_records(boot, b_start, b_end)):
                rec["seq"] = seq
                contract.require_valid(rec)
                lost = rec["type"] != "hello" and self._active("transport_loss", emit) is not None
                self.summary.records_generated += 1
                if lost:
                    self.summary.records_lost += 1
                    self.summary.lost_by_type[rec["type"]] = self.summary.lost_by_type.get(rec["type"], 0) + 1
                yield SimRecord(emit, rec, lost)
