"""Rule-based state estimator. No training data needed: everything is z-scored against a short
per-user calibration baseline, so thresholds transfer between people (and between sim and hardware).

Confusion score = frontal theta up + parietal alpha down + forehead EMG up (+ decaying behavioral evidence).
"""
import math
from collections import deque

import numpy as np

FEATURES = ["theta_f", "alpha_p", "eng", "emg_fp", "gamma_t"]
EVIDENCE_W = {"scrollback": 0.8, "long_pause": 0.5, "quiz_wrong": 1.0, "quiz_slow": 0.4}


class Estimator:
    def __init__(self, threshold: float = 0.65):
        self.threshold = threshold
        self.base: list = []
        self.mu = None
        self.sd = None
        self.ema = 0.0
        self.evidence: list = []          # (timestamp, weight)
        self.alpha_hist = deque(maxlen=6)  # last ~3 s of parietal alpha z-scores (for aha detection)
        self.last_aha = -1e9

    # --- calibration -------------------------------------------------------------------------
    def add_baseline(self, feats: dict) -> None:
        self.base.append([feats[k] for k in FEATURES])

    def finish_calibration(self) -> None:
        a = np.array(self.base)
        self.mu = a.mean(axis=0)
        self.sd = np.maximum(a.std(axis=0), 0.08)  # floor so tiny baseline variance can't explode z-scores

    # --- behavioral evidence (fused with EEG) --------------------------------------------------
    def add_evidence(self, kind: str, now: float) -> None:
        if kind in EVIDENCE_W:
            self.evidence.append((now, EVIDENCE_W[kind]))

    # --- main update ----------------------------------------------------------------------------
    def update(self, feats: dict, now: float) -> dict:
        v = np.array([feats[k] for k in FEATURES])
        z = dict(zip(FEATURES, map(float, np.clip((v - self.mu) / self.sd, -4, 4))))

        raw = 0.55 * z["theta_f"] - 0.45 * z["alpha_p"] + 0.2 * z["emg_fp"]
        self.evidence = [(t, w) for t, w in self.evidence if now - t < 40]
        ev = sum(w * 0.5 ** ((now - t) / 10) for t, w in self.evidence)  # 10 s half-life
        self.ema = 0.6 * self.ema + 0.4 * (raw + ev)
        conf = 1 / (1 + math.exp(-1.6 * (self.ema - 1.0)))

        self.alpha_hist.append(z["alpha_p"])
        state = "engaged"
        if z["gamma_t"] > 2.0 and max(self.alpha_hist) > 1.5 and now - self.last_aha > 10:
            state = "aha"          # alpha burst followed by right-temporal gamma
            self.last_aha = now
        elif conf >= self.threshold:
            state = "confused"
        elif z["eng"] < -1.0 and z["alpha_p"] > 1.0:
            state = "bored"        # low engagement index + high alpha
        return {"state": state, "confidence": round(conf, 3), "z": z, "ts": now}
