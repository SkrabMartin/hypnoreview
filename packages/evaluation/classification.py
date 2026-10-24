"""Initial classification checks; review efficiency belongs to a later phase."""

import numpy as np
from numpy.typing import ArrayLike
from sklearn.metrics import (
    balanced_accuracy_score,
    cohen_kappa_score,
    confusion_matrix,
    f1_score,
    log_loss,
)
from sklearn.model_selection import LeaveOneGroupOut

from packages.models.baseline import export_baseline, fit_baseline
from packages.models.contracts import STAGES, probabilities


def classification_metrics(labels: list[str], p: ArrayLike) -> dict:
    matrix = probabilities(p, list(STAGES))
    if len(labels) != len(matrix) or not set(labels) <= set(STAGES):
        raise ValueError("Reference labels must match eligible prediction rows")
    predicted = np.asarray(STAGES)[matrix.argmax(axis=1)]
    truth = np.array([STAGES.index(label) for label in labels])
    kappa = float(cohen_kappa_score(labels, predicted, labels=list(STAGES)))
    # sklearn log_loss sorts class names; reorder columns explicitly.
    alphabetical = sorted(STAGES)
    return {
        "n_epochs": len(labels),
        "macro_f1": float(
            f1_score(labels, predicted, labels=list(STAGES), average="macro", zero_division=0)
        ),
        "balanced_accuracy": float(balanced_accuracy_score(labels, predicted)),
        "cohen_kappa": kappa if np.isfinite(kappa) else None,
        "confusion_matrix": confusion_matrix(labels, predicted, labels=list(STAGES)).tolist(),
        "class_order": list(STAGES),
        "log_loss": float(
            log_loss(
                labels, matrix[:, [STAGES.index(s) for s in alphabetical]], labels=alphabetical
            )
        ),
        "multiclass_brier_sum": float(np.mean(np.sum((matrix - np.eye(5)[truth]) ** 2, axis=1))),
    }


def development_oof(
    x: ArrayLike, labels: list[str], subjects: list[str]
) -> tuple[np.ndarray, list[dict]]:
    matrix = np.asarray(x, dtype=float)
    if len(subjects) != len(labels) or len(set(subjects)) < 2:
        raise ValueError(
            "At least two participants are needed for subject-wise development evaluation"
        )
    output = np.empty((len(labels), 5))
    folds = []
    y = np.asarray(labels)
    groups = np.asarray(subjects)
    for train, held in LeaveOneGroupOut().split(matrix, y, groups):
        train_ids, held_ids = set(groups[train]), set(groups[held])
        if train_ids & held_ids:
            raise ValueError("Participant leakage detected")
        model = fit_baseline(matrix[train], y[train].tolist())
        output[held] = probabilities(model.predict_proba(matrix[held]), model.classes_.tolist())
        folds.append(
            {
                "train_subjects": sorted(train_ids),
                "held_subjects": sorted(held_ids),
                "n_held_epochs": len(held),
                "model_artifact": export_baseline(model, sorted(train_ids)),
            }
        )
    return output, folds
