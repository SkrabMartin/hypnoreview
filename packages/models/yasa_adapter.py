"""Pinned YASA 0.8 adapter using untouched voltage input and packaged weights."""

import warnings
from pathlib import Path

import mne
import numpy as np

from packages.models.contracts import STAGES, probabilities
from packages.signals.edf_loader import EDFRecording
from tools.audit_sleep_edf import sha256


def bundled_weights(eog: bool = True, emg: bool = False) -> Path:
    import yasa

    if yasa.__version__ != "0.8.0":
        raise ValueError("Adapter requires exactly YASA 0.8.0")
    name = "clf_eeg" + ("+eog" if eog else "") + ("+emg" if emg else "") + "_lgb_0.5.0.joblib"
    weights = Path(yasa.__file__).parent / "classifiers" / name
    if not weights.is_file():
        raise ValueError("Pinned YASA classifier is missing from the installed package")
    return weights


def infer_yasa(
    psg: Path, eeg: str, eog: str | None = None, emg: str | None = None
) -> tuple[np.ndarray, dict]:
    import yasa

    if yasa.__version__ != "0.8.0":
        raise ValueError("Adapter requires exactly YASA 0.8.0")
    selected = [n for n in (eeg, eog, emg) if n is not None]
    if len(selected) != len(set(selected)):
        raise ValueError("YASA channel roles must be distinct")
    with EDFRecording(psg) as recording:
        if any(name not in recording.channels for name in selected):
            raise ValueError("YASA channel mapping does not match the recording")
        channels = [recording.channels[name] for name in selected]
        if any(c.unit.lower() not in ("uv", "µv", "μv", "mv", "v") for c in channels):
            raise ValueError("YASA requires recognized voltage units")
        if len({c.sample_rate_hz for c in channels}) != 1 or channels[0].sample_rate_hz < 64:
            raise ValueError(
                "Select same-rate EEG/EOG/EMG >=64Hz; low-rate EMG envelopes are not staging inputs"
            )
        if sum(c.sample_count for c in channels) * 8 > 512 * 1024**2:
            raise ValueError("Selected YASA signals exceed the 512MiB input allocation limit")
        expected = int(recording.header.duration_s // 30)
        if expected < 10:
            raise ValueError("YASA staging requires at least five minutes of data")
    weights = bundled_weights(bool(eog), bool(emg))
    # No caller-supplied pickle is accepted. Only the pinned package's bundled model.
    with mne.io.read_raw_edf(
        psg, include=selected, preload=False, infer_types=False, verbose="ERROR"
    ) as raw:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            prediction = yasa.SleepStaging(raw, eeg_name=eeg, eog_name=eog, emg_name=emg).predict(
                path_to_model=str(weights)
            )
    if prediction.proba is None:
        raise ValueError("YASA did not return probabilities")
    p = probabilities(prediction.proba.to_numpy(), list(prediction.proba.columns))
    labels = prediction.hypno.astype(str).tolist()
    if (
        len(p) != expected
        or len(labels) != expected
        or any(label not in STAGES for label in labels)
    ):
        raise ValueError("YASA output labels/epoch count do not match full input epochs")
    if labels != [STAGES[int(i)] for i in p.argmax(axis=1)]:
        raise ValueError("YASA labels disagree with its probability schema")
    return p, {
        "model_name": "yasa",
        "model_version": "yasa-0.8.0-adapter-v1",
        "weight_name": weights.name,
        "weight_sha256": sha256(weights),
        "yasa_version": yasa.__version__,
        "mapping": {"eeg": eeg, "eog": eog, "emg": emg},
        "external_preprocessing": "none",
        "compatibility_warnings": [str(w.message) for w in caught],
        "context": "offline whole-night",
        "pretraining_overlap": "unresolved; not independently externally validated",
        "montage_limitation": "Sleep-EDF Fpz-Cz differs from preferred central EEG",
    }
