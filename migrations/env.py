"""Alembic environment for the JANUS SQLite schema.

The database URL resolution order is:

1. ``JANUS_DB_URL`` environment variable (CI, tests, deployments);
2. ``sqlalchemy.url`` from ``alembic.ini`` (development default).

Migrations run through :func:`janus.infrastructure.database.engine.create_engine_from_url`
so migrated connections get the same ``foreign_keys=ON`` / WAL pragmas as the
application. ``render_as_batch=True`` keeps future SQLite ``ALTER`` migrations
possible, and ``compare_type=True`` makes ``alembic check`` meaningful.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from janus.infrastructure.database.engine import (
    DEFAULT_DATABASE_URL,
    create_engine_from_url,
    ensure_sqlite_parent_directory,
)
from janus.infrastructure.database.tables import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

JANUS_DB_URL_ENV = "JANUS_DB_URL"


def _database_url() -> str:
    """Resolve the migration target URL (env var wins over alembic.ini)."""
    return (
        os.environ.get(JANUS_DB_URL_ENV)
        or config.get_main_option("sqlalchemy.url")
        or DEFAULT_DATABASE_URL
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (``alembic upgrade --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the configured database."""
    url = _database_url()
    ensure_sqlite_parent_directory(url)
    engine = create_engine_from_url(url)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
