"""Fixtures for the persistence integration tests.

Self-contained on purpose: these fixtures live in ``tests/integration`` so they
cannot collide with a repository-wide ``tests/conftest.py`` owned by another
contributor.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from janus.infrastructure.database import Database, SqlAlchemyUnitOfWorkFactory


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    """A file-backed SQLite URL inside pytest's temp directory."""
    return f"sqlite:///{(tmp_path / 'janus-test.db').as_posix()}"


@pytest.fixture
def database(database_url: str) -> Iterator[Database]:
    """A schema-complete test database (metadata bootstrap, not migrations)."""
    db = Database(database_url)
    db.create_all()
    try:
        yield db
    finally:
        db.dispose()


@pytest.fixture
def uow_factory(database: Database) -> SqlAlchemyUnitOfWorkFactory:
    """Unit-of-work factory bound to the test database."""
    return SqlAlchemyUnitOfWorkFactory(database)
