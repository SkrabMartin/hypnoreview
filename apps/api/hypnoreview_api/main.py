"""Loopback-only single-user reviewer. Run migrations and CLI registration first."""

import csv
import io
import json
import logging
import os
import uuid
from enum import Enum
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from apps.api.hypnoreview_api.database import (
    AnalysisRun,
    Epoch,
    Prediction,
    Recording,
    Review,
    engine_for,
)
from apps.api.hypnoreview_api.signals import window

LOGGER = logging.getLogger("hypnoreview.api")


class Stage(str, Enum):
    WAKE = "WAKE"
    N1 = "N1"
    N2 = "N2"
    N3 = "N3"
    REM = "REM"
    UNSCORED = "UNSCORED"
    UNSCORABLE = "UNSCORABLE"


class ReviewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Stage
    note: str = Field(default="", max_length=1000)
    expected_revision: int = Field(ge=0, strict=True)
    analysis_run_id: str = Field(min_length=1, max_length=180)


def recording_data(r: Recording) -> dict:
    # Explicit allowlist; never expose local file paths or EDF patient/date fields.
    return {
        "id": r.id,
        "subject_id": r.subject_id,
        "sha256": r.sha256,
        "duration_s": r.duration_s,
        "channels": r.metadata_json["channels"],
        "suggested_mapping": r.metadata_json["suggested_mapping"],
    }


def find_run(session: Session, recording_id: str, run_id: str | None) -> AnalysisRun:
    query = select(AnalysisRun).where(AnalysisRun.recording_id == recording_id)
    if run_id:
        query = query.where(AnalysisRun.id == run_id)
    run = session.scalars(query.order_by(AnalysisRun.finished_at.desc(), AnalysisRun.id)).first()
    if run is None:
        raise HTTPException(404, "Analysis not found")
    return run


def epoch_data(e: Epoch, p: Prediction | None, reveal: bool) -> dict:
    result = {
        "epoch_id": e.id,
        "epoch_index": e.epoch_index,
        "start_s": e.start_s,
        "duration_s": e.duration_s,
        "complete": e.complete,
        "revision": e.revision,
        "final_reviewed_stage": e.final_reviewed_stage,
        "review_status": "REVIEWED" if e.revision else "PENDING",
        "prediction": p.payload if p else None,
    }
    if reveal:
        result.update(reference_stage=e.reference_stage, valid_reference=e.valid_reference)
    return result


def create_app(engine=None, reveal_references: bool | None = None) -> FastAPI:
    engine = engine if engine is not None else engine_for()
    reveal = (
        reveal_references
        if reveal_references is not None
        else os.environ.get("HYPNOREVIEW_SHOW_REFERENCES") == "1"
    )
    app = FastAPI(title="HypnoReview — local research reviewer", version="0.5.0")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        diagnostic = uuid.uuid4().hex
        LOGGER.error(
            "database_operation_failed diagnostic=%s type=%s", diagnostic, type(error).__name__
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Database unavailable; check migrations and connection",
                "diagnostic_id": diagnostic,
            },
        )

    allowed_origins = {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    }
    if os.environ.get("HYPNOREVIEW_WEB_ORIGIN"):
        allowed_origins.add(os.environ["HYPNOREVIEW_WEB_ORIGIN"])
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            return Response("Local access only", status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            return Response("Untrusted origin", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/v1/health")
    def health():
        with engine.connect() as connection:
            connection.execute(select(1))
        return {"status": "ok", "mode": "local-single-user", "references_visible": reveal}

    @app.get("/api/v1/recordings")
    def recordings(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        with Session(engine) as s:
            rows = s.scalars(
                select(Recording).order_by(Recording.id).offset(offset).limit(limit)
            ).all()
            return {
                "items": [recording_data(r) for r in rows],
                "total": s.scalar(select(func.count()).select_from(Recording)),
                "offset": offset,
                "limit": limit,
            }

    @app.get("/api/v1/recordings/{recording_id}")
    def recording(recording_id: str):
        with Session(engine) as s:
            r = s.get(Recording, recording_id)
            if r is None:
                raise HTTPException(404, "Recording not found")
            runs = s.scalars(
                select(AnalysisRun)
                .where(AnalysisRun.recording_id == recording_id)
                .order_by(AnalysisRun.finished_at.desc())
            ).all()
            return {
                **recording_data(r),
                "analysis_runs": [
                    {
                        "id": run.id,
                        "status": run.status,
                        "model_id": run.model_id,
                        "config_hash": run.config_hash,
                    }
                    for run in runs
                ],
            }

    @app.get("/api/v1/analysis-runs/{run_id}")
    def analysis(run_id: str):
        with Session(engine) as s:
            run = s.get(AnalysisRun, run_id)
            if run is None:
                raise HTTPException(404, "Analysis not found")
            return {
                "id": run.id,
                "recording_id": run.recording_id,
                "status": run.status,
                "model_id": run.model_id,
                "config_hash": run.config_hash,
                "provenance": run.provenance,
            }

    @app.get("/api/v1/recordings/{recording_id}/epochs")
    def epochs(
        recording_id: str,
        run_id: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        order: str = Query("timeline", pattern="^(timeline|strict_priority|coverage_aware)$"),
        stage: Stage | None = None,
        min_risk: float = Query(0, ge=0, le=1),
        pending: bool = False,
    ):
        with Session(engine) as s:
            run = find_run(s, recording_id, run_id)
            q = (
                select(Epoch, Prediction)
                .outerjoin(
                    Prediction, (Prediction.epoch_id == Epoch.id) & (Prediction.run_id == run.id)
                )
                .where(Epoch.recording_id == recording_id)
            )
            if stage:
                q = q.where(Prediction.payload["predicted_stage"].as_string() == stage.value)
            if min_risk > 0:
                q = q.where(Prediction.payload["review_score"].as_float() >= min_risk)
            if pending:
                q = q.where(Epoch.revision == 0)
            total = s.scalar(select(func.count()).select_from(q.subquery()))
            if order != "timeline":
                q = q.order_by(Prediction.payload["ranks"][order].as_integer().asc().nulls_last())
            rows = s.execute(q.order_by(Epoch.epoch_index).offset(offset).limit(limit)).all()
            return {
                "items": [epoch_data(e, p, reveal) for e, p in rows],
                "total": total,
                "offset": offset,
                "limit": limit,
                "analysis_run_id": run.id,
            }

    @app.get("/api/v1/epochs/{epoch_id}/history")
    def history(epoch_id: str):
        with Session(engine) as s:
            if s.get(Epoch, epoch_id) is None:
                raise HTTPException(404, "Epoch not found")
            rows = s.scalars(
                select(Review).where(Review.epoch_id == epoch_id).order_by(Review.revision)
            ).all()
            return {
                "items": [
                    {
                        "revision": r.revision,
                        "stage": r.stage,
                        "note": r.note,
                        "reviewer_id": r.reviewer_id,
                        "analysis_run_id": r.analysis_run_id,
                        "created_at": r.created_at,
                    }
                    for r in rows
                ]
            }

    @app.patch("/api/v1/epochs/{epoch_id}/review")
    def review(epoch_id: str, payload: ReviewPatch):
        with Session(engine) as s, s.begin():
            e = s.get(Epoch, epoch_id)
            if e is None:
                raise HTTPException(404, "Epoch not found")
            run = s.get(AnalysisRun, payload.analysis_run_id)
            if run is None or run.recording_id != e.recording_id:
                raise HTTPException(422, "Analysis does not belong to epoch")
            updated = s.execute(
                update(Epoch)
                .where(Epoch.id == epoch_id, Epoch.revision == payload.expected_revision)
                .values(revision=Epoch.revision + 1, final_reviewed_stage=payload.stage.value)
                .returning(Epoch.revision)
            ).scalar_one_or_none()
            if updated is None:
                raise HTTPException(409, "Revision conflict; reload current epoch before saving")
            s.add(
                Review(
                    epoch_id=epoch_id,
                    analysis_run_id=run.id,
                    reviewer_id="local-owner",
                    stage=payload.stage.value,
                    note=payload.note,
                    revision=updated,
                )
            )
        LOGGER.info("review_saved epoch=%s revision=%d", epoch_id, updated)
        return {
            "epoch_id": epoch_id,
            "revision": updated,
            "final_reviewed_stage": payload.stage.value,
        }

    def read_window(
        recording_id: str,
        start_s: float,
        duration_s: float,
        channels: str,
        target_points: int,
        spectral: bool,
    ):
        with Session(engine) as s:
            r = s.get(Recording, recording_id)
            if r is None:
                raise HTTPException(404, "Recording not found")
            path = Path(r.file_key)
        try:
            return window(path, start_s, duration_s, channels.split(","), target_points, spectral)
        except (ValueError, OSError):
            raise HTTPException(422, "Invalid/unavailable signal window or channel") from None

    @app.get("/api/v1/recordings/{recording_id}/signal")
    def signal(
        recording_id: str,
        channels: str = Query(min_length=1, max_length=200),
        start_s: float = Query(0, ge=0, allow_inf_nan=False),
        duration_s: float = Query(30, gt=0, le=150, allow_inf_nan=False),
        target_points: int = Query(1000, ge=100, le=3000),
    ):
        return read_window(recording_id, start_s, duration_s, channels, target_points, False)

    @app.get("/api/v1/recordings/{recording_id}/spectrogram")
    def spectrum(
        recording_id: str,
        channels: str = Query(min_length=1, max_length=200),
        start_s: float = Query(0, ge=0, allow_inf_nan=False),
        duration_s: float = Query(30, ge=2, le=150, allow_inf_nan=False),
    ):
        return read_window(recording_id, start_s, duration_s, channels, 100, True)

    @app.post("/api/v1/recordings/{recording_id}/exports")
    def export(
        recording_id: str,
        run_id: str | None = None,
        format: str = Query("json", pattern="^(json|csv)$"),
    ):
        with Session(engine) as s:
            run = find_run(s, recording_id, run_id)
            # A single joined statement observes one consistent review snapshot.
            rows = s.execute(
                select(Epoch, Prediction)
                .outerjoin(
                    Prediction, (Prediction.epoch_id == Epoch.id) & (Prediction.run_id == run.id)
                )
                .where(Epoch.recording_id == recording_id)
                .order_by(Epoch.epoch_index)
            ).all()
            items = [epoch_data(e, p, reveal) for e, p in rows]
            if format == "json":
                body = json.dumps(
                    {
                        "schema_version": 1,
                        "recording_id": recording_id,
                        "analysis_run_id": run.id,
                        "model_id": run.model_id,
                        "config_hash": run.config_hash,
                        "source": run.provenance["import"],
                        "epochs": items,
                    },
                    allow_nan=False,
                )
                media = "application/json"
            else:
                stream = io.StringIO(newline="")
                writer = csv.writer(stream)
                writer.writerow(
                    [
                        "epoch_id",
                        "start_s",
                        "duration_s",
                        "predicted_stage",
                        "reviewed_stage",
                        "effective_stage",
                        "revision",
                        "analysis_run_id",
                        "review_score",
                        "probabilities_json",
                    ]
                )
                for e in items:
                    p = e["prediction"] or {}
                    writer.writerow(
                        [
                            e["epoch_id"],
                            e["start_s"],
                            e["duration_s"],
                            p.get("predicted_stage"),
                            e["final_reviewed_stage"],
                            e["final_reviewed_stage"] or p.get("predicted_stage"),
                            e["revision"],
                            run.id,
                            p.get("review_score"),
                            json.dumps(p.get("stage_probabilities")),
                        ]
                    )
                body, media = stream.getvalue(), "text/csv"
            return Response(
                body,
                media_type=media,
                headers={
                    "Content-Disposition": f'attachment; filename="hypnoreview-export.{format}"'
                },
            )

    return app


app = create_app()
