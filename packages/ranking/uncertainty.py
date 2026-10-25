"""Bounded uncertainty features and the specification's unvalidated heuristic."""

from dataclasses import asdict, dataclass

import numpy as np
from numpy.typing import ArrayLike

from packages.models.contracts import STAGES, probabilities


@dataclass(frozen=True)
class RankingConfig:
    entropy_weight: float = 0.45
    margin_weight: float = 0.20
    transition_weight: float = 0.15
    quality_weight: float = 0.10
    disagreement_weight: float = 0.10

    def __post_init__(self) -> None:
        weights = np.array(list(asdict(self).values()))
        if (
            not np.isfinite(weights).all()
            or np.any(weights < 0)
            or not np.isclose(weights.sum(), 1)
        ):
            raise ValueError("Priority weights must be finite, nonnegative and sum to one")


def uncertainty(p: ArrayLike) -> tuple[np.ndarray, np.ndarray]:
    matrix = probabilities(p, list(STAGES))
    entropy = -np.sum(matrix * np.log(np.maximum(matrix, 1e-12)), axis=1) / np.log(5)
    ordered = np.sort(matrix, axis=1)
    margin = 1 - (ordered[:, -1] - ordered[:, -2])
    return np.clip(entropy, 0, 1), np.clip(margin, 0, 1)


def score_epochs(
    p: ArrayLike,
    transitions: ArrayLike,
    quality: ArrayLike,
    secondary: list | None = None,
    config: RankingConfig = RankingConfig(),
) -> list[dict]:
    matrix = probabilities(p, list(STAGES))
    entropy, margin = uncertainty(matrix)
    transition, qc = np.asarray(transitions, dtype=float), np.asarray(quality, dtype=float)
    for values in (transition, qc):
        if (
            values.shape != (len(matrix),)
            or not np.isfinite(values).all()
            or np.any((values < 0) | (values > 1))
        ):
            raise ValueError("Normalized ranking features must match epoch count and lie in [0,1]")
    if secondary is not None and len(secondary) != len(matrix):
        raise ValueError("Secondary predictions must align with primary epochs")
    result = []
    for i, row in enumerate(matrix):
        other = secondary[i] if secondary is not None else None
        disagreement = (
            float(row.argmax() != probabilities([other], list(STAGES))[0].argmax())
            if other is not None
            else 0.0
        )
        components = {
            "entropy": float(entropy[i]),
            "margin_uncertainty": float(margin[i]),
            "transition_surprise": float(transition[i]),
            "signal_quality": float(qc[i]),
            "model_disagreement": disagreement,
        }
        score = sum(
            v * w for v, w in zip(components.values(), asdict(config).values(), strict=True)
        )
        reasons = [
            key
            for key, threshold in (
                ("entropy", 0.7),
                ("margin_uncertainty", 0.75),
                ("transition_surprise", 0.7),
                ("signal_quality", 0.7),
                ("model_disagreement", 0.5),
            )
            if components[key] > threshold
        ]
        top = np.argsort(-row, kind="stable")[:2]
        explanation = "; ".join([*(f"{STAGES[j]}={row[j]:.4f}" for j in top), *reasons])
        result.append(
            {
                "review_score": float(np.clip(score, 0, 1)),
                "review_reasons": reasons,
                "components": components,
                "explanation": explanation,
                "secondary_available": other is not None,
                "secondary_predicted_stage": STAGES[int(np.argmax(other))]
                if other is not None
                else None,
            }
        )
    return result
