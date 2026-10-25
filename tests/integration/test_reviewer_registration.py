"""Artifact ingestion is idempotent, immutable and all-or-nothing."""

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.hypnoreview_api.database import AnalysisRun, Base, Recording, engine_for
from apps.api.hypnoreview_api.ingest import register
from packages.models.cli import digest
from tools.audit_sleep_edf import sha256


@pytest.fixture
def registration(tmp_path, monkeypatch):
    engine = engine_for(f"sqlite:///{tmp_path / 'registration.sqlite3'}")
    Base.metadata.create_all(engine)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    config = {"manifest_sha256": sha256(manifest), "model_run_id": "model-run"}
    run = digest(config)
    (tmp_path / "review-metrics.json").write_text(
        json.dumps({"configuration": config, "run_id": run})
    )
    jobs = []
    for name in ("night1", "night2"):
        source = {"psg_sha256": "a" * 64}
        imported = {
            "source": source,
            "source_annotations": [],
            "metadata": {
                "duration_s": 30,
                "channels": [],
                "suggested_mapping": {"eeg": "EEG Fpz-Cz"},
            },
            "epochs": [
                {
                    "index": 0,
                    "complete": True,
                    "start_s": 0,
                    "duration_s": 30,
                    "reference_stage": "N2",
                    "valid_reference": True,
                }
            ],
        }
        (tmp_path / f"{name}.json").write_text(json.dumps(imported))
        row = {
            "epoch_index": 0,
            "start_s": 0,
            "predicted_stage": "N2",
            "stage_probabilities": {"WAKE": 0, "N1": 0, "N2": 1, "N3": 0, "REM": 0},
            "review_score": 0.5,
        }
        queue = {
            "run_id": run,
            "model_run_id": "model-run",
            "source": source,
            "rows": [row],
            "ranking_context": {"train_subjects": ["other"]},
            "orders": {"strict_priority": [0], "coverage_aware": [0]},
            "primary_model": {"version": "fixture"},
        }
        (tmp_path / f"{name}-queue.json").write_text(json.dumps(queue))
        jobs.append(
            (
                {"recording_id": name, "subject_id": "subject", "psg_sha256": "a" * 64},
                imported,
                tmp_path / "test.edf",
            )
        )
    monkeypatch.setattr("apps.api.hypnoreview_api.ingest.development_inputs", lambda *_: jobs)
    yield engine, tmp_path, manifest
    engine.dispose()


def test_idempotent_and_refuses_replacing_prediction(registration):
    engine, root, manifest = registration
    assert register(engine, root, manifest, root) == 2
    assert register(engine, root, manifest, root) == 0
    path = root / "night1-queue.json"
    q = json.loads(path.read_text())
    q["rows"][0]["review_score"] = 0.7
    path.write_text(json.dumps(q))
    with pytest.raises(ValueError, match="immutable predictions"):
        register(engine, root, manifest, root)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(AnalysisRun)) == 2


def test_late_invalid_queue_rolls_back_entire_batch(registration):
    engine, root, manifest = registration
    path = root / "night2-queue.json"
    q = json.loads(path.read_text())
    q["orders"]["strict_priority"] = []
    path.write_text(json.dumps(q))
    with pytest.raises(ValueError, match="permutation"):
        register(engine, root, manifest, root)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Recording)) == 0


def test_manifest_and_existing_source_protection(registration):
    engine, root, manifest = registration
    assert register(engine, root, manifest, root) == 2
    p = root / "night1.json"
    p.write_text(p.read_text() + " ")
    with pytest.raises(ValueError, match="immutable import"):
        register(engine, root, manifest, root)
    manifest.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="checksum"):
        register(engine, root, manifest, root)
