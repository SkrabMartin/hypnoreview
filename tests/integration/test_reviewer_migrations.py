"""The frozen migration can upgrade, match the ORM and recreate a clean schema."""

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from apps.api.hypnoreview_api.database import engine_for


def test_migration_round_trip(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migration.sqlite3'}"
    monkeypatch.setenv("HYPNOREVIEW_DATABASE_URL", url)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    command.check(cfg)
    engine = engine_for(url)
    assert "reviews" in inspect(engine).get_table_names()
    command.downgrade(cfg, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    command.upgrade(cfg, "head")
    assert "predictions" in inspect(engine).get_table_names()
    engine.dispose()
