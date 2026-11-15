"""NestTrace: a tiered, bounded-error session log for brain-state data.

The estimator produces a state + confidence + 5 z-scored features every 0.5 s (and the headset produces
thousands of raw samples per second). Logging all of that is wasteful; logging only discrete events loses
the shape of the session. NestTrace keeps both, in four tiers, in one append-only JSONL file per session:

  Tier 1  "ev" / "st"   Every discrete event and every *debounced* state transition, with a full feature
                        snapshot. Never dropped.
  Tier 2  "ep"          Confusion *episodes*: onset -> (relapses merged) -> outcome (aha / faded / moved_on),
                        with peak confidence, mean feature vector and which interventions were offered.
  Tier 3  "seg"         One summary per segment visit: dwell time, share of time in each state, counts.
  Tier 4  "kf"          Keyframes of the continuous signal (confidence + 5 features), compressed with a strict
                        multi-channel swinging-door filter: linear interpolation between consecutive keyframes
                        is guaranteed to be within a per-channel tolerance of every tick that was dropped.

A per-session byte budget drives graceful degradation: as the file fills, keyframe tolerances widen
(x2, x4, x8) and finally keyframes stop. Tiers 1-3 are always kept, so the instructor view never loses an
event, only resolution of the curve. See docs/NESTTRACE.md.
"""
import json
import math
from pathlib import Path
from typing import Optional

FEATURES = ["theta_f", "alpha_p", "eng", "emg_fp", "gamma_t"]
CHANNELS = ["conf"] + FEATURES
BASE_TOL = [0.04, 1.0, 1.0, 1.0, 1.0, 1.2]      # per channel: confidence (0-1), then z-score units
DECISION = 0.65           # confusion threshold: confidence tolerance is tightest here (decision-aware)
FEATURE_EMA = 0.35        # keyframes store the smoothed feature trend; transitions keep exact raw snapshots
DEBOUNCE_S = {"bored": 3.0}   # a state must persist this long to count (default 1 s; aha is instant)
GRACE_S = 15.0            # confusion that returns within this window is the same episode (a relapse)
AHA_LINK_S = 20.0         # an aha this soon after an intervention is credited to it
HEARTBEAT_S = 30.0        # force a keyframe at least this often so live views stay fresh
PRESSURE_STEPS = [(0.95, None), (0.85, 8), (0.70, 4), (0.50, 2)]   # (budget fraction, tolerance multiplier)


class SwingingDoor:
    """Strict multi-channel swinging-door compression.

    Keeps an anchor (last emitted point) and, per channel, the range of slopes [lo, up] that a line from
    the anchor may take while staying within tolerance of every point seen since. A new point is accepted
    if its own slope from the anchor fits inside every channel's range; otherwise the previous point is
    emitted and becomes the new anchor. Because each emitted point's slope was inside the doors of all the
    points before it, interpolating between emitted points reproduces every dropped point within tolerance.
    """

    def __init__(self, n: int):
        self.n = n
        self.anchor = None
        self.prev = None
        self._open()

    def _open(self):
        self.up = [math.inf] * self.n
        self.lo = [-math.inf] * self.n

    def reset(self, point) -> None:
        self.anchor = self.prev = point
        self._open()

    def _narrow(self, t, v, tol):
        ta, va = self.anchor
        dt = t - ta
        for i in range(self.n):
            self.up[i] = min(self.up[i], (v[i] + tol[i] - va[i]) / dt)
            self.lo[i] = max(self.lo[i], (v[i] - tol[i] - va[i]) / dt)

    def push(self, t: float, v: list, tol: list):
        """Returns a point (t, v) to store, or None."""
        if self.anchor is None:
            self.reset((t, v))
            return (t, v)
        ta, va = self.anchor
        dt = t - ta
        if dt <= 0:
            return None
        fits = all(self.lo[i] <= (v[i] - va[i]) / dt <= self.up[i] for i in range(self.n))
        if fits:
            self._narrow(t, v, tol)
            self.prev = (t, v)
            return None
        emit = self.prev
        self.anchor = emit
        self._open()
        self._narrow(t, v, tol)
        self.prev = (t, v)
        return emit

    def pending(self):
        """The last accepted point if it hasn't been stored yet (flush on close)."""
        return self.prev if self.prev is not self.anchor else None


def reconstruct(points: list, t: float) -> Optional[list]:
    """Linear interpolation over stored (t, values) points. Used by tests and the instructor timeline."""
    if not points:
        return None
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            a = 0 if t1 == t0 else (t - t0) / (t1 - t0)
            return [x0 + a * (x1 - x0) for x0, x1 in zip(v0, v1)]
    return list(points[-1][1]) if t >= points[-1][0] else list(points[0][1])


class SessionTrace:
    def __init__(self, path: Path, header: dict, budget: int, t0: float):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a", encoding="utf-8")
        self.t0, self.budget = t0, max(budget, 10_000)
        self.bytes = 0
        self.stats = {"ticks": 0, "keyframes": 0, "transitions": 0, "events": 0, "episodes": 0}
        self.sdt = SwingingDoor(len(CHANNELS))
        self.tol_mult: Optional[float] = 1.0       # None => keyframes disabled (budget nearly spent)
        self.last_kf = None
        self.listeners = []                       # callables(record) - the live debug pane subscribes here
        # debounced state machine
        self.state, self.state_since = "engaged", t0
        self.cand, self.cand_since = None, None
        self.last_tick = None
        self.last_out = None
        self.ema = None
        # segment accounting
        self.seg, self.seg_enter = None, None
        self.seg_time: dict = {}
        self.seg_counts: dict = {}
        # episodes
        self.ep: Optional[dict] = None             # currently confused
        self.pending: Optional[dict] = None        # confusion ended, waiting to see how it resolves
        self.last_offer = None                     # (modality, ts)
        self.closed = False
        self._write({"k": "hdr", "v": 1, "t0": round(t0, 3), "budget": self.budget,
                     "channels": CHANNELS, "tol": BASE_TOL, **header})

    # ---- low level --------------------------------------------------------------------------------
    def rel(self, now: float) -> float:
        return round(now - self.t0, 2)

    def _write(self, rec: dict) -> None:
        if self.closed:
            return
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        self.f.write(line)
        self.f.flush()
        self.bytes += len(line.encode())
        self._pressure()
        for fn in self.listeners:
            try:
                fn(rec)
            except Exception:
                pass

    def _pressure(self) -> None:
        frac = self.bytes / self.budget
        mult = 1.0
        for threshold, m in PRESSURE_STEPS:
            if frac >= threshold:
                mult = m
                break
        if mult != self.tol_mult and self.tol_mult is not None:
            self.tol_mult = mult
            # announce so readers know the curve resolution changed here (written directly, no recursion)
            note = json.dumps({"k": "budget", "frac": round(frac, 3), "tol_mult": mult}, separators=(",", ":")) + "\n"
            self.f.write(note)
            self.bytes += len(note.encode())

    def _tol(self, conf: float):
        """Decision-aware tolerance: the confidence channel is kept tight near the 0.65 decision threshold
        (where a small error would flip 'confused' vs 'not') and relaxed up to 3x far from it."""
        far = min(1.0, abs(conf - DECISION) / 0.4)
        t = [b * self.tol_mult for b in BASE_TOL]
        t[0] *= 1 + 2 * far
        return t

    @staticmethod
    def _vec(out: dict) -> list:
        z = out.get("z") or {}
        return [round(out.get("confidence", 0.0), 3)] + [round(float(z.get(k, 0.0)), 2) for k in FEATURES]

    def _kf(self, point) -> None:
        t, v = point
        self.stats["keyframes"] += 1
        self.last_kf = t
        self._write({"k": "kf", "ts": t, "v": v})

    # ---- tier 1: events -----------------------------------------------------------------------------
    def event(self, now: float, ev: str, **payload) -> None:
        kind = ev
        if self.bytes > self.budget * 1.2 and kind == "behavior":
            return  # hard ceiling: drop only low-value behavior pings
        self.stats["events"] += 1
        self.seg_counts[kind] = self.seg_counts.get(kind, 0) + 1
        self._write({"k": "ev", "ts": self.rel(now), "seg": self.seg, **payload, "kind": kind})

    def segment(self, now: float, seg_id: str) -> None:
        self._close_segment(now)
        if self.ep:
            self._end_episode(now)
            self._finalize(self.pending, "moved_on")
        elif self.pending:
            self._finalize(self.pending, "faded")
        self.seg, self.seg_enter, self.seg_time, self.seg_counts = seg_id, now, {}, {}
        self.event(now, "segment_enter")

    def intervention(self, now: float, modality: str, auto: bool, source: str = "") -> None:
        self.last_offer = (modality, now)
        self.event(now, "intervention", modality=modality, auto=auto, source=source)
        target = self.ep or self.pending
        if target is not None:
            target["offers"].append({"m": modality, "auto": auto, "at": self.rel(now)})

    def intervention_response(self, now: float, accepted: bool) -> None:
        self.event(now, "intervention_response", accepted=accepted)
        target = self.ep or self.pending
        if target is not None and target["offers"]:
            target["offers"][-1]["acc"] = accepted

    # ---- the 2 Hz state stream ----------------------------------------------------------------------
    def tick(self, now: float, out: dict) -> list:
        """Feed one estimator output. Returns debounced transitions as [(state, ts)]."""
        self.stats["ticks"] += 1
        t, raw_v = self.rel(now), self._vec(out)
        self.last_out = out
        if self.ema is None:
            self.ema = raw_v[1:]
        else:
            self.ema = [a + FEATURE_EMA * (b - a) for a, b in zip(self.ema, raw_v[1:])]
        v = [raw_v[0]] + [round(x, 2) for x in self.ema]
        if self.last_tick is not None:
            self.seg_time[self.state] = self.seg_time.get(self.state, 0.0) + (now - self.last_tick)
        self.last_tick = now

        # tier 4: keyframes
        if self.tol_mult is not None:
            p = self.sdt.push(t, v, self._tol(v[0]))
            if p:
                self._kf(p)
            elif self.last_kf is not None and t - self.last_kf >= HEARTBEAT_S:
                self._kf((t, v))
                self.sdt.reset((t, v))

        # episode statistics while confused
        if self.ep is not None:
            self.ep["peak"] = max(self.ep["peak"], raw_v[0])
            self.ep["zsum"] = [a + b for a, b in zip(self.ep["zsum"], raw_v[1:])]
            self.ep["n"] += 1

        # debounced state machine
        made = []
        raw = out.get("state", "engaged")
        if raw == "aha":
            self._transition(now, "aha", out)
            made.append(("aha", now))
            self.cand = None
        elif raw != self.state and raw != "artifact":
            if self.cand != raw:
                self.cand, self.cand_since = raw, now
            elif now - self.cand_since >= DEBOUNCE_S.get(raw, 1.0):
                self._transition(self.cand_since, raw, out)
                made.append((raw, self.cand_since))
                self.cand = None
        else:
            self.cand = None

        if self.pending and now - self.pending["end_abs"] > GRACE_S:
            self._finalize(self.pending, "faded")
        return made

    def _transition(self, ts: float, to: str, out: dict) -> None:
        frm = self.state
        t = self.rel(ts)
        v = self._vec(out)
        self.stats["transitions"] += 1
        self._write({"k": "st", "ts": t, "seg": self.seg, "from": frm, "to": to,
                     "dwell": round(ts - self.state_since, 2), "v": v})
        if self.tol_mult is not None and self.ema is not None:
            # the transition doubles as an exact keyframe of the (smoothed) curve
            self.sdt.reset((t, [v[0]] + [round(x, 2) for x in self.ema]))
            self.last_kf = t

        if frm == "confused" and to != "confused":
            self._end_episode(ts)
        if to == "confused":
            self.event(ts, "confusion_onset", confidence=v[0])
            if self.pending and ts - self.pending["end_abs"] <= GRACE_S and self.pending["seg"] == self.seg:
                self.ep, self.pending = self.pending, None   # relapse: reopen the same episode
                self.ep["relapses"] += 1
            else:
                if self.pending:
                    self._finalize(self.pending, "faded")
                self.ep = {"seg": self.seg, "start_abs": ts, "end_abs": None, "peak": v[0],
                           "zsum": [0.0] * len(FEATURES), "n": 0, "relapses": 0, "offers": []}
        elif to == "aha":
            after = None
            if self.last_offer and ts - self.last_offer[1] < AHA_LINK_S:
                after = self.last_offer[0]
            self.event(ts, "aha", after=after)
            if self.ep:
                self._end_episode(ts)
            if self.pending:
                self._finalize(self.pending, "aha")
            self.state, self.state_since = "engaged", ts   # aha is a moment, not a state you stay in
            return
        elif to == "bored":
            self.event(ts, "bored_onset")
        self.state, self.state_since = to, ts

    def _end_episode(self, ts: float) -> None:
        if self.ep is None:
            return
        self.ep["end_abs"] = ts
        self.pending, self.ep = self.ep, None

    def _finalize(self, ep: Optional[dict], outcome: str) -> None:
        if ep is None:
            return
        if ep is self.pending:
            self.pending = None
        end = ep["end_abs"] if ep["end_abs"] is not None else ep["start_abs"]
        helped = next((o["m"] for o in reversed(ep["offers"]) if o.get("acc")), None)
        n = max(ep["n"], 1)
        self.stats["episodes"] += 1
        self._write({"k": "ep", "kind": "confusion", "seg": ep["seg"],
                     "start": self.rel(ep["start_abs"]), "end": self.rel(end),
                     "dur": round(end - ep["start_abs"], 2), "peak": round(ep["peak"], 3),
                     "mean_z": [round(x / n, 2) for x in ep["zsum"]], "relapses": ep["relapses"],
                     "offers": ep["offers"], "outcome": outcome,
                     "helped_by": helped if outcome == "aha" else None})

    def _close_segment(self, now: float) -> None:
        if self.seg is None:
            return
        if self.last_tick is not None and now > self.last_tick:
            self.seg_time[self.state] = self.seg_time.get(self.state, 0.0) + (now - self.last_tick)
            self.last_tick = now
        dwell = now - self.seg_enter
        total = sum(self.seg_time.values()) or 1.0
        self._write({"k": "seg", "seg": self.seg, "enter": self.rel(self.seg_enter), "exit": self.rel(now),
                     "dwell": round(dwell, 2),
                     "share": {k: round(s / total, 3) for k, s in self.seg_time.items()},
                     "counts": self.seg_counts})

    def close(self, now: float, reason: str = "disconnect") -> None:
        if self.closed:
            return
        if self.tol_mult is not None:
            p = self.sdt.pending()
            if p:
                self._kf(p)
        if self.ep:
            self._end_episode(now)
        self._finalize(self.pending, "session_end")
        self._close_segment(now)
        ratio = round(self.stats["ticks"] / max(self.stats["keyframes"], 1), 1)
        self._write({"k": "ftr", "ts": self.rel(now), "reason": reason, **self.stats,
                     "bytes": self.bytes, "tol_mult": self.tol_mult, "kf_ratio": ratio})
        self.closed = True
        self.f.close()

    def summary(self) -> dict:
        return {**self.stats, "bytes": self.bytes, "budget": self.budget, "tol_mult": self.tol_mult,
                "kf_ratio": round(self.stats["ticks"] / max(self.stats["keyframes"], 1), 1)}


def session_path(log_dir: Path, lesson_id: str, session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return Path(log_dir) / lesson_id / f"{safe}.jsonl"


