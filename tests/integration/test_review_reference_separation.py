"""Current reference labels must never change a generated review queue."""

import copy

import pytest

from packages.models.contracts import epoch_predictions
from packages.models.features import FEATURE_NAMES, FEATURE_VERSION
from packages.ranking.cli import build_queue, review_orders


def inputs():
    artifact = {
        "feature_names": list(FEATURE_NAMES),
        "feature_version": FEATURE_VERSION,
        "classes": ["WAKE", "N1", "N2", "N3", "REM"],
        "train_subjects": ["other"],
        "imputer_median": [0] * 9,
        "scaler_mean": [0] * 9,
        "scaler_scale": [1] * 9,
        "coefficients": [[0] * 9 for _ in range(5)],
        "intercept": [0] * 5,
    }
    imported = {
        "identity": {"recording_id": "night", "subject_id": "held"},
        "epochs": [
            {
                "index": i,
                "complete": True,
                "valid_reference": True,
                "reference_stage": "N1",
                "quality": {},
            }
            for i in range(3)
        ],
    }
    primary = [
        {**row, "status": "predicted"}
        for row in epoch_predictions(
            [[0.9, 0.025, 0.025, 0.025, 0.025], [0.2] * 5, [0.025, 0.9, 0.025, 0.025, 0.025]]
        )
    ]
    context = {
        "train_subjects": ["other"],
        "transition_counts": [[1] * 5 for _ in range(5)],
        "quality_bounds_uv": {},
    }
    return imported, primary, [[0] * 9 for _ in range(3)], artifact, context


def test_current_reference_and_eligibility_do_not_influence_scores_or_order():
    imported, primary, features, artifact, context = inputs()
    original = build_queue(imported, primary, features, artifact, context)
    changed = copy.deepcopy(imported)
    for epoch in changed["epochs"]:
        epoch["reference_stage"] = "REM"
        epoch["valid_reference"] = False
    reranked = build_queue(changed, primary, features, artifact, context)
    assert original == reranked
    assert review_orders(original) == review_orders(reranked)
    assert all("reference_stage" not in row for row in original)


def test_in_sample_secondary_is_refused():
    imported, primary, features, artifact, context = inputs()
    artifact["train_subjects"] = ["held"]
    with pytest.raises(ValueError, match="excluded"):
        build_queue(imported, primary, features, artifact, context)
