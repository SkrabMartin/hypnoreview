"""Transparent logistic baseline; JSON artifacts contain no executable pickle."""

import warnings

import numpy as np
from numpy.typing import ArrayLike, NDArray
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from packages.models.contracts import STAGES, probabilities
from packages.models.features import FEATURE_NAMES, FEATURE_VERSION


def fit_baseline(x: ArrayLike, labels: list[str]) -> Pipeline:
    matrix = np.asarray(x, dtype=float)
    if (
        matrix.ndim != 2
        or matrix.shape != (len(labels), len(FEATURE_NAMES))
        or np.isinf(matrix).any()
    ):
        raise ValueError("Invalid baseline feature matrix")
    if set(labels) != set(STAGES):
        raise ValueError("Training must contain all five sleep stages")
    if np.isnan(matrix).all(axis=0).any():
        raise ValueError("Entirely missing feature column cannot be fit")
    model = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "classifier",
                LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000, random_state=42),
            ),
        ]
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        model.fit(matrix, labels)
    return model


def export_baseline(model: Pipeline, train_subjects: list[str]) -> dict:
    return {
        "model_name": "logistic-baseline",
        "model_version": "logistic-v1",
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "classes": model.classes_.tolist(),
        "train_subjects": sorted(set(train_subjects)),
        "configuration": {"C": 1.0, "class_weight": "balanced", "seed": 42},
        "imputer_median": model["imputer"].statistics_.tolist(),
        "scaler_mean": model["scaler"].mean_.tolist(),
        "scaler_scale": model["scaler"].scale_.tolist(),
        "coefficients": model["classifier"].coef_.tolist(),
        "intercept": model["classifier"].intercept_.tolist(),
    }


def predict_baseline(artifact: dict, x: ArrayLike) -> NDArray[np.float64]:
    if artifact["feature_version"] != FEATURE_VERSION or artifact["feature_names"] != list(
        FEATURE_NAMES
    ):
        raise ValueError("Baseline feature schema mismatch")
    n = len(FEATURE_NAMES)
    median = np.asarray(artifact["imputer_median"], dtype=float)
    mean = np.asarray(artifact["scaler_mean"], dtype=float)
    scale = np.asarray(artifact["scaler_scale"], dtype=float)
    coef = np.asarray(artifact["coefficients"], dtype=float)
    intercept = np.asarray(artifact["intercept"], dtype=float)
    matrix = np.asarray(x, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] != n or np.isinf(matrix).any():
        raise ValueError("Invalid prediction features")
    if (
        any(v.shape != (n,) or not np.isfinite(v).all() for v in (median, mean, scale))
        or np.any(scale <= 0)
        or coef.shape != (5, n)
        or intercept.shape != (5,)
        or not np.isfinite(coef).all()
        or not np.isfinite(intercept).all()
    ):
        raise ValueError("Invalid baseline artifact parameters")
    matrix = np.where(np.isnan(matrix), median, matrix)
    logits = ((matrix - mean) / scale) @ coef.T + intercept
    logits -= logits.max(axis=1, keepdims=True)
    exp = np.exp(logits)
    return probabilities(exp / exp.sum(axis=1, keepdims=True), artifact["classes"])
