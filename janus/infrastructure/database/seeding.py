"""Idempotent persistence of deterministic fixture data.

Phase 0 tests and the `janus init`/ingest bootstrap path need the security
master in the database before orders/fills can reference it. Seeding is an
upsert, so running it twice is harmless.
"""

from __future__ import annotations

from collections.abc import Sequence

from janus.domain.instruments.models import Instrument
from janus.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWorkFactory

__all__ = ["seed_cash_instrument", "seed_instruments"]


def seed_instruments(
    factory: SqlAlchemyUnitOfWorkFactory, instruments: Sequence[Instrument]
) -> int:
    """Upsert ``instruments`` in one transaction; returns how many were written."""
    items = list(instruments)
    if not items:
        return 0
    with factory() as uow:
        uow.instruments.upsert_many(items)
        uow.commit()
    return len(items)


def seed_cash_instrument(factory: SqlAlchemyUnitOfWorkFactory) -> int:
    """Upsert the canonical ``CASH.CNY`` pseudo-instrument."""
    return seed_instruments(factory, [Instrument.cash()])
