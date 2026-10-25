"""Numerical ranking, budget definitions, and participant-level uncertainty."""

import copy

import numpy as np
import pytest

from packages.evaluation.review import aggregate_review, review_curve
from packages.ranking.context import fit_context, quality_risk, transition_surprise
from packages.ranking.prioritization import order_queue
from packages.ranking.uncertainty import RankingConfig, score_epochs


def test_exact_entropy_margin_and_weighted_score():
    onehot = [[1, 0, 0, 0, 0]]
    sure = score_epochs(onehot, [0], [0])[0]
    assert sure["review_score"] == 0
    uniform = score_epochs([[0.2] * 5], [0], [0])[0]
    assert uniform["review_score"] == pytest.approx(0.65)
    other = [[0, 1, 0, 0, 0]]
    alert = score_epochs(onehot, [1], [1], other)[0]
    assert alert["review_score"] == pytest.approx(0.35)
    assert alert["secondary_predicted_stage"] == "N1"
    assert "model_disagreement" in alert["review_reasons"]
    assert "WAKE=1.0000" in alert["explanation"]


@pytest.mark.parametrize("value", [-1, 1.1, float("nan"), float("inf")])
def test_invalid_normalized_features_refused(value):
    with pytest.raises(ValueError):
        score_epochs([[0.2] * 5], [value], [0])


def test_invalid_weights_and_missing_secondary_are_explicit():
    with pytest.raises(ValueError):
        RankingConfig(entropy_weight=1)
    with pytest.raises(ValueError):
        RankingConfig(quality_weight=float("nan"))
    row = score_epochs([[0.2] * 5], [0], [0], [None])[0]
    assert row["secondary_available"] is False
    assert row["components"]["model_disagreement"] == 0


def training_record(subject="train"):
    return {
        "subject_id": subject,
        "epochs": [
            {
                "index": i,
                "reference_stage": stage,
                "valid_reference": valid,
                "quality": {"eeg": {"ptp_uv": float(i + 1), "flags": []}},
            }
            for i, stage, valid in [
                (0, "WAKE", True),
                (1, "N2", True),
                (2, "N2", False),
                (3, "REM", True),
            ]
        ],
    }


def test_training_context_excludes_held_subject_and_does_not_bridge_gaps():
    context = fit_context([training_record()], {"held"})
    counts = np.array(context["transition_counts"])
    assert counts[0, 2] == 2
    assert counts.sum() == 26  # exactly one valid transition plus smoothing
    with pytest.raises(ValueError, match="participant-disjoint"):
        fit_context([training_record("held")], {"held"})
    p = np.array([[1, 0, 0, 0, 0], [0, 0, 1, 0, 0]])
    assert transition_surprise(p, [0, 2], context).tolist() == [0, 0]
    assert 0 < transition_surprise(p, [0, 1], context)[1] < 1


def test_quality_thresholds_are_fitted_on_training_and_flags_are_bounded():
    context = fit_context([training_record()], {"held"})
    rows = [
        {"quality": {"eeg": {"ptp_uv": 1000, "flags": []}}},
        {"quality": {"eeg": {"ptp_uv": 1, "flags": ["flat_signal"]}}},
        {"quality": {}},
    ]
    assert quality_risk(rows, context).tolist() == [1, 1, 0]
    assert context["quality_bounds_uv"]["eeg"][1] < 4


def queue_rows():
    return [
        {"recording_id": "night", "epoch_index": i, "review_score": 1 - i / 10} for i in range(6)
    ]


def test_coverage_order_is_complete_deterministic_and_night_local():
    rows = queue_rows()
    assert order_queue(rows) == list(range(6))
    assert order_queue(rows, "coverage_aware") == [0, 3, 1, 2, 4, 5]
    rows.append({"recording_id": "another", "epoch_index": 0, "review_score": 1})
    covered = order_queue(rows, "coverage_aware")
    assert covered[:2] == [6, 0]
    assert order_queue(rows, "random") == order_queue(rows, "random")
    assert sorted(covered) == list(range(7))
    with pytest.raises(ValueError):
        order_queue(rows + [rows[0]])


def test_budget_counts_and_error_recall_are_hand_computable():
    curve = review_curve([True, False, True, False], [0, 2, 1, 3], [0, 0.25, 0.5, 1])
    assert curve["reviewed"] == [0, 1, 2, 4]
    assert curve["found"] == [0, 1, 2, 2]
    assert review_curve([True] * 3, [0, 1, 2], [0.1])["reviewed"] == [1]
    with pytest.raises(ValueError):
        review_curve([False], [0.0], [0.5])


def test_zero_errors_have_undefined_recall_and_no_fake_interval():
    curve = review_curve([False, False], [0, 1], [0, 0.5, 1])
    records = [{"subject_id": "p", "methods": {"risk": curve}}]
    result = aggregate_review(records, [0, 0.5, 1], bootstrap_samples=20)
    method = result["methods"]["risk"]
    assert method["area_under_review_recall"] is None
    assert method["curve"][1]["error_recall"] is None
    assert method["curve"][1]["precision"] == 0
    assert method["curve"][1]["error_recall_ci95"] is None


def test_cluster_bootstrap_is_seeded_and_equal_budgets_enforced():
    budgets = [0, 0.5, 1]
    a = review_curve([True, False], [0, 1], budgets)
    b = review_curve([False, True], [0, 1], budgets)
    records = [
        {"subject_id": "p1", "methods": {"risk": a, "confidence": a}},
        {"subject_id": "p1", "methods": {"risk": b, "confidence": b}},
        {"subject_id": "p2", "methods": {"risk": b, "confidence": b}},
    ]
    first = aggregate_review(records, budgets, 50)
    assert first == aggregate_review(records, budgets, 50)
    assert first["bootstrap"]["subjects"] == ["p1", "p2"]
    assert first["methods"]["risk"]["curve"][-1]["error_recall"] == 1
    bad = copy.deepcopy(records)
    bad[0]["methods"]["risk"]["reviewed"][1] = 0
    with pytest.raises(ValueError, match="reference pool"):
        aggregate_review(bad, budgets, 20)
