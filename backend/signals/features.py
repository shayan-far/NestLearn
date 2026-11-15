"""Feature extraction: raw EEG window -> a handful of interpretable, log-scaled features."""
import numpy as np

# Row order of every window. Map your real electrodes to this order (or edit names + GROUPS together).
CHANNELS = ["Fp1", "Fp2", "Fz", "FCz", "Pz", "P3", "P4", "T8"]
GROUPS = {"fp": [0, 1], "frontal": [2, 3], "parietal": [4, 5, 6], "temporal": [7]}
BANDS = {"theta": (4, 8), "alpha": (8, 12), "beta": (13, 30), "gamma": (30, 45)}
ARTIFACT_PTP_UV = 300.0  # peak-to-peak above this in any channel => window rejected


def band_powers(window: np.ndarray, fs: int) -> dict:
    """Per-channel band power via a Hann-windowed FFT. Only 4-45 Hz bins are used,
    which also keeps 50/60 Hz mains noise out without a separate notch filter."""
    x = window - window.mean(axis=1, keepdims=True)
    x = x * np.hanning(x.shape[1])
    power = np.abs(np.fft.rfft(x, axis=1)) ** 2
    freqs = np.fft.rfftfreq(x.shape[1], 1.0 / fs)
    return {
        name: power[:, (freqs >= lo) & (freqs < hi)].mean(axis=1)
        for name, (lo, hi) in BANDS.items()
    }


def is_artifact(window: np.ndarray) -> bool:
    return bool(np.ptp(window, axis=1).max() > ARTIFACT_PTP_UV)


def extract(window: np.ndarray, fs: int) -> dict:
    bp = band_powers(window, fs)

    def p(band: str, group: str) -> float:
        return float(bp[band][GROUPS[group]].mean()) + 1e-12

    theta_f = p("theta", "frontal")   # frontal-midline theta: load / conflict
    alpha_p = p("alpha", "parietal")  # parietal alpha: falls with effort, rises with mind-wandering
    beta_f = p("beta", "frontal")
    return {
        "theta_f": float(np.log10(theta_f)),
        "alpha_p": float(np.log10(alpha_p)),
        "eng": float(np.log10(beta_f / (alpha_p + theta_f))),  # engagement index beta/(alpha+theta)
        "emg_fp": float(np.log10(p("gamma", "fp"))),           # forehead high-freq power ~ brow-furrow EMG
        "gamma_t": float(np.log10(p("gamma", "temporal"))),    # right temporal gamma: insight proxy
    }
