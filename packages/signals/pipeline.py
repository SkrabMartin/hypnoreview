"""Reproducible, unfiltered signal import with immutable source provenance."""

from dataclasses import asdict
from pathlib import Path

import mne
import numpy as np

from packages.signals.edf_loader import EDFRecording, channel_mapping, load_annotations
from packages.signals.epochs import align_epochs
from packages.signals.quality import QualityConfig, signal_quality
from tools.audit_sleep_edf import sha256


def import_recording(
    psg: Path,
    annotation_path: Path | None = None,
    mapping: dict[str, str | None] | None = None,
    quality_config: QualityConfig = QualityConfig(),
) -> dict:
    """Return JSON-safe metadata/epochs/QC. Never modifies original files.

    Unannotated recordings are valid inputs with invalid (missing) reference
    labels. QC uses raw voltages in bounded windows, without filtering/resampling.
    """
    with EDFRecording(psg) as recording:
        chosen = mapping if mapping is not None else channel_mapping(recording.header.channels)
        if not chosen.get("eeg"):
            raise ValueError("EEG channel mapping required; unknown montage is not inferred")
        for role, name in chosen.items():
            if role not in ("eeg", "eog", "emg"):
                raise ValueError("Unsupported channel role")
            if name is not None and name not in recording.channels:
                raise ValueError(f"Mapped {role} channel does not exist")
        selected = [n for n in chosen.values() if n is not None]
        if len(selected) != len(set(selected)):
            raise ValueError("Channel roles must refer to distinct signals")
        annotations, alignment = (
            load_annotations(recording, annotation_path)
            if annotation_path is not None
            else ([], {"status": "missing_annotations"})
        )
        epochs = align_epochs(recording.header.duration_s, annotations)
        rows = []
        for epoch in epochs:
            qc = {}
            for role, name in chosen.items():
                if name is None:
                    continue
                channel = recording.channels[name]
                values = recording.read_channel(name, epoch.start_s, epoch.duration_s)
                qc[role] = {
                    "channel": name,
                    "native_rate_hz": channel.sample_rate_hz,
                    **signal_quality(values, quality_config),
                }
                if role == "emg" and channel.sample_rate_hz < 10:
                    qc[role]["flags"].append("low_rate_emg_envelope")
            rows.append({**asdict(epoch), "quality": qc})
        return {
            "schema_version": 1,
            "parser_version": "signal-foundation-v1",
            "source": {
                "psg_name": psg.name,
                "psg_sha256": sha256(psg),
                "annotation_name": annotation_path.name if annotation_path else None,
                "annotation_sha256": sha256(annotation_path) if annotation_path else None,
            },
            "versions": {"mne": mne.__version__, "numpy": np.__version__},
            "metadata": recording.metadata(),
            "mapping": chosen,
            "processing": {
                "filtered": False,
                "resampled": False,
                "epoch_seconds": 30,
                "minimum_reference_coverage_s": 29.9,
                "quality_config": asdict(quality_config),
            },
            "alignment": alignment,
            "source_annotations": [asdict(a) for a in annotations],
            "epochs": rows,
        }
