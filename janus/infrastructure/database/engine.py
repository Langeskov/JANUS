"""Engine / session-factory construction with SQLite durability pragmas.

Phase 0 spec s10: "Enable foreign keys and WAL where appropriate, apply
migrations, and maintain backups before schema changes."

Every connection created from a SQLite URL gets:

=============== ===========================================================
``foreign_keys=ON``       Foreign keys are OFF by default in SQLite; without
                          this the ``ON DELETE RESTRICT`` integrity rules are
                          decorative.
``journal_mode=WAL``      Readers do not block the writer (single-user app,
                          periodic CLI cycles).
``busy_timeout=5000``     Bounded retry window instead of an instant
                          ``database is locked`` failure.
``synchronous=NORMAL``    Safe with WAL; avoids an fsync per commit.
=============== ===========================================================

In-memory SQLite URLs use :class:`~sqlalchemy.pool.StaticPool` so every session
in one process sees the same in-memory database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

__all__ = [
    "DEFAULT_DATABASE_URL",
    "Database",
    "create_engine_from_url",
    "ensure_sqlite_parent_directory",
    "is_memory_url",
]

DEFAULT_DATABASE_URL = "sqlite:///data/local/janus.db"
"""Development default; ``JANUS_DB_URL`` / CLI config overrides it."""

SQLITE_BUSY_TIMEOUT_MS = 5000
"""How long SQLite waits on a lock before failing."""

_MEMORY_URLS = frozenset(
    {
        "sqlite://",
        "sqlite+pysqlite://",
        "sqlite:///:memory:",
        "sqlite+pysqlite:///:memory:",
    }
)


def is_memory_url(url: str) -> bool:
    """True for an in-memory SQLite URL (which needs a shared ``StaticPool``)."""
    return url in _MEMORY_URLS or ":memory:" in url


def ensure_sqlite_parent_directory(url: str) -> None:
    """Create the parent directory of a file-backed SQLite database.

    SQLite cannot create missing parent directories itself, so
    ``sqlite:///data/local/janus.db`` would fail on a fresh checkout.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    path = url[len(prefix) :]
    if not path or path == ":memory:" or path.startswith("file:"):
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """Apply JANUS's SQLite safety/durability pragmas to a new connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_engine_from_url(url: str, *, echo: bool = False) -> Engine:
    """Build an :class:`~sqlalchemy.engine.Engine` for ``url``.

    SQLite URLs get the pragmas above and a shared static pool when in-memory;
    any other dialect (e.g. a future PostgreSQL deployment) is built plainly so
    the port boundary really is storage-agnostic.
    """
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            echo=echo,
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if is_memory_url(url) else None,
        )
        event.listen(engine, "connect", _sqlite_pragmas)
        return engine
    return create_engine(url, echo=echo, future=True)


class Database:
    """An engine plus the session factory bound to it.

    This is the single object the application composition root needs::

        database = Database(database_url)
        database.create_all()          # tests / local bootstrap
        # or: database.upgrade_to_head()  # use the Alembic migration path
        uow_factory = SqlAlchemyUnitOfWorkFactory(database)
    """

    def __init__(self, url_or_engine: str | Engine, *, echo: bool = False) -> None:
        if isinstance(url_or_engine, Engine):
            self._engine = url_or_engine
        else:
            ensure_sqlite_parent_directory(url_or_engine)
            self._engine = create_engine_from_url(url_or_engine, echo=echo)
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
        )

    @classmethod
    def from_url(cls, url: str, *, echo: bool = False) -> Database:
        """Convenience constructor mirroring :func:`create_engine_from_url`."""
        return cls(url, echo=echo)

    @property
    def engine(self) -> Engine:
        """The underlying SQLAlchemy engine."""
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """Session factory used by the unit of work."""
        return self._session_factory

    @property
    def url(self) -> str:
        """The connection URL (never contains credentials in Phase 0)."""
        return self._engine.url.render_as_string(hide_password=False)

    def create_all(self) -> None:
        """Create the schema from metadata (tests / fixture bootstrap only)."""
        from janus.infrastructure.database.tables import Base

        Base.metadata.create_all(self._engine)

    def drop_all(self) -> None:
        """Drop all known tables (test teardown only; never used at runtime)."""
        from janus.infrastructure.database.tables import Base

        Base.metadata.drop_all(self._engine)

    def upgrade_to_head(self) -> None:
        """Apply every Alembic migration up to ``head``."""
        from janus.infrastructure.database.schema_check import upgrade_to_head

        upgrade_to_head(self.url)

    def dispose(self) -> None:
        """Release pooled connections (call on app shutdown)."""
        self._engine.dispose()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.dispose()
