"""Develop baseline staging on frozen training samples without opening the test set."""

import argparse
import hashlib
import importlib.metadata
import json
import logging
import subprocess
from pathlib import Path

import numpy as np

from packages.evaluation.classification import classification_metrics, development_oof
from packages.models.baseline import export_baseline, fit_baseline, predict_baseline
from packages.models.contracts import STAGES, epoch_predictions, probabilities
from packages.models.features import FEATURE_NAMES, FEATURE_VERSION, eeg_features
from packages.models.yasa_adapter import bundled_weights, infer_yasa
from packages.signals.cli import write_result
from packages.signals.edf_loader import EDFRecording
from tools.audit_sleep_edf import MANIFEST_SHA256, parse_checksums, safe_path, sha256

LOGGER = logging.getLogger("hypnoreview.models")


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def development_inputs(manifest_path: Path, imports: Path) -> list[tuple[dict, dict, Path]]:
    root = manifest_path.parent
    sums_path = root / "SHA256SUMS.txt"
    if sha256(sums_path) != MANIFEST_SHA256:
        raise ValueError("Unrecognized source checksum manifest")
    sums = parse_checksums(sums_path.read_text())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ids = manifest["sample_recording_ids"]
    if len(set(ids)) != 5:
        raise ValueError("Expected the five development samples")
    jobs = []
    for record in manifest["recordings"]:
        if record["recording_id"] not in ids:
            continue
        if manifest["subject_splits"][record["subject_id"]] != "train":
            raise ValueError("Development command refuses validation and test participants")
        imported = json.loads(
            safe_path(imports, record["recording_id"] + ".json").read_text(encoding="utf-8")
        )
        for kind in ("psg", "annotation"):
            path = safe_path(root, record[kind])
            if (
                sha256(path) != sums[record[kind]]
                or imported["source"][kind + "_sha256"] != sums[record[kind]]
            ):
                raise ValueError("Source or imported-reference provenance mismatch")
        if imported["identity"]["subject_id"] != record["subject_id"] or imported["identity"][
            "audit_manifest_sha256"
        ] != sha256(manifest_path):
            raise ValueError("Import participant/manifest identity mismatch")
        jobs.append((record, imported, safe_path(root, record["psg"])))
    if len(jobs) != 5:
        raise ValueError("Incomplete development sample inventory")
    return jobs


def run_development(manifest: Path, imports: Path, output: Path, with_yasa: bool = True) -> dict:
    jobs = development_inputs(manifest, imports)
    versions = {
        name: importlib.metadata.version(name)
        for name in ("hypnoreview", "yasa", "scikit-learn", "lightgbm", "mne", "numpy", "scipy")
    }
    code_hash = hashlib.sha256(
        b"".join(p.read_bytes() for p in sorted(Path(__file__).parents[1].rglob("*.py")))
    ).hexdigest()
    revision = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        ).stdout.strip()
        or None
    )
    source_config = {
        "manifest_sha256": sha256(manifest),
        "versions": versions,
        "implementation_sha256": code_hash,
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "with_yasa": with_yasa,
        "source_revision": revision,
        "recordings": [record["recording_id"] for record, _, _ in jobs],
        "import_sha256": {
            record["recording_id"]: sha256(imports / (record["recording_id"] + ".json"))
            for record, _, _ in jobs
        },
        "yasa_weight_sha256": sha256(bundled_weights()) if with_yasa else None,
    }
    run_id = digest(source_config)
    run_dir = output / run_id
    all_features, eligible, labels, subjects = [], [], [], []
    features_by_record = {}
    for record, imported, psg in jobs:
        identifier = record["recording_id"]
        eeg = imported["mapping"]["eeg"]
        cache_path = run_dir / (identifier + "-features.json")
        cache = None
        if cache_path.exists():
            cache = json.loads(cache_path.read_text())
            if (
                cache["psg_sha256"] != record["psg_sha256"]
                or cache["feature_version"] != FEATURE_VERSION
            ):
                raise ValueError("Feature cache provenance mismatch")
        if cache is None:
            rows: list[list[float] | None] = []
            with EDFRecording(psg) as recording:
                rate = recording.channels[eeg].sample_rate_hz
                for epoch in imported["epochs"]:
                    if not epoch["complete"]:
                        rows.append(None)
                        continue
                    try:
                        rows.append(
                            eeg_features(
                                recording.read_channel(eeg, epoch["start_s"], 30), rate
                            ).tolist()
                        )
                    except ValueError:
                        rows.append(None)
            cache = {
                "psg_sha256": record["psg_sha256"],
                "feature_version": FEATURE_VERSION,
                "features": rows,
            }
            write_result(cache_path, cache)
        features_by_record[identifier] = cache["features"]
        for epoch, features in zip(imported["epochs"], cache["features"], strict=True):
            if epoch["valid_reference"] and features is not None:
                eligible.append((identifier, epoch["index"]))
                all_features.append(features)
                labels.append(epoch["reference_stage"])
                subjects.append(record["subject_id"])
        LOGGER.info("features_ready recording=%s", identifier)
    x = np.asarray(all_features)
    oof, folds = development_oof(x, labels, subjects)
    model = fit_baseline(x, labels)
    artifact = export_baseline(model, subjects)
    artifact["provenance"] = source_config
    write_result(run_dir / "baseline-model.json", artifact)
    weight_digest = sha256(run_dir / "baseline-model.json")
    baseline_metadata = {
        "model_version": artifact["model_version"],
        "weight_sha256": weight_digest,
        "training_subjects": artifact["train_subjects"],
    }
    oof_by_epoch = {key: row for key, row in zip(eligible, oof, strict=True)}
    all_yasa, yasa_labels = [], []
    for record, imported, psg in jobs:
        identifier = record["recording_id"]
        feature_rows = features_by_record[identifier]
        valid_indices = [i for i, row in enumerate(feature_rows) if row is not None]
        final_p = predict_baseline(artifact, np.array([feature_rows[i] for i in valid_indices]))
        prediction_by_index = dict(zip(valid_indices, final_p, strict=True))
        baseline_rows = []
        for epoch in imported["epochs"]:
            index = epoch["index"]
            row = {
                "epoch_index": index,
                "start_s": epoch["start_s"],
                "duration_s": epoch["duration_s"],
                "predicted_stage": None,
                "stage_probabilities": None,
                "status": "incomplete_tail" if not epoch["complete"] else "invalid_features",
            }
            if index in prediction_by_index:
                prediction = epoch_predictions([prediction_by_index[index]])[0]
                row.update(
                    predicted_stage=prediction["predicted_stage"],
                    stage_probabilities=prediction["stage_probabilities"],
                    status="predicted",
                )
            if (identifier, index) in oof_by_epoch:
                row["development_oof_probabilities"] = dict(
                    zip(STAGES, oof_by_epoch[identifier, index].tolist())
                )
            baseline_rows.append(row)
        write_result(
            run_dir / (identifier + "-baseline.json"),
            {
                "run_id": run_id,
                "source": imported["source"],
                "model": baseline_metadata,
                "prediction_scope": "final model fit on all local training subjects; in-sample predictions",
                "epochs": baseline_rows,
            },
        )
        if with_yasa:
            cache_path = run_dir / (identifier + "-yasa.json")
            if cache_path.exists():
                yasa_run = json.loads(cache_path.read_text())
                if (
                    yasa_run["run_id"] != run_id
                    or yasa_run["source"] != imported["source"]
                    or yasa_run["model"]["weight_sha256"] != source_config["yasa_weight_sha256"]
                ):
                    raise ValueError("Cached YASA provenance mismatch")
                p = np.array(
                    [
                        list(row["stage_probabilities"][s] for s in STAGES)
                        for row in yasa_run["epochs"]
                        if row["status"] == "predicted"
                    ]
                )
                p = probabilities(p, list(STAGES))
                if len(p) != sum(e["complete"] for e in imported["epochs"]):
                    raise ValueError("Cached YASA epoch count mismatch")
            else:
                mapping = imported["mapping"]
                # Use EEG+EOG consistently; the SC EMG channel is a 1Hz envelope.
                p, metadata = infer_yasa(psg, mapping["eeg"], mapping["eog"])
                if metadata["compatibility_warnings"]:
                    LOGGER.warning(
                        "yasa_compatibility_warning recording=%s count=%d; details retained in model metadata",
                        identifier,
                        len(metadata["compatibility_warnings"]),
                    )
                yasa_rows = [{**row, "status": "predicted"} for row in epoch_predictions(p)]
                yasa_rows.extend(
                    {
                        "epoch_index": e["index"],
                        "start_s": e["start_s"],
                        "status": "incomplete_tail",
                        "predicted_stage": None,
                        "stage_probabilities": None,
                    }
                    for e in imported["epochs"]
                    if not e["complete"]
                )
                yasa_run = {
                    "run_id": run_id,
                    "source": imported["source"],
                    "model": metadata,
                    "epochs": yasa_rows,
                }
                write_result(cache_path, yasa_run)
            for epoch in imported["epochs"]:
                if epoch["valid_reference"] and epoch["index"] < len(p):
                    all_yasa.append(p[epoch["index"]])
                    yasa_labels.append(epoch["reference_stage"])
            LOGGER.info("yasa_ready recording=%s", identifier)
    report = {
        "run_id": run_id,
        "provenance": source_config,
        "protocol": "leave-one-subject-out on local TRAIN subjects only; not locked-test evaluation",
        "n_subjects": len(set(subjects)),
        "folds": folds,
        "baseline_oof": classification_metrics(labels, oof),
        "baseline_model": baseline_metadata,
        "yasa_local_reference": classification_metrics(yasa_labels, all_yasa)
        if with_yasa
        else None,
        "limitations": [
            "Small development sample only",
            "Pretraining overlap unresolved",
            "No tuning or calibration on locked test",
            "No review ranking implemented",
        ],
    }
    write_result(run_dir / "metrics.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/audit/manifest.json"))
    parser.add_argument("--imports", type=Path, default=Path("derived/imports"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/models"))
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        report = run_development(args.manifest, args.imports, args.output, not args.baseline_only)
        print(
            json.dumps(
                {
                    "run_id": report["run_id"],
                    "n_subjects": report["n_subjects"],
                    "baseline_oof": report["baseline_oof"],
                    "yasa_local_reference": report["yasa_local_reference"],
                },
                allow_nan=False,
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        LOGGER.error("development_run_failed: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
