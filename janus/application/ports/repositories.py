"""Repository and unit-of-work ports.

Infrastructure implements these protocols (Phase 0 spec s4.2). The application
layer depends only on the protocols, so SQLite can later be replaced by
PostgreSQL without touching services.

Transaction rule (spec s5.2): order state changes, fills, fees and ledger
entries must be written inside one database transaction. The
:class:`UnitOfWork` object is that boundary; ``commit`` is the only way to make
changes durable.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from janus.application.ports.data_foundation import (
    DataProvenanceRepository,
    DataRevisionRepository,
    DataSourceRepository,
    IngestionRunRepository,
    ProviderCapabilityRepository,
    RawPayloadRepository,
    RevisionInvalidationRepository,
)
from janus.domain.instruments.models import Instrument
from janus.domain.ledger.models import CashLedgerEntry, PositionEvent
from janus.domain.market_data.models import DataSnapshot, MarketBar
from janus.domain.orders.models import ExecutionFill, PaperOrder
from janus.domain.portfolio.models import PortfolioAccount, PortfolioSnapshot
from janus.domain.risk.models import RiskDecision
from janus.domain.strategy.decision import DecisionRecord

__all__ = [
    "AccountRepository",
    "CashLedgerRepository",
    "DataProvenanceRepository",
    "DataRevisionRepository",
    "DataSnapshotRepository",
    "DataSourceRepository",
    "DecisionRepository",
    "FillRepository",
    "IngestionRunRepository",
    "InstrumentRepository",
    "MarketBarRepository",
    "OrderRepository",
    "PortfolioSnapshotRepository",
    "PositionLedgerRepository",
    "ProviderCapabilityRepository",
    "RawPayloadRepository",
    "RevisionInvalidationRepository",
    "RiskDecisionRepository",
    "UnitOfWork",
    "UnitOfWorkFactory",
]


@runtime_checkable
class InstrumentRepository(Protocol):
    """Instrument master persistence."""

    def get(self, symbol: str) -> Instrument | None: ...

    def list_all(self) -> list[Instrument]: ...

    def upsert(self, instrument: Instrument) -> None: ...

    def upsert_many(self, instruments: list[Instrument]) -> None: ...


@runtime_checkable
class AccountRepository(Protocol):
    """Paper-account persistence. Accounts are versioned, never silently mutated."""

    def get(self, account_id: str) -> PortfolioAccount | None: ...

    def save(self, account: PortfolioAccount) -> None: ...

    def list_all(self) -> list[PortfolioAccount]: ...


@runtime_checkable
class CashLedgerRepository(Protocol):
    """Append-only cash ledger.

    ``append`` returns ``True`` when the entry was stored and ``False`` when an
    entry with the same idempotency key already existed (harmless replay).
    """

    def append(self, entry: CashLedgerEntry) -> bool: ...

    def list_for_account(self, account_id: str) -> list[CashLedgerEntry]: ...

    def get_by_idempotency_key(self, idempotency_key: str) -> CashLedgerEntry | None: ...

    def next_sequence(self, account_id: str) -> int: ...


@runtime_checkable
class PositionLedgerRepository(Protocol):
    """Append-only position event ledger (mirrors the cash ledger contract)."""

    def append(self, event: PositionEvent) -> bool: ...

    def list_for_account(self, account_id: str) -> list[PositionEvent]: ...

    def get_by_idempotency_key(self, idempotency_key: str) -> PositionEvent | None: ...

    def next_sequence(self, account_id: str) -> int: ...


@runtime_checkable
class OrderRepository(Protocol):
    """Paper order persistence with lifecycle state transitions."""

    def get(self, order_id: str) -> PaperOrder | None: ...

    def save(self, order: PaperOrder) -> None: ...

    def list_by_account(self, account_id: str) -> list[PaperOrder]: ...

    def list_open(self, account_id: str) -> list[PaperOrder]: ...

    def list_by_trade_date(self, account_id: str, trade_date: date) -> list[PaperOrder]: ...

    def count_orders_on(self, account_id: str, trade_date: date) -> int: ...

    def list_by_status(self, account_id: str, statuses: tuple[str, ...]) -> list[PaperOrder]: ...


@runtime_checkable
class FillRepository(Protocol):
    """Immutable execution fills with idempotent append."""

    def append(self, fill: ExecutionFill) -> bool: ...

    def get(self, fill_id: str) -> ExecutionFill | None: ...

    def list_by_order(self, order_id: str) -> list[ExecutionFill]: ...

    def list_by_account(self, account_id: str) -> list[ExecutionFill]: ...

    def list_by_trade_date(self, account_id: str, trade_date: date) -> list[ExecutionFill]: ...

    def get_by_idempotency_key(self, idempotency_key: str) -> ExecutionFill | None: ...


@runtime_checkable
class RiskDecisionRepository(Protocol):
    def append(self, decision: RiskDecision) -> None: ...

    def get(self, risk_decision_id: str) -> RiskDecision | None: ...

    def list_for_intent(self, order_intent_id: str) -> list[RiskDecision]: ...


@runtime_checkable
class DecisionRepository(Protocol):
    """Immutable decision-lineage records (spec s10)."""

    def append(self, record: DecisionRecord) -> None: ...

    def get(self, decision_id: str) -> DecisionRecord | None: ...

    def list_recent(self, limit: int = 50) -> list[DecisionRecord]: ...


@runtime_checkable
class DataSnapshotRepository(Protocol):
    def save(self, snapshot: DataSnapshot) -> None: ...

    def get(self, snapshot_id: str) -> DataSnapshot | None: ...

    def list_recent(self, limit: int = 50) -> list[DataSnapshot]: ...


@runtime_checkable
class PortfolioSnapshotRepository(Protocol):
    def save(self, snapshot: PortfolioSnapshot) -> None: ...

    def get(self, snapshot_id: str) -> PortfolioSnapshot | None: ...

    def latest_for_account(self, account_id: str) -> PortfolioSnapshot | None: ...


@runtime_checkable
class MarketBarRepository(Protocol):
    def upsert_many(self, bars: list[MarketBar]) -> int: ...

    def get(self, symbol: str, trade_date: date) -> MarketBar | None: ...

    def list_range(self, symbol: str, start: date, end: date) -> list[MarketBar]: ...

    def latest(self, symbol: str) -> MarketBar | None: ...


class UnitOfWork(Protocol):
    """One transactional unit of work exposing every repository.

    Usage::

        with uow_factory() as uow:
            uow.fills.append(fill)
            uow.orders.save(order)
            uow.commit()
    """

    instruments: InstrumentRepository
    accounts: AccountRepository
    cash_ledger: CashLedgerRepository
    position_ledger: PositionLedgerRepository
    orders: OrderRepository
    fills: FillRepository
    risk_decisions: RiskDecisionRepository
    decisions: DecisionRepository
    data_snapshots: DataSnapshotRepository
    portfolio_snapshots: PortfolioSnapshotRepository
    market_bars: MarketBarRepository

    # -- data foundation (PR-001) ---------------------------------------
    data_sources: DataSourceRepository
    raw_payloads: RawPayloadRepository
    data_provenance: DataProvenanceRepository
    data_revisions: DataRevisionRepository
    revision_invalidations: RevisionInvalidationRepository
    ingestion_runs: IngestionRunRepository
    provider_capabilities: ProviderCapabilityRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    """Creates units of work bound to one engine/session factory."""

    def __call__(self) -> UnitOfWork: ...
