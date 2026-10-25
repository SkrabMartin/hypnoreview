"""Register verified development artifacts atomically, outside HTTP requests."""

import argparse
import json
import logging
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.hypnoreview_api.database import (
    AnalysisRun,
    Epoch,
    ModelVersion,
    Prediction,
    Recording,
    SourceAnnotation,
    Study,
    Subject,
    engine_for,
)
from packages.models.cli import development_inputs, digest
from packages.models.contracts import STAGES, probabilities
from tools.audit_sleep_edf import sha256

LOGGER = logging.getLogger("hypnoreview.register")


def register(engine, review_dir: Path, manifest: Path, imports: Path) -> int:
    """One transaction for all five imports; never updates an existing reference/run."""
    jobs = development_inputs(manifest, imports)
    metrics = json.loads((review_dir / "review-metrics.json").read_text())
    config = metrics["configuration"]
    run_id = metrics["run_id"]
    if digest(config) != run_id or config["manifest_sha256"] != sha256(manifest):
        raise ValueError("Review configuration or manifest checksum mismatch")
    count = 0
    with Session(engine) as session, session.begin():
        study_id = "sleep-edfx-1.0.0"
        if session.get(Study, study_id) is None:
            session.add(
                Study(
                    id=study_id,
                    source_url="https://physionet.org/content/sleep-edfx/1.0.0/",
                    license="ODC-By-1.0",
                )
            )
            session.flush()
        for record, imported, path in jobs:
            name, subject = record["recording_id"], record["subject_id"]
            queue = json.loads((review_dir / f"{name}-queue.json").read_text())
            if (
                queue["run_id"] != run_id
                or queue["source"] != imported["source"]
                or queue["model_run_id"] != config["model_run_id"]
            ):
                raise ValueError("Queue source/run provenance mismatch")
            rows = queue["rows"]
            complete = [e["index"] for e in imported["epochs"] if e["complete"]]
            if [r["epoch_index"] for r in rows] != complete:
                raise ValueError("Queue must cover every complete epoch exactly once")
            if subject in queue["ranking_context"]["train_subjects"]:
                raise ValueError("Ranking context overlaps reviewed participant")
            for order in queue["orders"].values():
                if sorted(order) != list(range(len(rows))):
                    raise ValueError("Invalid queue permutation")
            for row in rows:
                p = probabilities([[row["stage_probabilities"][s] for s in STAGES]], list(STAGES))[
                    0
                ]
                if (
                    STAGES[int(p.argmax())] != row["predicted_stage"]
                    or not 0 <= row["review_score"] <= 1
                    or row["start_s"] != 30 * row["epoch_index"]
                ):
                    raise ValueError("Invalid queue prediction or timeline")
            if session.get(Subject, subject) is None:
                session.add(Subject(id=subject, study_id=study_id))
                session.flush()
            existing = session.get(Recording, name)
            provenance = {
                "source": imported["source"],
                "import_sha256": sha256(imports / f"{name}.json"),
            }
            if existing is not None:
                annotation = session.get(SourceAnnotation, name)
                if (
                    existing.sha256 != record["psg_sha256"]
                    or annotation is None
                    or annotation.provenance != provenance
                ):
                    raise ValueError("Existing immutable import differs")
            else:
                session.add(
                    Recording(
                        id=name,
                        subject_id=subject,
                        file_key=str(path.resolve()),
                        sha256=record["psg_sha256"],
                        duration_s=imported["metadata"]["duration_s"],
                        metadata_json=imported["metadata"],
                    )
                )
                session.flush()
                session.add(
                    SourceAnnotation(
                        recording_id=name,
                        provenance=provenance,
                        intervals=imported["source_annotations"],
                    )
                )
                session.add_all(
                    [
                        Epoch(
                            id=f"{name}:{e['index']}",
                            recording_id=name,
                            epoch_index=e["index"],
                            start_s=e["start_s"],
                            duration_s=e["duration_s"],
                            reference_stage=e["reference_stage"],
                            valid_reference=e["valid_reference"],
                            complete=e["complete"],
                            revision=0,
                            final_reviewed_stage=None,
                        )
                        for e in imported["epochs"]
                    ]
                )
                session.flush()
            model_id = digest(queue["primary_model"])
            if session.get(ModelVersion, model_id) is None:
                session.add(ModelVersion(id=model_id, provenance=queue["primary_model"]))
                session.flush()
            analysis_id = f"{name}:{run_id}"
            payload_hash = sha256(review_dir / f"{name}-queue.json")
            previous = session.get(AnalysisRun, analysis_id)
            if previous is not None:
                if previous.provenance["queue_sha256"] != payload_hash:
                    raise ValueError(
                        "Existing analysis differs; immutable predictions cannot be replaced"
                    )
                continue
            session.add(
                AnalysisRun(
                    id=analysis_id,
                    recording_id=name,
                    model_id=model_id,
                    config_hash=run_id,
                    status="SUCCEEDED",
                    provenance={
                        "queue_sha256": payload_hash,
                        "review_configuration": config,
                        "ranking_context": queue["ranking_context"],
                        "import": provenance,
                    },
                )
            )
            session.flush()
            ranks = {
                method: {position: rank for rank, position in enumerate(order)}
                for method, order in queue["orders"].items()
            }
            session.add_all(
                [
                    Prediction(
                        run_id=analysis_id,
                        epoch_id=f"{name}:{row['epoch_index']}",
                        payload={
                            **row,
                            "ranks": {method: rank[i] for method, rank in ranks.items()},
                        },
                    )
                    for i, row in enumerate(rows)
                ]
            )
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-run", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path("data/audit/manifest.json"))
    parser.add_argument("--imports", type=Path, default=Path("derived/imports"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        count = register(engine_for(), args.review_run, args.manifest, args.imports)
        LOGGER.info("registration_succeeded new_runs=%d", count)
        return 0
    except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError):
        LOGGER.error("registration_failed: invalid/missing artifacts or provenance")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
