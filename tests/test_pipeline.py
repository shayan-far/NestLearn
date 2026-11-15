import numpy as np

from backend.signals.estimator import Estimator
from backend.signals.features import CHANNELS, extract
from backend.signals.source import SimulatedSource

FS, WIN, CHUNK = 250, 500, 125  # 2 s window, 0.5 s step


class Rig:
    """Drives the simulated source through the real feature + estimator code on a simulated clock."""

    def __init__(self, seed=1):
        self.src = SimulatedSource(fs=FS, seed=seed)
        self.est = Estimator()
        self.buf = np.zeros((len(CHANNELS), 0))
        self.t = 0.0

    def step(self):
        self.buf = np.concatenate([self.buf, self.src.generate(CHUNK)], axis=1)[:, -WIN:]
        self.t += 0.5
        return extract(self.buf, FS)

    def calibrate(self, secs=10):
        for _ in range(int(secs * 2)):
            f = self.step()
            if self.buf.shape[1] >= WIN:
                self.est.add_baseline(f)
        self.est.finish_calibration()

    def run(self, state, secs):
        self.src.set_state(state)
        return [self.est.update(self.step(), self.t) for _ in range(int(secs * 2))]


def test_neutral_is_not_confused():
    r = Rig()
    r.calibrate()
    outs = r.run("neutral", 8)
    assert not any(o["state"] == "confused" for o in outs)


def test_confusion_detected():
    r = Rig()
    r.calibrate()
    r.run("neutral", 4)
    outs = r.run("confused", 8)
    assert any(o["state"] == "confused" for o in outs[-4:])
    assert outs[-1]["confidence"] > 0.65


def test_works_across_simulated_people():
    for seed in (2, 3, 4):
        r = Rig(seed)
        r.calibrate()
        assert r.run("confused", 8)[-1]["state"] == "confused"


def test_aha_after_confusion():
    r = Rig()
    r.calibrate()
    r.run("confused", 6)
    outs = r.run("aha", 6)
    assert any(o["state"] == "aha" for o in outs)


def test_bored_detected():
    r = Rig()
    r.calibrate()
    outs = r.run("bored", 8)
    assert outs[-1]["state"] == "bored"


def test_behavior_evidence_raises_confusion():
    r = Rig()
    r.calibrate()
    r.run("neutral", 4)
    base = r.run("neutral", 2)[-1]["confidence"]
    for _ in range(3):
        r.est.add_evidence("quiz_wrong", r.t)
    assert r.run("neutral", 1)[-1]["confidence"] > base
