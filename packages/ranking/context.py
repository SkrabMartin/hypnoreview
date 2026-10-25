"""Fit transition/quality context on explicitly separated training participants."""

import numpy as np

from packages.models.contracts import STAGES, probabilities


def fit_context(training: list[dict], held_subjects: set[str]) -> dict:
    subjects = {r["subject_id"] for r in training}
    if not subjects or subjects & held_subjects:
        raise ValueError("Ranking context requires nonempty participant-disjoint training records")
    counts = np.ones((5, 5), dtype=float)  # Laplace smoothing, including self transitions.
    amplitudes: dict[str, list[float]] = {"eeg": [], "eog": []}
    for recording in training:
        previous = None
        for row in recording["epochs"]:
            if row["valid_reference"]:
                stage = STAGES.index(row["reference_stage"])
                if previous is not None and previous[0] + 1 == row["index"]:
                    counts[previous[1], stage] += 1
                previous = (row["index"], stage)
            else:
                previous = None
            for role in amplitudes:
                qc = row.get("quality", {}).get(role)
                if qc and qc.get("ptp_uv") is not None and np.isfinite(qc["ptp_uv"]):
                    amplitudes[role].append(float(qc["ptp_uv"]))
    bounds = {
        role: np.quantile(values, [0.95, 0.995]).tolist()
        for role, values in amplitudes.items()
        if values
    }
    return {
        "version": "transition-laplace-quality-quantiles-v1",
        "train_subjects": sorted(subjects),
        "transition_counts": counts.tolist(),
        "quality_bounds_uv": bounds,
    }


def transition_surprise(p: np.ndarray, epoch_indices: list[int], context: dict) -> np.ndarray:
    matrix = probabilities(p, list(STAGES))
    counts = np.asarray(context["transition_counts"], dtype=float)
    if counts.shape != (5, 5) or not np.isfinite(counts).all() or np.any(counts < 1):
        raise ValueError("Transition counts require finite Laplace-smoothed 5x5 values")
    if len(epoch_indices) != len(matrix) or any(
        b <= a for a, b in zip(epoch_indices, epoch_indices[1:])
    ):
        raise ValueError("Epoch indices must align and be strictly increasing")
    labels = matrix.argmax(axis=1)
    output = np.zeros(len(matrix))
    for i in range(1, len(matrix)):
        if epoch_indices[i] != epoch_indices[i - 1] + 1:
            continue
        row = counts[labels[i - 1]]
        output[i] = -np.log(row[labels[i]] / row.sum()) / np.log(row.sum())
    return np.clip(output, 0, 1)


def quality_risk(rows: list[dict], context: dict) -> np.ndarray:
    output = []
    for epoch in rows:
        risks = []
        for role in ("eeg", "eog"):
            qc = epoch.get("quality", {}).get(role)
            if qc is None:
                continue
            if set(qc.get("flags", [])) & {"nonfinite_samples", "flat_signal", "extreme_amplitude"}:
                risks.append(1.0)
            value = qc.get("ptp_uv")
            if value is not None and role in context["quality_bounds_uv"]:
                lo, hi = context["quality_bounds_uv"][role]
                if not np.isfinite([lo, hi, value]).all() or lo > hi:
                    raise ValueError("Invalid quality normalization parameters")
                risks.append(
                    float(np.clip((value - lo) / (hi - lo), 0, 1)) if hi > lo else float(value > hi)
                )
        output.append(max(risks, default=0.0))
    return np.asarray(output)
