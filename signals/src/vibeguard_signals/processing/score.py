"""EXPERIMENTAL combined score: rate and time-integrated accumulation.

Formula (configurable coefficients, ``ScoreConfig``)::

    rate [au/h] = k_per_hour * (V / v_ref) ** exponent * (1 + activation_weight * a)

* V: combined dynamic accelerometer RMS in m/s^2 (``vibration.physical``).
  Unavailable without a known conversion; raw counts are never scored.
* a: normalised muscle activation clamped to [0, activation_cap], using the
  maximum over EMG channels. With ``activation_mode="ignored"``, a = 0 and the
  output says so.
* au: arbitrary units. The squared exponent is a design choice by analogy to
  energy-like quantities. This is NOT A(8), not a vibration dose, not a
  percentage of any legal or standard limit, and the activation weighting is
  an assumption of this algorithm, not validated vibration transmission.

Interval semantics (independent of EMG/IMU arrival order):

* An update for an input at device time t describes that input over
  (previous update of that input, t], reaching back at most the input's
  freshness limit. An update after a longer silence leaves the older part
  uncovered, and uncovered time is ``<input>_stale``. The first update of an
  input (per boot) only establishes its start.
* Time is FINALISED (added to valid or excluded totals) only up to the point
  that every required input has either covered, or provably will not cover:
  an input whose last update is more than its freshness limit behind the
  newest update of any input is declared stale from its last update onward.
  Because finalisation waits for both inputs, an interval in which either
  input was invalid can never be counted as valid merely because the other
  stream's update arrived first.
* Each finalised sub-interval is valid iff the covering update of every
  required input is usable (status ok/degraded). Its rate uses those covering
  updates' values. Otherwise it is excluded, attributed to a key naming every
  blocking input (sorted, joined with " & "), each with its reason chain
  joined with "+": e.g. ``activation_uncalibrated & vibration_warming_up+gap``.
* Updates at or before an input's last update are ignored, so duplicates and
  reordering never double-count or produce negative time.
* Pending (not yet finalised) state is bounded: it spans at most the largest
  freshness limit of device time.
* Device time restarts at a reboot. A boot change flushes the pending
  interval and starts a new boot segment; the reboot gap itself is not
  measurable in device time. A provenance change starts a new accumulation.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass

from .config import ScoreConfig

LABEL = "experimental_combined_score"
LABEL_NOTE = ("arbitrary units from an experimental formula; not A(8), not a vibration dose, "
              "not a percentage of any limit; activation weighting is an algorithm choice, "
              "not validated vibration transmission")
EXPRESSION = "k_per_hour * (V / v_ref)^exponent * (1 + activation_weight * clamp(a, 0, activation_cap))"
USABLE = ("ok", "degraded")


@dataclass(frozen=True)
class _Input:
    t_us: float
    usable: bool
    value: float | None
    status: str
    reasons: tuple[str, ...]
    degraded: bool


def _block_key(name: str, inp: _Input) -> str:
    """e.g. 'activation_warming_up+gap': the input, its status, and the cause chain."""
    return f"{name}_" + ("+".join(inp.reasons) if inp.reasons else inp.status)


class _Track:
    """Coverage of one input's timeline by its updates (bounded deque of segments)."""

    def __init__(self, name: str, fresh_s: float):
        self.name = name
        self.fresh_us = fresh_s * 1e6
        self.reset()

    def reset(self) -> None:
        self.first_t: float | None = None
        self.last_t: float | None = None
        self.latest: _Input | None = None
        self.segs: deque[tuple[float, float, _Input | None]] = deque()  # (t0, t1], None = stale gap

    def add(self, t: float, inp: _Input) -> bool:
        if self.last_t is not None and t <= self.last_t:
            return False  # duplicate or older: never replaces newer information
        if self.last_t is None:
            self.first_t = t
        else:
            start = max(self.last_t, t - self.fresh_us)
            if start > self.last_t:
                self.segs.append((self.last_t, start, None))  # silence beyond freshness
            self.segs.append((start, t, inp))
        self.last_t, self.latest = t, inp
        return True

    def determined_until(self, newest: float) -> float:
        """Time up to which this input's status is settled."""
        if self.last_t is None:
            return newest - self.fresh_us
        return max(self.last_t, newest - self.fresh_us)

    def block_at(self, b: float, uncovered: str) -> tuple[str | None, _Input | None]:
        """(blocking key or None, covering input) for a sub-interval ending at b."""
        if self.first_t is None or b <= self.first_t:
            return f"{self.name}_no_data", None
        if b > self.last_t:  # type: ignore[operator]
            return f"{self.name}_{uncovered}", None
        for t0, t1, inp in self.segs:
            if t0 < b <= t1:
                if inp is None:
                    return f"{self.name}_stale", None
                return (None if inp.usable else _block_key(self.name, inp)), inp
        return f"{self.name}_no_data", None  # pragma: no cover (segments are contiguous)

    def prune(self, cursor: float) -> None:
        while self.segs and self.segs[0][1] <= cursor:
            self.segs.popleft()

    def boundaries(self, a: float, b: float) -> list[float]:
        pts = [p for seg in self.segs for p in seg[:2] if a < p < b]
        for p in (self.first_t, self.last_t):
            if p is not None and a < p < b:
                pts.append(p)
        return pts


class ScoreEngine:
    def __init__(self, cfg: ScoreConfig):
        self.cfg = cfg
        self.accumulation = 0
        self._vib = _Track("vibration", cfg.vibration_fresh_s)
        self._act = _Track("activation", cfg.activation_fresh_s)
        self._new_accumulation()

    # -- lifecycle ----------------------------------------------------------------

    @property
    def _tracks(self) -> list[_Track]:
        return [self._vib, self._act] if self.cfg.activation_mode == "required" else [self._vib]

    def _new_accumulation(self) -> None:
        self.accumulation += 1
        self.cumulative = 0.0
        self.valid_s = 0.0
        self.degraded_s = 0.0
        self.excluded_s = 0.0
        self.excluded_by_reason: dict[str, float] = {}
        self.boot_segments = 1
        self._segment_reset()

    def _segment_reset(self) -> None:
        self.cursor: float | None = None
        self.newest: float | None = None
        self._finalised_any = False
        self._vib.reset()
        self._act.reset()

    def flush(self, reason: str) -> None:
        """Finalise everything up to the newest update; uncovered time gets ``reason``."""
        if self.newest is not None:
            self._finalise(self.newest, uncovered=reason)

    def on_boot_change(self) -> None:
        self.flush("boot_change")
        self.boot_segments += 1
        self._segment_reset()

    def reset_accumulation(self) -> None:
        self._new_accumulation()

    # -- updates --------------------------------------------------------------------

    def update_vibration(self, t_us: float | None, vib: dict) -> None:
        phys = vib["physical"]
        usable = phys["status"] in USABLE and phys["combined_rms"] is not None
        reasons = tuple(phys["reasons"]) or tuple(vib["reasons"])
        self._update(self._vib, t_us, _Input(t_us or 0.0, usable, phys["combined_rms"] if usable else None,
                                             phys["status"], reasons, phys["status"] == "degraded"))

    def update_activation(self, t_us: float | None, norm: dict) -> None:
        usable = norm["status"] in USABLE and norm["value"] is not None
        value = max(norm["value"].values()) if usable else None
        self._update(self._act, t_us, _Input(t_us or 0.0, usable, value, norm["status"],
                                             tuple(norm["reasons"]), norm["status"] == "degraded"))

    def _update(self, track: _Track, t_us: float | None, inp: _Input) -> None:
        if t_us is None or not track.add(t_us, inp):
            return
        if track not in self._tracks:
            return  # activation ignored: tracked for display only
        if self.cursor is None or (not self._finalised_any and t_us < self.cursor):
            # Until anything is finalised, the start is the earliest first update of any
            # input, whichever stream happens to be delivered first.
            self.cursor = t_us
        self.newest = t_us if self.newest is None else max(self.newest, t_us)
        bound = min(tr.determined_until(self.newest) for tr in self._tracks)
        self._finalise(bound, uncovered="stale")

    # -- finalisation ------------------------------------------------------------------

    def _finalise(self, until: float, uncovered: str) -> None:
        if self.cursor is None or until <= self.cursor:
            return
        pts = sorted({self.cursor, until, *(p for tr in self._tracks
                                           for p in tr.boundaries(self.cursor, until))})
        for a, b in zip(pts, pts[1:]):
            dur = (b - a) / 1e6
            blocks, inputs = [], {}
            for tr in self._tracks:
                key, inp = tr.block_at(b, uncovered)
                if key:
                    blocks.append(key)
                inputs[tr.name] = inp
            if blocks:
                key = " & ".join(sorted(blocks))
                self.excluded_s += dur
                self.excluded_by_reason[key] = self.excluded_by_reason.get(key, 0.0) + dur
                continue
            v = inputs["vibration"]
            a_in = inputs.get("activation")
            self.cumulative += self.rate_for(v.value, a_in.value if a_in else 0.0) * dur / 3600.0  # type: ignore[union-attr]
            self.valid_s += dur
            if v.degraded or (a_in is not None and a_in.degraded):  # type: ignore[union-attr]
                self.degraded_s += dur
        self.cursor = until
        self._finalised_any = True
        for tr in self._tracks:
            tr.prune(until)

    def pending_segments(self) -> int:
        return sum(len(tr.segs) for tr in self._tracks)

    # -- formula ------------------------------------------------------------------------

    def rate_for(self, v_ms2: float, activation: float | None) -> float:
        c = self.cfg
        a = min(c.activation_cap, max(0.0, activation or 0.0))
        return c.k_per_hour * (v_ms2 / c.v_ref) ** c.exponent * (1.0 + c.activation_weight * a)

    # -- output -----------------------------------------------------------------------

    def _current_blocks(self, now: float | None) -> list[str]:
        blocks = []
        for tr in self._tracks:
            if tr.latest is None:
                blocks.append(f"{tr.name}_no_data")
            elif not tr.latest.usable:
                blocks.append(_block_key(tr.name, tr.latest))
            elif now is not None and now - tr.last_t > tr.fresh_us:  # type: ignore[operator]
                blocks.append(f"{tr.name}_stale")
        return blocks

    def snapshot(self, t_us: float | None) -> dict:
        now = t_us if t_us is not None else self.newest
        reasons = self._current_blocks(now)
        valid = not reasons
        rate = None
        if valid:
            act = self._act.latest.value if self.cfg.activation_mode == "required" else 0.0  # type: ignore[union-attr]
            rate = self.rate_for(self._vib.latest.value, act)  # type: ignore[union-attr]
        r = lambda x: round(x, 6)  # noqa: E731

        def inp(tr: _Track, units: str) -> dict:
            i = tr.latest
            if i is None:
                return {"status": "unavailable", "value": None, "units": units, "t_us": None, "age_s": None}
            return {"status": i.status, "value": i.value, "units": units, "t_us": round(i.t_us),
                    "age_s": None if now is None else r(max(0.0, (now - i.t_us) / 1e6)),
                    "fresh_limit_s": tr.fresh_us / 1e6}

        return {
            "label": LABEL, "label_note": LABEL_NOTE,
            "status": "accumulating" if valid else "paused",
            "reasons": reasons,
            "rate": {"value": None if rate is None else r(rate), "units": "au/h"},
            "cumulative": {"value": r(self.cumulative), "units": "au"},
            "accumulation": self.accumulation,
            "boot_segments": self.boot_segments,
            "integrated_until_us": None if self.cursor is None else round(self.cursor),
            "pending_s": None if self.cursor is None or self.newest is None
            else r((self.newest - self.cursor) / 1e6),
            "valid_duration_s": r(self.valid_s),
            "degraded_duration_s": r(self.degraded_s),
            "excluded_duration_s": r(self.excluded_s),
            "excluded_by_reason": {k: r(v) for k, v in sorted(self.excluded_by_reason.items())},
            "clock": "device_time (recording time for replay; independent of wall-clock pacing)",
            "inputs": {
                "vibration": inp(self._vib, "m/s^2"),
                "activation": (inp(self._act, "normalized") if self.cfg.activation_mode == "required"
                               else {"status": "ignored", "value": None, "note": "activation_mode=ignored"}),
            },
            "formula": {"expression": EXPRESSION, "coefficients": asdict(self.cfg)},
        }
