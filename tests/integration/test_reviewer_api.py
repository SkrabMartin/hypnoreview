"""Durability, conflicts, blindness, bounded signals and exports through real HTTP contracts."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_edf_import import write_edf

from apps.api.hypnoreview_api.database import (
    AnalysisRun,
    Base,
    Epoch,
    ModelVersion,
    Prediction,
    Recording,
    Review,
    SourceAnnotation,
    Study,
    Subject,
    engine_for,
)
from apps.api.hypnoreview_api.main import create_app


@pytest.fixture
def reviewer(tmp_path):
    # Optional dedicated PostgreSQL database exercises exactly the same contracts.
    engine = engine_for(
        os.environ.get("HYPNOREVIEW_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'api.sqlite3'}")
    )
    if engine.dialect.name == "postgresql" and not engine.url.database.endswith("_test"):
        raise ValueError("PostgreSQL tests require an explicitly dedicated *_test database")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed_recording(engine, tmp_path)
    client = TestClient(create_app(engine))
    yield client, engine
    client.close()
    engine.dispose()


def seed_recording(engine, tmp_path):
    path = tmp_path / "signal.edf"
    write_edf(path)
    with Session(engine) as s, s.begin():
        s.add(Study(id="test-study", source_url="synthetic", license="test-fixture"))
        s.flush()
        s.add(Subject(id="test-subject", study_id="test-study"))
        s.flush()
        s.add(
            Recording(
                id="night",
                subject_id="test-subject",
                file_key=str(path),
                sha256="0" * 64,
                duration_s=65,
                metadata_json={"channels": [], "suggested_mapping": {"eeg": "EEG Fpz-Cz"}},
            )
        )
        s.flush()
        s.add(
            SourceAnnotation(
                recording_id="night",
                provenance={"original": True},
                intervals=[{"label": "Sleep stage 2"}],
            )
        )
        s.add(ModelVersion(id="model", provenance={"version": "synthetic"}))
        s.flush()
        s.add(
            AnalysisRun(
                id="run",
                recording_id="night",
                model_id="model",
                config_hash="config",
                status="SUCCEEDED",
                provenance={"import": {"sha256": "0" * 64}},
            )
        )
        s.add_all(
            [
                Epoch(
                    id=f"night:{i}",
                    recording_id="night",
                    epoch_index=i,
                    start_s=i * 30,
                    duration_s=30 if i < 2 else 5,
                    reference_stage="N2",
                    valid_reference=i < 2,
                    complete=i < 2,
                    revision=0,
                    final_reviewed_stage=None,
                )
                for i in range(3)
            ]
        )
        s.flush()
        for i in range(2):
            s.add(
                Prediction(
                    run_id="run",
                    epoch_id=f"night:{i}",
                    payload={
                        "predicted_stage": "N1",
                        "stage_probabilities": {
                            "WAKE": 0.1,
                            "N1": 0.6,
                            "N2": 0.1,
                            "N3": 0.1,
                            "REM": 0.1,
                        },
                        "review_score": 0.2 + i * 0.5,
                        "review_reasons": [],
                        "explanation": "N1=0.6000; WAKE=0.1000 (synthetic fixture)",
                        "ranks": {"strict_priority": 1 - i, "coverage_aware": i},
                    },
                )
            )


def payload(revision=0, stage="N3"):
    return {
        "stage": stage,
        "note": "observed correction",
        "expected_revision": revision,
        "analysis_run_id": "run",
    }


def test_pagination_order_filters_and_blindness(reviewer):
    c, _ = reviewer
    page = c.get("/api/v1/recordings/night/epochs?order=strict_priority&limit=1").json()
    assert page["total"] == 3 and page["items"][0]["epoch_index"] == 1
    assert "reference_stage" not in page["items"][0]
    assert c.get("/api/v1/recordings/night/epochs?min_risk=.5").json()["total"] == 1
    assert (
        c.get("/api/v1/recordings/night/epochs?offset=2").json()["items"][0]["prediction"] is None
    )
    assert c.get("/api/v1/recordings/night/epochs?limit=501").status_code == 422
    assert c.get("/api/v1/recordings/night/epochs?order=evil").status_code == 422
    assert c.get("/api/v1/recordings/missing").status_code == 404
    assert "file_key" not in c.get("/api/v1/recordings/night").json()


def test_persistent_audit_conflict_restore_and_snapshot(reviewer):
    c, engine = reviewer
    assert c.patch("/api/v1/epochs/night:0/review", json=payload()).status_code == 200
    assert c.patch("/api/v1/epochs/night:0/review", json=payload()).status_code == 409
    assert c.patch("/api/v1/epochs/night:0/review", json=payload(1, "N1")).status_code == 200
    with TestClient(create_app(engine)) as restarted:
        history = restarted.get("/api/v1/epochs/night:0/history").json()["items"]
        assert [h["stage"] for h in history] == ["N3", "N1"]
        snapshot = restarted.post("/api/v1/recordings/night/exports").json()
        epoch = snapshot["epochs"][0]
        assert epoch["revision"] == 2 and epoch["final_reviewed_stage"] == "N1"
        assert epoch["prediction"]["predicted_stage"] == "N1"
        assert "reference_stage" not in epoch
        assert snapshot["analysis_run_id"] == "run"
        csv = restarted.post("/api/v1/recordings/night/exports?format=csv")
        assert csv.status_code == 200 and "effective_stage" in csv.text
    with Session(engine) as s:
        assert s.get(Epoch, "night:0").reference_stage == "N2"
        assert s.get(SourceAnnotation, "night").intervals == [{"label": "Sleep stage 2"}]
        assert len(s.scalars(select(Review)).all()) == 2
    assert c.get("/api/v1/recordings/night/epochs?pending=true").json()["total"] == 2


def test_simultaneous_writes_only_one_wins(reviewer):
    c, engine = reviewer

    def save(stage):
        with TestClient(create_app(engine)) as other:
            return other.patch(
                "/api/v1/epochs/night:1/review", json=payload(stage=stage)
            ).status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(save, ["N2", "REM"])) == [200, 409]
    assert len(c.get("/api/v1/epochs/night:1/history").json()["items"]) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"stage": "BAD"},
        {"expected_revision": -1},
        {"expected_revision": True},
        {"note": "x" * 1001},
        {"analysis_run_id": "missing"},
        {"unexpected": 1},
    ],
)
def test_invalid_review_rejected(reviewer, changes):
    c, _ = reviewer
    assert (
        c.patch("/api/v1/epochs/night:0/review", json={**payload(), **changes}).status_code == 422
    )
    assert c.get("/api/v1/epochs/night:0/history").json()["items"] == []


def test_local_access_and_untrusted_origin(reviewer):
    c, _ = reviewer
    assert c.get("/api/v1/recordings", headers={"host": "attacker.example"}).status_code == 400
    assert (
        c.patch(
            "/api/v1/epochs/night:0/review",
            json=payload(),
            headers={"origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    assert c.get("/api/v1/health").headers["cache-control"] == "no-store"


def test_signal_tail_envelope_native_units_and_psd(reviewer):
    c, _ = reviewer
    data = c.get(
        "/api/v1/recordings/night/signal",
        params={
            "start_s": 60.005,
            "duration_s": 20,
            "channels": "EEG Fpz-Cz",
            "target_points": 100,
        },
    ).json()
    assert data["duration_s"] == pytest.approx(4.995)
    row = data["channels"][0]
    assert row["start_s"] == 60.01 and len(row["min_uv"]) == 100
    assert row["native_rate_hz"] == 100 and row["unit"] == "uV"
    assert min(row["min_uv"]) < 0 < max(row["max_uv"])
    assert (
        c.get("/api/v1/recordings/night/signal?start_s=65&channels=EEG%20Fpz-Cz").status_code == 422
    )
    assert (
        c.get("/api/v1/recordings/night/signal?duration_s=151&channels=EEG%20Fpz-Cz").status_code
        == 422
    )
    assert c.get("/api/v1/recordings/night/signal?channels=../secret").status_code == 422
    response = c.get("/api/v1/recordings/night/spectrogram?channels=EEG%20Fpz-Cz").json()[
        "channels"
    ][0]
    assert max(response["frequencies_hz"]) <= 35
    assert len(response["power_db_uv2_hz"]) == len(response["frequencies_hz"])
    assert response["times_s"][0] >= 0


def test_reference_visibility_is_server_configuration(reviewer):
    _, engine = reviewer
    with TestClient(create_app(engine, reveal_references=True)) as c:
        assert (
            c.get("/api/v1/recordings/night/epochs").json()["items"][0]["reference_stage"] == "N2"
        )


def test_missing_migrations_return_redacted_error(tmp_path):
    engine = engine_for(f"sqlite:///{tmp_path / 'private-name.sqlite3'}")
    with TestClient(create_app(engine)) as client:
        response = client.get("/api/v1/recordings")
        assert response.status_code == 503
        assert len(response.json()["diagnostic_id"]) == 32
        assert "private-name" not in response.text and "SELECT" not in response.text
    engine.dispose()
