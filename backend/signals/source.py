"""EEG sources. Everything downstream only sees EEGSource.read() -> (n_channels, n_samples) in uV.

SimulatedSource synthesizes raw-looking EEG (band-limited sinusoids + noise) whose band amplitudes
follow a latent state, so the SAME feature/estimator code runs in simulation and on hardware.
"""
import time
from abc import ABC, abstractmethod

import numpy as np

from backend import config
from backend.signals.features import BANDS, CHANNELS, GROUPS


class EEGSource(ABC):
    fs: int = 250

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def read(self) -> np.ndarray:
        """New samples since the previous call, shape (len(CHANNELS), n)."""

    def stop(self) -> None:
        pass


REGION_OF = {i: g for g, idxs in GROUPS.items() for i in idxs}
BASE_AMP = {"theta": 6.0, "alpha": 10.0, "beta": 4.0, "gamma": 1.5}
KEYS = [(g, b) for g in GROUPS for b in BANDS]

# Band-amplitude multipliers per latent state, keyed by (region, band). Missing = 1.0.
STATE_MULT = {
    "neutral": {},
    "confused": {("frontal", "theta"): 2.2, ("parietal", "alpha"): 0.5,
                 ("fp", "gamma"): 2.5, ("frontal", "beta"): 1.2},
    "bored": {("parietal", "alpha"): 2.0, ("frontal", "beta"): 0.6, ("frontal", "theta"): 0.8},
    # "aha" is two phases: a parietal alpha burst, then right-temporal gamma
    "aha_alpha": {("parietal", "alpha"): 2.0},
    "aha_gamma": {("temporal", "gamma"): 4.0},
}


class SimulatedSource(EEGSource):
    def __init__(self, fs: int = 250, seed=None, noise: float = 2.0):
        self.fs, self.noise = fs, noise
        self.rng = np.random.default_rng(seed)
        self.state = "neutral"
        self._i = 0            # samples generated so far (our simulated clock)
        self._aha_t0 = 0.0
        self._last = None      # wall-clock time of last read()
        self.mult = {k: 1.0 for k in KEYS}
        nch = len(CHANNELS)
        # Each "person" has their own overall amplitude -> calibration actually matters.
        self.scale = self.rng.uniform(0.7, 1.4) * self.rng.uniform(0.9, 1.1, nch)
        self.tones = {
            (c, b): (self.rng.uniform(lo + 0.5, hi - 0.5, 2), self.rng.uniform(0, 2 * np.pi, 2))
            for c in range(nch) for b, (lo, hi) in BANDS.items()
        }

    def start(self) -> None:
        self._last = time.time()

    def set_state(self, state: str) -> None:
        if state == "aha":
            self._aha_t0 = self._i / self.fs
        self.state = state

    def _key(self) -> str:
        if self.state == "aha":
            dt = self._i / self.fs - self._aha_t0
            if dt < 1.5:
                return "aha_alpha"
            if dt < 3.5:
                return "aha_gamma"
            self.state = "neutral"
        return self.state

    def read(self) -> np.ndarray:
        now = time.time()
        if self._last is None:
            self._last = now
        n = int((now - self._last) * self.fs)
        if n <= 0:
            return np.zeros((len(CHANNELS), 0))
        self._last += n / self.fs
        return self.generate(n)

    def generate(self, n: int) -> np.ndarray:
        """Produce n samples at the current latent state (deterministic clock; used by tests too)."""
        target = STATE_MULT[self._key()]
        a = 1 - np.exp(-n / self.fs / 0.6)  # ease amplitudes toward target, tau ~0.6 s
        for k in KEYS:
            self.mult[k] += a * (target.get(k, 1.0) - self.mult[k])
        t = (np.arange(n) + self._i) / self.fs
        self._i += n
        out = np.empty((len(CHANNELS), n))
        for c in range(len(CHANNELS)):
            sig = self.rng.normal(0, self.noise, n)
            region = REGION_OF[c]
            for band in BANDS:
                f, ph = self.tones[(c, band)]
                amp = BASE_AMP[band] * self.scale[c] * self.mult[(region, band)] * 0.7
                sig += amp * (np.sin(2 * np.pi * f[0] * t + ph[0]) + np.sin(2 * np.pi * f[1] * t + ph[1]))
            out[c] = sig
        return out


class SimDriver:
    """Makes the simulated student behave plausibly: harder segments are more likely to cause
    confusion, accepting an intervention usually leads to an 'aha'. UI buttons can override."""

    def __init__(self, source: SimulatedSource, seed=None):
        self.src = source
        self.rng = np.random.default_rng(seed)
        self.confuse_at = None
        self.resolve_at = None
        self.confused = False
        self.manual_until = 0.0

    def force_neutral(self) -> None:
        self.src.set_state("neutral")
        self.confuse_at = self.resolve_at = None
        self.confused = False

    def on_segment(self, difficulty: float, now: float) -> None:
        self.force_neutral()
        if self.rng.random() < difficulty:
            self.confuse_at = now + self.rng.uniform(3, 6)

    def on_intervention_accepted(self, now: float) -> None:
        if self.confused and self.rng.random() < 0.7:
            self.resolve_at = now + self.rng.uniform(4, 7)

    def manual(self, state: str, now: float) -> None:
        self.src.set_state(state)
        self.confused = state == "confused"
        self.confuse_at = None            # a forced state replaces whatever the script had planned
        self.manual_until = now + 10

    def tick(self, now: float) -> None:
        if now < self.manual_until:
            return
        if self.manual_until:
            # a forced state lasts 10 s, then the simulated student goes back to neutral
            self.manual_until = 0.0
            if self.src.state in ("confused", "bored"):
                self.src.set_state("neutral")
                self.confused = False
        if self.confuse_at and now >= self.confuse_at and not self.confused:
            self.src.set_state("confused")
            self.confused = True
            self.confuse_at = None
        if self.resolve_at and now >= self.resolve_at:
            self.src.set_state("aha")
            self.confused = False
            self.resolve_at = None


class BrainFlowSource(EEGSource):
    """Generic BrainFlow adapter. Works for any board BrainFlow supports; board_id -1 is its synthetic board.
    If your hardware has its own driver instead, copy this class and implement start/read/stop."""

    def __init__(self, board_id: int, serial_port: str = ""):
        self.board_id, self.serial_port = board_id, serial_port
        self.board = None

    def start(self) -> None:
        from brainflow.board_shim import BoardShim, BrainFlowInputParams

        params = BrainFlowInputParams()
        params.serial_port = self.serial_port
        self.board = BoardShim(self.board_id, params)
        self.fs = BoardShim.get_sampling_rate(self.board_id)
        self.eeg_idx = BoardShim.get_eeg_channels(self.board_id)[: len(CHANNELS)]
        self.board.prepare_session()
        self.board.start_stream()

    def read(self) -> np.ndarray:
        data = self.board.get_board_data()  # everything since last call
        return np.asarray(data[self.eeg_idx], dtype=float)

    def stop(self) -> None:
        if self.board:
            self.board.stop_stream()
            self.board.release_session()


def make_source(seed=None) -> EEGSource:
    if config.SOURCE == "brainflow":
        return BrainFlowSource(config.BRAINFLOW_BOARD_ID, config.SERIAL_PORT)
    return SimulatedSource(seed=seed)
