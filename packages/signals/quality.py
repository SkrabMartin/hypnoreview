"""Descriptive per-channel QC; thresholds are heuristics, not clinical claims."""

from dataclasses import dataclass
from math import isfinite

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class QualityConfig:
    flat_ptp_uv: float = 0.5
    extreme_abs_uv: float = 500.0

    def __post_init__(self) -> None:
        if not all(isfinite(v) and v > 0 for v in (self.flat_ptp_uv, self.extreme_abs_uv)):
            raise ValueError("QC thresholds must be finite and positive")


def signal_quality(
    values_v: NDArray[np.floating],
    config: QualityConfig = QualityConfig(),
) -> dict:
    """Accept one voltage channel in volts, preserve input, report amplitudes in µV."""
    values = np.asarray(values_v, dtype=float)
    if values.ndim != 1 or values.size == 0:
        raise ValueError("QC expects a nonempty one-dimensional channel")
    finite = np.isfinite(values)
    clean = values[finite] * 1e6
    flags = []
    if not finite.all():
        flags.append("nonfinite_samples")
    ptp = float(np.ptp(clean)) if clean.size else None
    max_abs = float(np.max(np.abs(clean))) if clean.size else None
    if ptp is not None and ptp <= config.flat_ptp_uv:
        flags.append("flat_signal")
    if max_abs is not None and max_abs >= config.extreme_abs_uv:
        flags.append("extreme_amplitude")
    return {
        "sample_count": int(values.size),
        "nonfinite_count": int((~finite).sum()),
        "ptp_uv": ptp,
        "max_abs_uv": max_abs,
        "rms_uv": float(np.sqrt(np.mean(clean**2))) if clean.size else None,
        "flags": flags,
    }
