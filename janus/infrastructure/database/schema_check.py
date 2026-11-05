"""Report (and apply) the Alembic migration state of the JANUS database.

Used by CI / startup health checks so a stale schema fails loudly instead of
producing confusing runtime errors (Phase 0 spec s10: "apply migrations").

Command line::

    uv run python -m janus.infrastructure.database.schema_check
    uv run python -m janus.infrastructure.database.schema_check --database-url sqlite:///tmp/x.db

Exit code is ``0`` only when the database is at ``head``.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from janus.domain.common.errors import ConfigurationError
from janus.infrastructure.database.engine import (
    DEFAULT_DATABASE_URL,
    create_engine_from_url,
)
from sqlalchemy.engine import Engine

__all__ = [
    "JANUS_DB_URL_ENV",
    "SchemaStatus",
    "alembic_config",
    "assert_schema_current",
    "check_schema",
    "current_revision",
    "find_alembic_ini",
    "head_revision",
    "main",
    "upgrade_to_head",
]

JANUS_DB_URL_ENV = "JANUS_DB_URL"
"""Environment variable that overrides ``sqlalchemy.url`` for Alembic."""

ALEMBIC_INI_FILENAME = "alembic.ini"
_MIGRATIONS_DIRNAME = "migrations"


def find_alembic_ini() -> Path:
    """Locate ``alembic.ini`` from the CWD or this module's repository root."""
    candidates: list[Path] = [Path.cwd(), *Path(__file__).resolve().parents]
    for start in candidates:
        candidate = start / ALEMBIC_INI_FILENAME
        if candidate.is_file():
            return candidate
    raise ConfigurationError(
        "alembic.ini was not found; run from the repository root or pass a config",
        searched=[str(candidate) for candidate in candidates],
    )


def alembic_config(database_url: str | None = None) -> Config:
    """Build an Alembic config with an explicit, env-var-aware database URL."""
    ini_path = find_alembic_ini()
    config = Config(str(ini_path))
    repo_root = ini_path.parent
    config.set_main_option("script_location", str(repo_root / _MIGRATIONS_DIRNAME))
    url = database_url or os.environ.get(JANUS_DB_URL_ENV) or DEFAULT_DATABASE_URL
    # ConfigParser interpolation: a literal ``%`` must be escaped.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _resolved_url(config: Config) -> str:
    configured = config.get_main_option("sqlalchemy.url")
    return configured or DEFAULT_DATABASE_URL


def current_revision(engine: Engine) -> str | None:
    """The revision recorded in ``alembic_version`` (``None`` when unversioned)."""
    with engine.connect() as connection:
        context = MigrationContext.configure(connection=connection)
        revision: str | None = context.get_current_revision()
        return revision


def head_revision(config: Config) -> str | None:
    """The latest revision available in the local migration scripts."""
    script = ScriptDirectory.from_config(config)
    head: str | None = script.get_current_head()
    return head


@dataclass(frozen=True, slots=True)
class SchemaStatus:
    """Snapshot of a database's migration state."""

    database_url: str
    current_revision: str | None
    head_revision: str | None

    @property
    def is_current(self) -> bool:
        """True when the database is migrated to the latest known revision."""
        return self.current_revision is not None and self.current_revision == self.head_revision

    def describe(self) -> str:
        """Human-readable one-block report for logs and CI output."""
        current = self.current_revision or "<none>"
        head = self.head_revision or "<none>"
        state = "up to date" if self.is_current else "PENDING MIGRATIONS"
        return (
            f"database: {self.database_url}\n"
            f"current revision: {current}\n"
            f"head revision: {head}\n"
            f"schema: {state}"
        )


def check_schema(database_url: str | None = None) -> SchemaStatus:
    """Compare the database's current revision with the script head."""
    config = alembic_config(database_url)
    head = head_revision(config)
    url = _resolved_url(config)
    engine = create_engine_from_url(url)
    try:
        current = current_revision(engine)
    finally:
        engine.dispose()
    return SchemaStatus(database_url=url, current_revision=current, head_revision=head)


def assert_schema_current(database_url: str | None = None) -> SchemaStatus:
    """Raise :class:`ConfigurationError` unless the database is at ``head``."""
    status = check_schema(database_url)
    if not status.is_current:
        raise ConfigurationError(
            "database schema is not migrated to head; run 'alembic upgrade head'",
            current_revision=status.current_revision or "<none>",
            head_revision=status.head_revision or "<none>",
        )
    return status


def upgrade_to_head(database_url: str | None = None) -> None:
    """Apply every migration up to ``head`` (programmatic ``alembic upgrade``)."""
    config = alembic_config(database_url)
    command.upgrade(config, "head")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns ``0`` only when the schema is current."""
    parser = argparse.ArgumentParser(
        prog="python -m janus.infrastructure.database.schema_check",
        description="Report whether the JANUS database schema is at Alembic head.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=f"SQLAlchemy URL (default: ${JANUS_DB_URL_ENV} or {DEFAULT_DATABASE_URL})",
    )
    args = parser.parse_args(argv)
    status = check_schema(args.database_url)
    print(status.describe())
    return 0 if status.is_current else 1


if __name__ == "__main__":
    raise SystemExit(main())
