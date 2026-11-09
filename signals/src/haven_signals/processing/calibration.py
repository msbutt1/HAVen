"""Relaxed/squeeze EMG calibration state machine and normalisation.

States::

    uncalibrated --start(relaxed)--> capturing_relaxed --ok--> relaxed_captured
    relaxed_captured --start(squeeze)--> capturing_squeeze --ok--> calibrated
    any capture --quality failure / interruption--> failed
    calibrated --incompatible change--> invalidated
    start(relaxed) from any state discards the active calibration (explicit restart).

Capture: a capture begins at the first usable (ok/degraded) envelope update
after the command and lasts ``relaxed_s`` / ``squeeze_s`` of device time.
The first ``discard_s`` are excluded. Any unusable envelope update after the
capture began (gap, reset, warm-up, stale, disconnect, rate mismatch, ...)
fails it: captures must be contiguous. Clipped samples beyond
``max_clipped_fraction`` fail it. Fewer than ``min_updates`` retained
updates fail it.

References: per channel, the median envelope over the retained updates
(robust to brief spikes). P25/P75 are recorded for inspection.

Acceptance: per channel, squeeze >= min_ratio x relaxed AND
squeeze - relaxed >= min_separation (envelope units). So the normalisation
denominator is never near zero.

Normalised activation = (envelope - relaxed) / (squeeze - relaxed),
reported unbounded. ``display`` is clamped to [0, 1]; ``above_reference`` and
``below_relaxed`` flag values outside the calibration range instead of hiding
them. This is a relative muscle-activation proxy for this wearer and
electrode placement, not force and not %MVC.

Compatibility: a calibration is used only while device_id, provenance, the
EMG processing signature (rate, channels, units, pipeline config) and, unless
``survives_reboot``, boot_id all match. Otherwise it is invalidated, visibly.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass

import numpy as np

from .config import CalibrationConfig
from .emg import EnvelopeState

CAL_SCHEMA = "vibeguard.emg_calibration"
CAL_VERSION = 1
LABEL = "relative_muscle_activation_proxy"
LABEL_NOTE = ("normalised to this wearer's relaxed and squeeze calibration for the current "
              "electrode placement; not force, not %MVC, not clinically validated")
USABLE = ("ok", "degraded")
_EMG_CH = re.compile(r"^emg[0-7]$")
_SOURCES = ("live", "simulated", "replay")
# Resets that are followed by their own explicit handling (boot change, reconfiguration).
_RESETS_HANDLED_ELSEWHERE = ("no_data", "configured")


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_record(record, cfg: CalibrationConfig) -> list[str]:
    """Structural and numerical checks for a calibration record. Empty list = valid."""
    if not isinstance(record, dict):
        return ["record is not an object"]
    p: list[str] = []
    if record.get("schema") != CAL_SCHEMA:
        p.append(f"schema must be {CAL_SCHEMA!r}")
    if record.get("version") != CAL_VERSION:
        p.append(f"version must be {CAL_VERSION}")
    if record.get("label") != LABEL:
        p.append(f"label must be {LABEL!r}")
    for key in ("calibration_id", "device_id", "signature"):
        if not isinstance(record.get(key), str) or not record.get(key):
            p.append(f"{key} must be a non-empty string")
    if not (record.get("boot_id") is None or isinstance(record.get("boot_id"), str)):
        p.append("boot_id must be a string or null")
    prov = record.get("provenance")
    if not isinstance(prov, dict) or prov.get("source") not in _SOURCES:
        p.append("provenance must be an object with a valid source")
    ref = record.get("reference")
    if not isinstance(ref, dict) or not isinstance(ref.get("relaxed"), dict) \
            or not isinstance(ref.get("squeeze"), dict):
        return p + ["reference must contain relaxed and squeeze objects"]
    rel, sq = ref["relaxed"], ref["squeeze"]
    if not rel or set(rel) != set(sq):
        p.append("relaxed and squeeze references must name the same, non-empty channels")
    for ch in sorted(set(rel) | set(sq)):
        if not isinstance(ch, str) or not _EMG_CH.match(ch):
            p.append(f"invalid channel name {ch!r}")
            continue
        r, s = rel.get(ch), sq.get(ch)
        if not (_finite(r) and _finite(s)):
            p.append(f"{ch}: references must be finite numbers")
            continue
        if r < 0:
            p.append(f"{ch}: relaxed reference must be >= 0")
        if not (s >= cfg.min_ratio * r and s - r >= cfg.min_separation):
            p.append(f"{ch}: insufficient separation (relaxed {r}, squeeze {s}; need ratio >= "
                     f"{cfg.min_ratio} and difference >= {cfg.min_separation})")
    pipe = record.get("pipeline")
    if pipe is not None:
        if not isinstance(pipe, dict) or not isinstance(pipe.get("channels"), list):
            p.append("pipeline must be an object with a channels list")
        elif set(pipe["channels"]) != set(rel):
            p.append("pipeline channels do not match the reference channels")
    return p


@dataclass(frozen=True)
class CalContext:
    device_id: str | None
    boot_id: str | None
    provenance: dict | None
    signature: str | None  # EmgPipeline.signature
    pipeline: dict | None  # EmgPipeline.describe()


@dataclass
class _Capture:
    phase: str
    signature: str
    boot_id: str | None
    t_first: float | None = None
    last_t: float | None = None
    samples0: int = 0
    clipped0: int = 0
    values: list | None = None


class CalibrationManager:
    def __init__(self, cfg: CalibrationConfig):
        self.cfg = cfg
        self.state = "uncalibrated"
        self.reason: str | None = None
        self.active: dict | None = None
        self._relaxed: dict | None = None
        self._cap: _Capture | None = None
        self.events: list[tuple[str, dict, float | None]] = []  # drained by the Processor

    # -- commands -------------------------------------------------------------

    def start(self, phase: str, ctx: CalContext) -> bool:
        if phase not in ("relaxed", "squeeze"):
            raise ValueError("phase must be 'relaxed' or 'squeeze'")
        if ctx.signature is None:
            return self._reject(phase, "emg_not_configured")
        if phase == "squeeze":
            rel = self._relaxed
            if self.state != "relaxed_captured" or rel is None:
                return self._reject(phase, "relaxed_reference_missing")
            if rel["signature"] != ctx.signature or rel["boot_id"] != ctx.boot_id:
                return self._reject(phase, "relaxed_reference_incompatible")
        else:
            if self.active is not None:
                self._emit("calibration_cleared", {"calibration_id": self.active["calibration_id"],
                                                   "reason": "recalibration_started"})
            self.active = None
            self._relaxed = None
        self._cap = _Capture(phase, ctx.signature, ctx.boot_id, values=[])
        self._set(f"capturing_{phase}", None, phase=phase)
        return True

    def cancel(self) -> None:
        if self._cap is not None:
            self._fail("cancelled")

    def invalidate(self, reason: str) -> None:
        """Called by the Processor on provenance change, boot change, etc."""
        if self._cap is not None:
            self._fail(f"interrupted_{reason}")
        if self.active is not None:
            cid = self.active["calibration_id"]
            self.active = None
            self._relaxed = None
            self._set("invalidated", reason, calibration_id=cid)
        elif self._relaxed is not None:
            self._relaxed = None
            if self.state == "relaxed_captured":
                self._set("invalidated", reason)

    def on_signal_reset(self, reason: str) -> None:
        """An EMG filter reset ends the contiguity a capture requires."""
        if reason in _RESETS_HANDLED_ELSEWHERE:
            return
        if self._cap is not None and self._cap.t_first is not None:
            self._fail(f"interrupted_{reason}")

    def on_boot_change(self) -> None:
        if self._cap is not None:
            self._fail("interrupted_boot_change")
        if not self.cfg.survives_reboot:
            self.invalidate("boot_change")

    def load(self, record: dict, ctx: CalContext) -> bool:
        """Install a previously exported calibration; checked like any other."""
        problems = validate_record(record, self.cfg)
        if problems:
            # Rejected visibly; the current state (and any active calibration) is unchanged.
            self._emit("calibration_rejected", {"reason": "invalid_calibration_record",
                                                "problems": problems, "state": self.state})
            return False
        self.active = record
        self._relaxed = None
        self._cap = None
        self._set("calibrated", "loaded", calibration_id=record["calibration_id"])
        self.check_compatible(ctx)
        return self.active is not None

    # -- per envelope update ---------------------------------------------------

    def check_compatible(self, ctx: CalContext) -> None:
        a = self.active
        if a is None:
            return
        # Fields not known yet (e.g. a calibration loaded before any data) are
        # checked as soon as they become known; they are never assumed to match.
        reason = None
        if ctx.device_id is not None and a["device_id"] != ctx.device_id:
            reason = "device_mismatch"
        elif ctx.provenance is not None and a["provenance"] != ctx.provenance:
            reason = "provenance_mismatch"
        elif ctx.signature is not None and a["signature"] != ctx.signature:
            reason = "processing_config_mismatch"
        elif ctx.pipeline is not None and set(ctx.pipeline["channels"] or []) != set(a["reference"]["relaxed"]):
            reason = "channel_mismatch"
        elif (not self.cfg.survives_reboot and ctx.boot_id is not None
              and a["boot_id"] != ctx.boot_id):
            reason = "boot_mismatch"
        if reason:
            self.invalidate(reason)

    def observe(self, env: EnvelopeState, t_us: float | None, ctx: CalContext) -> None:
        cap = self._cap
        if cap is None:
            return
        if ctx.signature != cap.signature or ctx.boot_id != cap.boot_id:
            self._fail("interrupted_processing_change")
            return
        if env.status not in USABLE or t_us is None:
            if cap.t_first is not None:
                self._fail("interrupted_" + (env.reasons[-1] if env.reasons else env.status))
            return  # not started yet: wait for a usable envelope
        if cap.last_t is not None and t_us <= cap.last_t:
            return  # same update reported twice
        if cap.t_first is None:
            cap.t_first, cap.samples0, cap.clipped0 = t_us, env.samples_total, env.clipped_total
        cap.last_t = t_us
        elapsed = (t_us - cap.t_first) / 1e6
        if elapsed >= self.cfg.discard_s:
            cap.values.append((t_us, env.values))  # type: ignore[union-attr]
        duration = self.cfg.relaxed_s if cap.phase == "relaxed" else self.cfg.squeeze_s
        if elapsed >= duration:
            self._finish(env, ctx)

    def normalized(self, env: EnvelopeState, ctx: CalContext) -> dict:
        a = self.active
        unverified = None in (ctx.device_id, ctx.provenance, ctx.signature) or \
            (not self.cfg.survives_reboot and ctx.boot_id is None)
        if a is not None and unverified:
            # Identity not yet checked against live data: never normalise on trust.
            return {"status": "unavailable", "reasons": ["calibration_unverified"], "value": None,
                    "display": None, "above_reference": None, "below_relaxed": None,
                    "calibration_id": a["calibration_id"]}
        out = {"status": "unavailable", "reasons": [], "value": None, "display": None,
               "above_reference": None, "below_relaxed": None,
               "calibration_id": a["calibration_id"] if a else None}
        if a is None:
            out["reasons"] = [self.state if self.state != "calibrated" else "uncalibrated"]
            if self.reason and self.state in ("failed", "invalidated"):
                out["reasons"].append(self.reason)
            return out
        if env.status not in USABLE:
            out["status"] = "warming_up" if env.status == "warming_up" else "unavailable"
            out["reasons"] = list(env.reasons)
            return out
        rel, sq = a["reference"]["relaxed"], a["reference"]["squeeze"]
        value = {ch: round((env.values[ch] - rel[ch]) / (sq[ch] - rel[ch]), 4)  # type: ignore[index]
                 for ch in env.values}  # type: ignore[union-attr]
        out.update({
            "status": env.status, "reasons": list(env.reasons), "value": value,
            "display": {ch: min(1.0, max(0.0, v)) for ch, v in value.items()},
            "above_reference": {ch: v > 1.0 for ch, v in value.items()},
            "below_relaxed": {ch: v < 0.0 for ch, v in value.items()},
        })
        return out

    def summary(self) -> dict:
        return {"state": self.state, "reason": self.reason,
                "calibration_id": self.active["calibration_id"] if self.active else None,
                "capturing": self._cap.phase if self._cap else None}

    # -- internals ------------------------------------------------------------

    def _finish(self, env: EnvelopeState, ctx: CalContext) -> None:
        cap = self._cap
        assert cap is not None
        samples = env.samples_total - cap.samples0
        clipped = env.clipped_total - cap.clipped0
        frac = clipped / samples if samples else 1.0
        if len(cap.values) < self.cfg.min_updates:  # type: ignore[arg-type]
            self._fail("insufficient_data", retained_updates=len(cap.values),  # type: ignore[arg-type]
                       required=self.cfg.min_updates)
            return
        if frac > self.cfg.max_clipped_fraction:
            self._fail("clipped", clipped_fraction=round(frac, 6),
                       allowed=self.cfg.max_clipped_fraction)
            return
        channels = list(cap.values[0][1])  # type: ignore[index]
        arr = np.array([[v[ch] for ch in channels] for _, v in cap.values])  # type: ignore[union-attr]
        p25, med, p75 = np.percentile(arr, [25, 50, 75], axis=0)
        summary = {
            "t_start_us": round(cap.t_first), "t_end_us": round(cap.last_t),  # type: ignore[arg-type]
            "retained_updates": len(arr), "samples": samples, "clipped_samples": clipped,
            "median": {ch: round(float(m), 4) for ch, m in zip(channels, med)},
            "p25": {ch: round(float(m), 4) for ch, m in zip(channels, p25)},
            "p75": {ch: round(float(m), 4) for ch, m in zip(channels, p75)},
        }
        self._cap = None
        if cap.phase == "relaxed":
            self._relaxed = {"signature": cap.signature, "boot_id": cap.boot_id, "capture": summary}
            self._set("relaxed_captured", None, phase="relaxed", capture=summary)
            return

        rel = self._relaxed["capture"]["median"]  # type: ignore[index]
        sq = summary["median"]
        bad = {ch: {"relaxed": rel[ch], "squeeze": sq[ch]} for ch in channels
               if not (sq[ch] >= self.cfg.min_ratio * rel[ch]
                       and sq[ch] - rel[ch] >= self.cfg.min_separation)}
        if bad:
            self._relaxed = None
            self._fail("insufficient_separation", channels=bad, min_ratio=self.cfg.min_ratio,
                       min_separation=self.cfg.min_separation)
            return
        core = {
            "schema": CAL_SCHEMA, "version": CAL_VERSION, "label": LABEL, "label_note": LABEL_NOTE,
            "device_id": ctx.device_id, "boot_id": cap.boot_id, "provenance": ctx.provenance,
            "signature": cap.signature, "pipeline": ctx.pipeline,
            "calibration_config": asdict(self.cfg),
            "relaxed": self._relaxed["capture"], "squeeze": summary,  # type: ignore[index]
            "reference": {"relaxed": dict(rel), "squeeze": dict(sq)},
            "reference_method": "median envelope over the retained capture updates",
        }
        cid = "cal-" + hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]
        self.active = {"calibration_id": cid, **core}
        self._relaxed = None
        self._set("calibrated", None, calibration_id=cid, phase="squeeze", capture=summary,
                  reference=self.active["reference"])

    def _fail(self, reason: str, **detail) -> None:
        phase = self._cap.phase if self._cap else None
        self._cap = None
        self._relaxed = None  # any failure restarts calibration from the relaxed capture
        self._set("failed", reason, phase=phase, **detail)

    def _reject(self, phase: str, reason: str) -> bool:
        self._emit("calibration_rejected", {"phase": phase, "reason": reason, "state": self.state})
        return False

    def _set(self, state: str, reason: str | None, **detail) -> None:
        self.state, self.reason = state, reason
        self._emit("calibration_state", {"state": state, "reason": reason, **detail})

    def _emit(self, kind: str, detail: dict) -> None:
        self.events.append((kind, detail, None))
