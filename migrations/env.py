"""Migrations use the same explicit database URL as the application."""

from alembic import context

from apps.api.hypnoreview_api.database import Base, engine_for

with engine_for().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
