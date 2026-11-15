"""Adaptation policy: when sustained confusion is detected, offer the next-best alternative explanation."""
from typing import Optional

LADDER = ["rephrase", "analogy", "example", "diagram", "quiz"]


class AdaptationPolicy:
    def __init__(self, threshold: float = 0.65, sustain_s: float = 4.0, cooldown_s: float = 25.0):
        self.threshold, self.sustain_s, self.cooldown_s = threshold, sustain_s, cooldown_s
        self.above_since: Optional[float] = None
        self.last_fire = -1e9
        self.tried: dict = {}                    # segment_id -> modalities already offered
        self.pref = {m: 0.0 for m in LADDER}     # learned per-student preference
        self.last: Optional[tuple] = None        # (modality, ts) of most recent intervention

    def update(self, out: dict, seg_id, available: list, now: float) -> Optional[str]:
        # credit assignment: an aha shortly after an intervention rewards that modality
        if self.last and out["state"] == "aha" and now - self.last[1] < 20:
            self.pref[self.last[0]] += 1.0
            self.last = None

        confused = out["state"] == "confused" and out["confidence"] >= self.threshold
        if confused:
            self.above_since = self.above_since or now
        else:
            self.above_since = None

        if (not seg_id or not confused
                or now - self.above_since < self.sustain_s
                or now - self.last_fire < self.cooldown_s):
            return None
        return self._pick(seg_id, available, now)

    def force(self, seg_id, available: list, now: float) -> Optional[str]:
        """Student pressed 'explain differently'."""
        return self._pick(seg_id, available, now)

    def rejected(self) -> None:
        if self.last:
            self.pref[self.last[0]] -= 0.25

    def _pick(self, seg_id, available: list, now: float) -> Optional[str]:
        tried = self.tried.setdefault(seg_id, [])
        options = [m for m in available if m not in tried]
        if not options:
            return None
        # prefer what has worked for this student; tie-break by escalation order
        m = max(options, key=lambda x: (self.pref[x], -LADDER.index(x)))
        tried.append(m)
        self.last_fire, self.last, self.above_since = now, (m, now), None
        return m
