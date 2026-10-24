"""Independent, label-free EEG features; no changes to YASA's input branch."""

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.signal import welch

FEATURE_NAMES = (
    "log_rms_uv",
    "log_std_uv",
    "hjorth_mobility",
    "hjorth_complexity",
    "relative_delta",
    "relative_theta",
    "relative_alpha",
    "relative_sigma",
    "relative_beta",
)
FEATURE_VERSION = "eeg-welch-hjorth-v1"


def eeg_features(values_v: ArrayLike, sfreq: float) -> NDArray[np.float64]:
    x = np.asarray(values_v, dtype=float) * 1e6
    if not np.isfinite(sfreq) or sfreq < 64:
        raise ValueError("EEG feature extraction requires a native sampling rate >=64 Hz")
    if x.ndim != 1 or len(x) != round(30 * sfreq) or not np.isfinite(x).all():
        raise ValueError("Features require one complete, finite 30-second EEG epoch")
    variance = float(np.var(x))
    d1, d2 = np.diff(x), np.diff(x, n=2)
    mobility = np.sqrt(np.var(d1) / variance) if variance > 0 else 0.0
    complexity = (
        np.sqrt(np.var(d2) / np.var(d1)) / mobility if mobility > 0 and np.var(d1) > 0 else 0.0
    )
    frequencies, psd = welch(x, fs=sfreq, nperseg=round(4 * sfreq), noverlap=round(2 * sfreq))
    bands = [(0.5, 4), (4, 8), (8, 12), (12, 16), (16, 30)]
    power = np.array(
        [
            np.trapezoid(
                psd[(frequencies >= lo) & (frequencies <= hi)],
                frequencies[(frequencies >= lo) & (frequencies <= hi)],
            )
            for lo, hi in bands
        ]
    )
    relative = power / power.sum() if power.sum() > 0 else np.zeros(5)
    output = np.r_[
        np.log10(max(float(np.sqrt(np.mean(x**2))), 1e-12)),
        np.log10(max(float(np.std(x)), 1e-12)),
        mobility,
        complexity,
        relative,
    ]
    if not np.isfinite(output).all():
        raise ValueError("Feature calculation produced nonfinite values")
    return output
