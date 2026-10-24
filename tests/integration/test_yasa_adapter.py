"""Adapter contract checks without performing a lengthy staging fit in CI."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from packages.models.contracts import STAGES
from packages.models.yasa_adapter import infer_yasa


def test_yasa_hypnogram_adapter_uses_named_probabilities_and_bundled_weights():
    channel = SimpleNamespace(unit="uV", sample_rate_hz=100, sample_count=30000)
    recording = SimpleNamespace(
        channels={"eeg": channel, "eog": channel}, header=SimpleNamespace(duration_s=300)
    )
    p = np.tile([0.6, 0.1, 0.1, 0.1, 0.1], (10, 1))
    frame = pd.DataFrame(p, columns=STAGES).loc[:, list(reversed(STAGES))]
    hyp = SimpleNamespace(proba=frame, hypno=pd.Series(["WAKE"] * 10))
    with (
        patch("packages.models.yasa_adapter.EDFRecording") as reader,
        patch("packages.models.yasa_adapter.mne.io.read_raw_edf") as raw,
        patch("yasa.SleepStaging") as staging,
    ):
        reader.return_value.__enter__.return_value = recording
        staging.return_value.predict.return_value = hyp
        actual, metadata = infer_yasa(Path("source.edf"), "eeg", "eog")
        np.testing.assert_allclose(actual, p)
        assert len(metadata["weight_sha256"]) == 64
        assert metadata["external_preprocessing"] == "none"
        assert raw.call_args.kwargs["preload"] is False
        assert raw.call_args.kwargs["include"] == ["eeg", "eog"]
        assert staging.return_value.predict.call_args.kwargs["path_to_model"].endswith(
            "clf_eeg+eog_lgb_0.5.0.joblib"
        )


def test_low_rate_emg_rejected_before_staging():
    eeg = SimpleNamespace(unit="uV", sample_rate_hz=100, sample_count=30000)
    emg = SimpleNamespace(unit="uV", sample_rate_hz=1, sample_count=300)
    recording = SimpleNamespace(
        channels={"eeg": eeg, "emg": emg}, header=SimpleNamespace(duration_s=300)
    )
    with patch("packages.models.yasa_adapter.EDFRecording") as reader:
        reader.return_value.__enter__.return_value = recording
        with pytest.raises(ValueError, match="same-rate"):
            infer_yasa(Path("source.edf"), "eeg", emg="emg")
