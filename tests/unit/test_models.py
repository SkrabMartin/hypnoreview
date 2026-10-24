"""Model schema, feature physics, probability calibration metrics and leakage guards."""

import copy

import numpy as np
import pytest

from packages.evaluation.classification import classification_metrics, development_oof
from packages.models.baseline import export_baseline, fit_baseline, predict_baseline
from packages.models.contracts import STAGES, epoch_predictions, probabilities
from packages.models.features import FEATURE_NAMES, eeg_features


def training_data():
    rng = np.random.default_rng(42)
    labels = list(STAGES) * 20
    x = rng.normal(0, 0.1, (100, len(FEATURE_NAMES)))
    for index in range(100):
        x[index, index % 5] += 3
    subjects = [f"participant-{i // 25}" for i in range(100)]
    return x, labels, subjects


def test_explicit_class_order_and_epoch_alignment():
    classes = list(reversed(STAGES))
    p = probabilities([[0.7, 0.1, 0.1, 0.05, 0.05]], classes)
    assert p[0, -1] == 0.7
    assert epoch_predictions(p)[0]["predicted_stage"] == "REM"
    assert epoch_predictions(np.repeat(p, 2, axis=0))[1]["start_s"] == 30


@pytest.mark.parametrize(
    "p", [[[1, 1, 0, 0, 0]], [[-1, 1, 1, 0, 0]], [[float("nan"), 0, 0, 0, 1]], []]
)
def test_invalid_probabilities_rejected(p):
    with pytest.raises(ValueError):
        probabilities(p, list(STAGES))


def test_missing_or_duplicate_classes_rejected():
    with pytest.raises(ValueError):
        probabilities([[0.2] * 5], ["WAKE"] * 5)


def test_welch_feature_physics_and_input_immutability():
    time = np.arange(3000) / 100
    values = 20e-6 * np.sin(2 * np.pi * 10 * time)
    original = values.copy()
    result = eeg_features(values, 100)
    assert result.shape == (len(FEATURE_NAMES),)
    assert result[6] > 0.99  # 10Hz energy belongs to alpha, independent of labels.
    np.testing.assert_array_equal(values, original)
    assert np.isfinite(eeg_features(np.zeros(3000), 100)).all()
    with pytest.raises(ValueError):
        eeg_features(values[:-1], 100)
    with pytest.raises(ValueError):
        eeg_features(values, 1)


def test_json_model_matches_sklearn_and_training_only_scaling():
    x, labels, subjects = training_data()
    x[0, 2] = np.nan
    model = fit_baseline(x, labels)
    artifact = export_baseline(model, subjects)
    expected = probabilities(model.predict_proba(x), model.classes_.tolist())
    np.testing.assert_allclose(predict_baseline(artifact, x), expected, atol=1e-12)
    original = model["scaler"].mean_.copy()
    predict_baseline(artifact, x * 1000)
    np.testing.assert_array_equal(model["scaler"].mean_, original)
    damaged = copy.deepcopy(artifact)
    damaged["scaler_scale"][0] = 0
    with pytest.raises(ValueError):
        predict_baseline(damaged, x)
    with pytest.raises(ValueError):
        fit_baseline(x[:1], labels[:1])


def test_grouped_oof_never_crosses_subjects():
    x, labels, subjects = training_data()
    p, folds = development_oof(x, labels, subjects)
    assert p.shape == (len(labels), 5)
    assert len(folds) == 4
    for fold in folds:
        assert set(fold["train_subjects"]).isdisjoint(fold["held_subjects"])
    with pytest.raises(ValueError):
        development_oof(x, labels, ["same"] * 100)


def test_metrics_use_canonical_order_and_multiclass_brier_definition():
    labels = list(STAGES)
    metrics = classification_metrics(labels, np.eye(5))
    assert metrics["macro_f1"] == metrics["balanced_accuracy"] == 1
    assert metrics["multiclass_brier_sum"] == 0
    assert metrics["log_loss"] < 1e-12
    uniform = classification_metrics(labels, np.ones((5, 5)) / 5)
    assert uniform["multiclass_brier_sum"] == pytest.approx(0.8)
    assert uniform["log_loss"] == pytest.approx(np.log(5))
