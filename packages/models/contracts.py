"""Canonical probability schema shared by every staging model."""

import numpy as np
from numpy.typing import ArrayLike, NDArray

STAGES = ("WAKE", "N1", "N2", "N3", "REM")


def probabilities(values: ArrayLike, classes: list[str]) -> NDArray[np.float64]:
    p = np.asarray(values, dtype=float)
    if len(classes) != 5 or set(classes) != set(STAGES):
        raise ValueError("Model must expose all five unique canonical stage names")
    if p.ndim != 2 or p.shape[1] != 5 or p.shape[0] == 0:
        raise ValueError("Expected nonempty epoch-by-five probability matrix")
    p = p[:, [classes.index(stage) for stage in STAGES]]
    if not np.isfinite(p).all() or np.any(p < 0) or np.any(p > 1):
        raise ValueError("Probabilities must be finite and lie in [0,1]")
    if not np.allclose(p.sum(axis=1), 1, atol=1e-5, rtol=0):
        raise ValueError("Probability rows must sum to one")
    return p


def epoch_predictions(p: ArrayLike) -> list[dict]:
    matrix = probabilities(p, list(STAGES))
    return [
        {
            "epoch_index": i,
            "start_s": 30 * i,
            "duration_s": 30,
            "predicted_stage": STAGES[int(row.argmax())],
            "stage_probabilities": dict(zip(STAGES, row.tolist())),
        }
        for i, row in enumerate(matrix)
    ]
