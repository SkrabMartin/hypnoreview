"""Relational storage; signals remain outside the database."""

import os
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def engine_for(url: str | None = None):
    url = url or os.environ.get("HYPNOREVIEW_DATABASE_URL", "sqlite:///artifacts/reviewer.sqlite3")
    engine = create_engine(
        url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}
    )
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


class Base(DeclarativeBase):
    pass


class Study(Base):
    __tablename__ = "studies"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_url: Mapped[str]
    license: Mapped[str]


class Subject(Base):
    __tablename__ = "subjects"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("studies.id"))


class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("subjects.id"))
    file_key: Mapped[str]
    sha256: Mapped[str] = mapped_column(String(64))
    duration_s: Mapped[float]
    metadata_json: Mapped[dict] = mapped_column(JSON)
    imported_at: Mapped[str] = mapped_column(default=now)


class SourceAnnotation(Base):
    __tablename__ = "source_annotations"
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"), primary_key=True)
    provenance: Mapped[dict] = mapped_column(JSON)
    intervals: Mapped[list] = mapped_column(JSON)


class Epoch(Base):
    __tablename__ = "epochs"
    __table_args__ = (
        UniqueConstraint("recording_id", "epoch_index"),
        CheckConstraint("revision >= 0"),
    )
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"), index=True)
    epoch_index: Mapped[int]
    start_s: Mapped[float]
    duration_s: Mapped[float]
    reference_stage: Mapped[str | None]
    valid_reference: Mapped[bool]
    complete: Mapped[bool]
    revision: Mapped[int] = mapped_column(default=0)
    final_reviewed_stage: Mapped[str | None]


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provenance: Mapped[dict] = mapped_column(JSON)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"), index=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    config_hash: Mapped[str]
    status: Mapped[str]
    provenance: Mapped[dict] = mapped_column(JSON)
    finished_at: Mapped[str] = mapped_column(default=now)


class Prediction(Base):
    __tablename__ = "predictions"
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), primary_key=True)
    epoch_id: Mapped[str] = mapped_column(ForeignKey("epochs.id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("epoch_id", "revision"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    epoch_id: Mapped[str] = mapped_column(ForeignKey("epochs.id"), index=True)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"))
    reviewer_id: Mapped[str]
    stage: Mapped[str]
    note: Mapped[str]
    revision: Mapped[int]
    created_at: Mapped[str] = mapped_column(default=now)
