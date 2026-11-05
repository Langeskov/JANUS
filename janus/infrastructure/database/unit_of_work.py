"""SQLAlchemy unit-of-work: one transaction boundary exposing every repository.

Phase 0 spec s5.2 requires order state changes, fills, fees and ledger entries
to be written inside one database transaction. Port usage::

    with uow_factory() as uow:
        uow.fills.append(fill)
        uow.orders.save(order)
        uow.commit()

``commit()`` is the only way to make work durable. Leaving the ``with`` block
without committing rolls everything back (even without an exception), so a
partially built accounting change can never leak into the ledger.
"""

from __future__ import annotations

from janus.domain.common.errors import InvariantViolation
from janus.infrastructure.database.engine import Database
from janus.infrastructure.database.repositories import (
    SqlAlchemyAccountRepository,
    SqlAlchemyCashLedgerRepository,
    SqlAlchemyDataSnapshotRepository,
    SqlAlchemyDecisionRepository,
    SqlAlchemyFillRepository,
    SqlAlchemyInstrumentRepository,
    SqlAlchemyMarketBarRepository,
    SqlAlchemyOrderRepository,
    SqlAlchemyPortfolioSnapshotRepository,
    SqlAlchemyPositionLedgerRepository,
    SqlAlchemyRiskDecisionRepository,
)
from sqlalchemy.orm import Session, sessionmaker

__all__ = ["SqlAlchemyUnitOfWork", "SqlAlchemyUnitOfWorkFactory"]


class SqlAlchemyUnitOfWork:
    """A single database transaction with all eleven repository ports bound."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session = session_factory()
        self._closed = False
        self.instruments = SqlAlchemyInstrumentRepository(self._session)
        self.accounts = SqlAlchemyAccountRepository(self._session)
        self.cash_ledger = SqlAlchemyCashLedgerRepository(self._session)
        self.position_ledger = SqlAlchemyPositionLedgerRepository(self._session)
        self.orders = SqlAlchemyOrderRepository(self._session)
        self.fills = SqlAlchemyFillRepository(self._session)
        self.risk_decisions = SqlAlchemyRiskDecisionRepository(self._session)
        self.decisions = SqlAlchemyDecisionRepository(self._session)
        self.data_snapshots = SqlAlchemyDataSnapshotRepository(self._session)
        self.portfolio_snapshots = SqlAlchemyPortfolioSnapshotRepository(self._session)
        self.market_bars = SqlAlchemyMarketBarRepository(self._session)

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        if self._closed:
            raise InvariantViolation("unit of work is already closed")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Discard uncommitted work and release the session."""
        try:
            self._session.rollback()
        finally:
            self._session.close()
            self._closed = True

    def commit(self) -> None:
        """Make this unit of work durable (the only durability path)."""
        self._session.commit()

    def rollback(self) -> None:
        """Discard everything written in this unit of work."""
        self._session.rollback()

    @property
    def is_closed(self) -> bool:
        """True once the ``with`` block has exited."""
        return self._closed


class SqlAlchemyUnitOfWorkFactory:
    """Creates units of work bound to one engine/session factory.

    Accepts either an already-built :class:`Database` or a database URL::

        factory = SqlAlchemyUnitOfWorkFactory("sqlite:///data/local/janus.db")
        factory = SqlAlchemyUnitOfWorkFactory(database)
    """

    def __init__(self, url_or_database: str | Database) -> None:
        self._database = (
            url_or_database if isinstance(url_or_database, Database) else Database(url_or_database)
        )

    @property
    def database(self) -> Database:
        """The database this factory is bound to."""
        return self._database

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """The underlying session factory (for advanced/administrative use)."""
        return self._database.session_factory

    def __call__(self) -> SqlAlchemyUnitOfWork:
        """Build a fresh unit of work (no transaction starts until first use)."""
        return SqlAlchemyUnitOfWork(self._database.session_factory)

    def create_all(self) -> None:
        """Create the schema from metadata (bootstrap/tests)."""
        self._database.create_all()

    def upgrade_to_head(self) -> None:
        """Apply migrations up to ``head`` (runtime path)."""
        self._database.upgrade_to_head()

    def dispose(self) -> None:
        """Release the engine's connection pool."""
        self._database.dispose()
