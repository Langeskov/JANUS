"""Concrete SQLAlchemy implementations of every repository port.

Contract: ``janus/application/ports/repositories.py`` (not modified). Behaviour
guaranteed here (Phase 0 spec s5.1, s5.2, s10; acceptance criteria 3 and 4):

* ``append`` on the cash ledger, position ledger, fills and other immutable
  records is **idempotent**: replaying the same event returns ``False`` and
  leaves the database unchanged; reusing a key with different content raises
  :class:`~janus.domain.common.errors.IdempotencyConflictError` instead of
  silently overwriting.
* Sequences are per-account and discovered with ``next_sequence``; the database
  UNIQUE constraints are the final arbiter against double-counting.
* ``fill`` children (fee lines), ``risk_decision`` children (checks) and
  ``portfolio_snapshot`` children (positions) are written in the same
  transaction as their header, parent first, so foreign keys hold.
* Immutable records (accounts versions, fills, decisions, snapshots) never
  silently overwrite existing rows.

Repository objects are bound to one :class:`~sqlalchemy.orm.Session` and are
created by :class:`~janus.infrastructure.database.unit_of_work.SqlAlchemyUnitOfWork`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from janus.application.ports.data_foundation import DataRevisionQuery
from janus.domain.common.enums import OrderStatus
from janus.domain.common.errors import (
    IdempotencyConflictError,
    InvariantViolation,
    ValidationError,
)
from janus.domain.data_foundation.capabilities import DataSource, ProviderCapabilities
from janus.domain.data_foundation.provenance import DataProvenance
from janus.domain.data_foundation.quality import IngestionReport
from janus.domain.data_foundation.query import LoadedRevision
from janus.domain.data_foundation.raw import RawPayload
from janus.domain.data_foundation.revisions import DataRevision, RevisionInvalidation
from janus.domain.instruments.models import Instrument
from janus.domain.ledger.models import CashLedgerEntry, PositionEvent
from janus.domain.market_data.models import DataSnapshot, MarketBar
from janus.domain.orders.models import ExecutionFill, PaperOrder
from janus.domain.portfolio.models import PortfolioAccount, PortfolioSnapshot
from janus.domain.risk.models import RiskDecision
from janus.domain.strategy.decision import DecisionRecord
from janus.infrastructure.database.mappers import (
    account_from_row,
    account_to_values,
    apply_values,
    cash_entry_from_row,
    cash_entry_to_values,
    data_provenance_from_row,
    data_provenance_to_values,
    data_revision_from_row,
    data_revision_to_values,
    data_snapshot_from_row,
    data_snapshot_to_values,
    data_source_from_row,
    data_source_to_values,
    decision_record_from_row,
    decision_record_to_values,
    fee_line_to_values,
    fill_from_row,
    fill_to_values,
    ingestion_issue_to_values,
    ingestion_report_from_row,
    ingestion_run_to_values,
    instrument_from_row,
    instrument_to_values,
    loaded_revision_from_rows,
    make_row,
    market_bar_from_row,
    market_bar_to_values,
    order_from_row,
    order_to_values,
    portfolio_position_to_values,
    portfolio_snapshot_from_row,
    portfolio_snapshot_to_values,
    position_event_from_row,
    position_event_to_values,
    provider_capabilities_from_row,
    provider_capabilities_to_values,
    raw_payload_from_row,
    raw_payload_to_values,
    revision_invalidation_from_row,
    revision_invalidation_to_values,
    risk_check_to_values,
    risk_decision_from_row,
    risk_decision_to_values,
)
from janus.infrastructure.database.tables import (
    AccountIdentityRow,
    AccountRow,
    CashLedgerEntryRow,
    DataProvenanceRow,
    DataRevisionInvalidationRow,
    DataRevisionRow,
    DataSnapshotRow,
    DataSourceRow,
    DecisionRecordRow,
    FeeLineRow,
    FillRow,
    IngestionIssueRow,
    IngestionRunRow,
    InstrumentRow,
    MarketBarRow,
    OrderRow,
    PortfolioPositionRow,
    PortfolioSnapshotRow,
    PositionEventRow,
    ProviderCapabilityRow,
    RawPayloadRow,
    RiskCheckRow,
    RiskDecisionRow,
)
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

__all__ = [
    "SqlAlchemyAccountRepository",
    "SqlAlchemyCashLedgerRepository",
    "SqlAlchemyDataProvenanceRepository",
    "SqlAlchemyDataRevisionRepository",
    "SqlAlchemyDataSnapshotRepository",
    "SqlAlchemyDataSourceRepository",
    "SqlAlchemyDecisionRepository",
    "SqlAlchemyFillRepository",
    "SqlAlchemyIngestionRunRepository",
    "SqlAlchemyInstrumentRepository",
    "SqlAlchemyMarketBarRepository",
    "SqlAlchemyOrderRepository",
    "SqlAlchemyPortfolioSnapshotRepository",
    "SqlAlchemyPositionLedgerRepository",
    "SqlAlchemyProviderCapabilityRepository",
    "SqlAlchemyRawPayloadRepository",
    "SqlAlchemyRevisionInvalidationRepository",
    "SqlAlchemyRiskDecisionRepository",
]

_LIVE_ORDER_STATUSES: tuple[OrderStatus, ...] = tuple(
    status for status in OrderStatus if status.is_live
)


class _SessionRepository:
    """Shared session plumbing for the concrete repositories."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _flush(self) -> None:
        self._session.flush()


def _replay_or_conflict(existing: Any, incoming: Any, *, message: str, **details: Any) -> bool:
    """``False`` for an identical replay; a hard error for altered content."""
    if existing == incoming:
        return False
    raise IdempotencyConflictError(message, **details)


def _insert_immutable(
    session: Session,
    *,
    row: Any,
    existing: Callable[[], Any | None],
    incoming: Any,
    message: str,
    details: dict[str, Any],
) -> bool:
    """Insert one immutable row with replay detection and savepoint recovery.

    A UNIQUE violation after the pre-check means a concurrent writer archived the
    same identity first: re-read and replay, never double-write.
    """
    found = existing()
    if found is not None:
        return _replay_or_conflict(found, incoming, message=message, **details)
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError as exc:
        found = existing()
        if found is not None:
            return _replay_or_conflict(found, incoming, message=message, **details)
        raise IdempotencyConflictError(message, **details) from exc
    return True


def _parse_order_statuses(statuses: Sequence[str]) -> tuple[OrderStatus, ...]:
    """Parse status filters fail-closed: an unknown status is a rejection."""
    parsed: list[OrderStatus] = []
    for status in statuses:
        try:
            parsed.append(OrderStatus(status))
        except ValueError as exc:
            raise ValidationError(f"unknown order status filter {status!r}", status=status) from exc
    return tuple(dict.fromkeys(parsed))


class SqlAlchemyInstrumentRepository(_SessionRepository):
    """Instrument master persistence."""

    def get(self, symbol: str) -> Instrument | None:
        row = self._session.get(InstrumentRow, symbol)
        return None if row is None else instrument_from_row(row)

    def list_all(self) -> list[Instrument]:
        rows = self._session.scalars(select(InstrumentRow).order_by(InstrumentRow.symbol)).all()
        return [instrument_from_row(row) for row in rows]

    def upsert(self, instrument: Instrument) -> None:
        self.upsert_many([instrument])

    def upsert_many(self, instruments: list[Instrument]) -> None:
        for instrument in instruments:
            values = instrument_to_values(instrument)
            row = self._session.get(InstrumentRow, instrument.symbol)
            if row is None:
                self._session.add(make_row(InstrumentRow, values))
            else:
                apply_values(row, values)
        self._flush()


class SqlAlchemyAccountRepository(_SessionRepository):
    """Versioned account persistence: one immutable row per version."""

    def get(self, account_id: str) -> PortfolioAccount | None:
        row = self._session.scalars(
            select(AccountRow)
            .where(AccountRow.account_id == account_id)
            .order_by(AccountRow.version.desc())
            .limit(1)
        ).first()
        return None if row is None else account_from_row(row)

    def save(self, account: PortfolioAccount) -> None:
        existing = self._session.get(AccountRow, (account.account_id, account.version))
        if existing is not None:
            if account_from_row(existing) == account:
                return
            raise IdempotencyConflictError(
                "account version already persisted with different content",
                account_id=account.account_id,
                version=account.version,
            )
        latest = self._latest_version(account.account_id)
        if latest is not None and account.version < latest:
            raise InvariantViolation(
                "cannot persist an account version older than the stored state",
                account_id=account.account_id,
                version=account.version,
                latest_version=latest,
            )
        if self._session.get(AccountIdentityRow, account.account_id) is None:
            self._session.add(make_row(AccountIdentityRow, {"account_id": account.account_id}))
            self._flush()
        self._session.add(make_row(AccountRow, account_to_values(account)))
        self._flush()

    def list_all(self) -> list[PortfolioAccount]:
        rows = self._session.scalars(
            select(AccountRow).order_by(AccountRow.account_id, AccountRow.version.desc())
        ).all()
        latest: dict[str, PortfolioAccount] = {}
        for row in rows:
            latest.setdefault(row.account_id, account_from_row(row))
        return [latest[account_id] for account_id in sorted(latest)]

    def _latest_version(self, account_id: str) -> int | None:
        value = self._session.scalar(
            select(func.max(AccountRow.version)).where(AccountRow.account_id == account_id)
        )
        return None if value is None else int(value)


class SqlAlchemyCashLedgerRepository(_SessionRepository):
    """Append-only, idempotent cash ledger."""

    def append(self, entry: CashLedgerEntry) -> bool:
        duplicate = self._find(entry.account_id, entry.idempotency_key)
        if duplicate is not None:
            return self._replay_or_conflict(duplicate, entry)
        row = make_row(CashLedgerEntryRow, cash_entry_to_values(entry))
        try:
            with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError as exc:
            duplicate = self._find(entry.account_id, entry.idempotency_key)
            if duplicate is not None:
                return self._replay_or_conflict(duplicate, entry)
            raise InvariantViolation(
                "cash ledger entry conflicts with an existing sequence number",
                account_id=entry.account_id,
                sequence=entry.sequence,
            ) from exc
        return True

    def list_for_account(self, account_id: str) -> list[CashLedgerEntry]:
        rows = self._session.scalars(
            select(CashLedgerEntryRow)
            .where(CashLedgerEntryRow.account_id == account_id)
            .order_by(CashLedgerEntryRow.sequence)
        ).all()
        return [cash_entry_from_row(row) for row in rows]

    def get_by_idempotency_key(self, idempotency_key: str) -> CashLedgerEntry | None:
        row = self._session.scalars(
            select(CashLedgerEntryRow)
            .where(CashLedgerEntryRow.idempotency_key == idempotency_key)
            .order_by(CashLedgerEntryRow.account_id, CashLedgerEntryRow.sequence)
            .limit(1)
        ).first()
        return None if row is None else cash_entry_from_row(row)

    def next_sequence(self, account_id: str) -> int:
        value = self._session.scalar(
            select(func.max(CashLedgerEntryRow.sequence)).where(
                CashLedgerEntryRow.account_id == account_id
            )
        )
        return int(value or 0) + 1

    def _find(self, account_id: str, idempotency_key: str) -> CashLedgerEntry | None:
        row = self._session.scalars(
            select(CashLedgerEntryRow)
            .where(
                CashLedgerEntryRow.account_id == account_id,
                CashLedgerEntryRow.idempotency_key == idempotency_key,
            )
            .limit(1)
        ).first()
        return None if row is None else cash_entry_from_row(row)

    @staticmethod
    def _replay_or_conflict(existing: CashLedgerEntry, entry: CashLedgerEntry) -> bool:
        if existing == entry:
            return False
        raise IdempotencyConflictError(
            "cash ledger idempotency key reused with different content",
            idempotency_key=entry.idempotency_key,
            entry_id=entry.entry_id,
        )


class SqlAlchemyPositionLedgerRepository(_SessionRepository):
    """Append-only, idempotent position-event ledger."""

    def append(self, event: PositionEvent) -> bool:
        duplicate = self._find(event.account_id, event.idempotency_key)
        if duplicate is not None:
            return self._replay_or_conflict(duplicate, event)
        row = make_row(PositionEventRow, position_event_to_values(event))
        try:
            with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError as exc:
            duplicate = self._find(event.account_id, event.idempotency_key)
            if duplicate is not None:
                return self._replay_or_conflict(duplicate, event)
            raise InvariantViolation(
                "position event conflicts with an existing sequence number",
                account_id=event.account_id,
                sequence=event.sequence,
            ) from exc
        return True

    def list_for_account(self, account_id: str) -> list[PositionEvent]:
        rows = self._session.scalars(
            select(PositionEventRow)
            .where(PositionEventRow.account_id == account_id)
            .order_by(PositionEventRow.sequence)
        ).all()
        return [position_event_from_row(row) for row in rows]

    def get_by_idempotency_key(self, idempotency_key: str) -> PositionEvent | None:
        row = self._session.scalars(
            select(PositionEventRow)
            .where(PositionEventRow.idempotency_key == idempotency_key)
            .order_by(PositionEventRow.account_id, PositionEventRow.sequence)
            .limit(1)
        ).first()
        return None if row is None else position_event_from_row(row)

    def next_sequence(self, account_id: str) -> int:
        value = self._session.scalar(
            select(func.max(PositionEventRow.sequence)).where(
                PositionEventRow.account_id == account_id
            )
        )
        return int(value or 0) + 1

    def _find(self, account_id: str, idempotency_key: str) -> PositionEvent | None:
        row = self._session.scalars(
            select(PositionEventRow)
            .where(
                PositionEventRow.account_id == account_id,
                PositionEventRow.idempotency_key == idempotency_key,
            )
            .limit(1)
        ).first()
        return None if row is None else position_event_from_row(row)

    @staticmethod
    def _replay_or_conflict(existing: PositionEvent, event: PositionEvent) -> bool:
        if existing == event:
            return False
        raise IdempotencyConflictError(
            "position event idempotency key reused with different content",
            idempotency_key=event.idempotency_key,
            event_id=event.event_id,
        )


class SqlAlchemyOrderRepository(_SessionRepository):
    """Paper order persistence with lifecycle-state updates."""

    def get(self, order_id: str) -> PaperOrder | None:
        row = self._session.get(OrderRow, order_id)
        return None if row is None else order_from_row(row)

    def save(self, order: PaperOrder) -> None:
        values = order_to_values(order)
        row = self._session.get(OrderRow, order.order_id)
        if row is None:
            self._session.add(make_row(OrderRow, values))
        else:
            apply_values(row, values)
        self._flush()

    def list_by_account(self, account_id: str) -> list[PaperOrder]:
        rows = self._session.scalars(
            select(OrderRow)
            .where(OrderRow.account_id == account_id)
            .order_by(OrderRow.created_at, OrderRow.order_id)
        ).all()
        return [order_from_row(row) for row in rows]

    def list_open(self, account_id: str) -> list[PaperOrder]:
        rows = self._session.scalars(
            select(OrderRow)
            .where(
                OrderRow.account_id == account_id,
                OrderRow.status.in_(_LIVE_ORDER_STATUSES),
            )
            .order_by(OrderRow.created_at, OrderRow.order_id)
        ).all()
        return [order_from_row(row) for row in rows]

    def list_by_trade_date(self, account_id: str, trade_date: date) -> list[PaperOrder]:
        """Orders whose ``target_trade_date`` is ``trade_date``.

        ``PaperOrder`` carries no separate "submitted trading date", so the
        intended trade date is the only date the risk controller and the daily
        cycle can agree on.
        """
        rows = self._session.scalars(
            select(OrderRow)
            .where(
                OrderRow.account_id == account_id,
                OrderRow.target_trade_date == trade_date,
            )
            .order_by(OrderRow.created_at, OrderRow.order_id)
        ).all()
        return [order_from_row(row) for row in rows]

    def count_orders_on(self, account_id: str, trade_date: date) -> int:
        value = self._session.scalar(
            select(func.count())
            .select_from(OrderRow)
            .where(
                OrderRow.account_id == account_id,
                OrderRow.target_trade_date == trade_date,
            )
        )
        return int(value or 0)

    def list_by_status(self, account_id: str, statuses: tuple[str, ...]) -> list[PaperOrder]:
        parsed = _parse_order_statuses(statuses)
        if not parsed:
            return []
        rows = self._session.scalars(
            select(OrderRow)
            .where(
                OrderRow.account_id == account_id,
                OrderRow.status.in_(parsed),
            )
            .order_by(OrderRow.created_at, OrderRow.order_id)
        ).all()
        return [order_from_row(row) for row in rows]


class SqlAlchemyFillRepository(_SessionRepository):
    """Immutable execution fills with idempotent append."""

    def append(self, fill: ExecutionFill) -> bool:
        duplicate = self._find(fill.fill_id, fill.idempotency_key)
        if duplicate is not None:
            return self._replay_or_conflict(duplicate, fill)
        fill_row = make_row(FillRow, fill_to_values(fill))
        try:
            with self._session.begin_nested():
                self._session.add(fill_row)
                self._flush()
                for index, line in enumerate(fill.fees.lines):
                    self._session.add(
                        make_row(FeeLineRow, fee_line_to_values(fill.fill_id, index, line))
                    )
                self._flush()
        except IntegrityError:
            duplicate = self._find(fill.fill_id, fill.idempotency_key)
            if duplicate is not None:
                return self._replay_or_conflict(duplicate, fill)
            raise
        return True

    def get(self, fill_id: str) -> ExecutionFill | None:
        row = self._session.get(FillRow, fill_id)
        if row is None:
            return None
        return fill_from_row(row, self._fee_rows([fill_id])[fill_id])

    def list_by_order(self, order_id: str) -> list[ExecutionFill]:
        rows = self._session.scalars(
            select(FillRow)
            .where(FillRow.order_id == order_id)
            .order_by(FillRow.executed_at, FillRow.fill_id)
        ).all()
        return self._hydrate(rows)

    def list_by_account(self, account_id: str) -> list[ExecutionFill]:
        rows = self._session.scalars(
            select(FillRow)
            .where(FillRow.account_id == account_id)
            .order_by(FillRow.executed_at, FillRow.fill_id)
        ).all()
        return self._hydrate(rows)

    def list_by_trade_date(self, account_id: str, trade_date: date) -> list[ExecutionFill]:
        rows = self._session.scalars(
            select(FillRow)
            .where(FillRow.account_id == account_id, FillRow.trade_date == trade_date)
            .order_by(FillRow.executed_at, FillRow.fill_id)
        ).all()
        return self._hydrate(rows)

    def get_by_idempotency_key(self, idempotency_key: str) -> ExecutionFill | None:
        row = self._session.scalars(
            select(FillRow).where(FillRow.idempotency_key == idempotency_key).limit(1)
        ).first()
        if row is None:
            return None
        return fill_from_row(row, self._fee_rows([row.fill_id])[row.fill_id])

    def _find(self, fill_id: str, idempotency_key: str) -> ExecutionFill | None:
        by_id = self.get(fill_id)
        if by_id is not None:
            return by_id
        return self.get_by_idempotency_key(idempotency_key)

    def _fee_rows(self, fill_ids: Sequence[str]) -> dict[str, list[FeeLineRow]]:
        grouped: dict[str, list[FeeLineRow]] = {fill_id: [] for fill_id in fill_ids}
        if not fill_ids:
            return grouped
        rows = self._session.scalars(
            select(FeeLineRow)
            .where(FeeLineRow.fill_id.in_(list(fill_ids)))
            .order_by(FeeLineRow.fill_id, FeeLineRow.line_index)
        ).all()
        for row in rows:
            grouped.setdefault(row.fill_id, []).append(row)
        return grouped

    def _hydrate(self, rows: Sequence[FillRow]) -> list[ExecutionFill]:
        if not rows:
            return []
        grouped = self._fee_rows([row.fill_id for row in rows])
        return [fill_from_row(row, grouped[row.fill_id]) for row in rows]

    @staticmethod
    def _replay_or_conflict(existing: ExecutionFill, fill: ExecutionFill) -> bool:
        if existing == fill:
            return False
        raise IdempotencyConflictError(
            "fill idempotency key reused with different content",
            idempotency_key=fill.idempotency_key,
            fill_id=fill.fill_id,
        )


class SqlAlchemyRiskDecisionRepository(_SessionRepository):
    """Immutable risk decisions with their check list."""

    def append(self, decision: RiskDecision) -> None:
        existing = self.get(decision.risk_decision_id)
        if existing is not None:
            self._replay_or_conflict(existing, decision)
            return
        try:
            with self._session.begin_nested():
                self._session.add(make_row(RiskDecisionRow, risk_decision_to_values(decision)))
                self._flush()
                for index, check in enumerate(decision.checks):
                    self._session.add(
                        make_row(
                            RiskCheckRow,
                            risk_check_to_values(decision.risk_decision_id, index, check),
                        )
                    )
                self._flush()
        except IntegrityError as exc:
            existing = self.get(decision.risk_decision_id)
            if existing is not None:
                self._replay_or_conflict(existing, decision)
                return
            raise IdempotencyConflictError(
                "risk decision conflicts with an existing record",
                risk_decision_id=decision.risk_decision_id,
            ) from exc

    def get(self, risk_decision_id: str) -> RiskDecision | None:
        row = self._session.get(RiskDecisionRow, risk_decision_id)
        if row is None:
            return None
        check_rows = self._session.scalars(
            select(RiskCheckRow)
            .where(RiskCheckRow.risk_decision_id == risk_decision_id)
            .order_by(RiskCheckRow.check_index)
        ).all()
        return risk_decision_from_row(row, check_rows)

    def list_for_intent(self, order_intent_id: str) -> list[RiskDecision]:
        rows = self._session.scalars(
            select(RiskDecisionRow)
            .where(RiskDecisionRow.order_intent_id == order_intent_id)
            .order_by(RiskDecisionRow.decided_at, RiskDecisionRow.risk_decision_id)
        ).all()
        decisions: list[RiskDecision] = []
        for row in rows:
            decision = self.get(row.risk_decision_id)
            if decision is not None:
                decisions.append(decision)
        return decisions

    @staticmethod
    def _replay_or_conflict(existing: RiskDecision, decision: RiskDecision) -> None:
        if existing == decision:
            return
        raise IdempotencyConflictError(
            "risk decision id reused with different content",
            risk_decision_id=decision.risk_decision_id,
        )


class SqlAlchemyDecisionRepository(_SessionRepository):
    """Immutable decision-lineage records."""

    def append(self, record: DecisionRecord) -> None:
        existing = self.get(record.decision_id)
        if existing is not None:
            self._replay_or_conflict(existing, record)
            return
        try:
            with self._session.begin_nested():
                self._session.add(make_row(DecisionRecordRow, decision_record_to_values(record)))
        except IntegrityError as exc:
            existing = self.get(record.decision_id)
            if existing is not None:
                self._replay_or_conflict(existing, record)
                return
            raise IdempotencyConflictError(
                "decision record conflicts with an existing record",
                decision_id=record.decision_id,
            ) from exc

    def get(self, decision_id: str) -> DecisionRecord | None:
        row = self._session.get(DecisionRecordRow, decision_id)
        return None if row is None else decision_record_from_row(row)

    def list_recent(self, limit: int = 50) -> list[DecisionRecord]:
        rows = self._session.scalars(
            select(DecisionRecordRow)
            .order_by(DecisionRecordRow.created_at.desc(), DecisionRecordRow.decision_id.desc())
            .limit(max(1, limit))
        ).all()
        return [decision_record_from_row(row) for row in rows]

    @staticmethod
    def _replay_or_conflict(existing: DecisionRecord, record: DecisionRecord) -> None:
        if existing == record:
            return
        raise IdempotencyConflictError(
            "decision id reused with different content", decision_id=record.decision_id
        )


class SqlAlchemyDataSnapshotRepository(_SessionRepository):
    """Immutable, content-addressed data provenance snapshots."""

    def save(self, snapshot: DataSnapshot) -> None:
        existing = self.get(snapshot.snapshot_id)
        if existing is not None:
            if existing == snapshot:
                return
            raise IdempotencyConflictError(
                "data snapshot id reused with different content",
                snapshot_id=snapshot.snapshot_id,
            )
        row = make_row(DataSnapshotRow, data_snapshot_to_values(snapshot))
        try:
            with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError as exc:
            raise IdempotencyConflictError(
                "data snapshot conflicts with an existing record",
                snapshot_id=snapshot.snapshot_id,
            ) from exc

    def get(self, snapshot_id: str) -> DataSnapshot | None:
        row = self._session.get(DataSnapshotRow, snapshot_id)
        return None if row is None else data_snapshot_from_row(row)

    def list_recent(self, limit: int = 50) -> list[DataSnapshot]:
        rows = self._session.scalars(
            select(DataSnapshotRow)
            .order_by(DataSnapshotRow.created_at.desc(), DataSnapshotRow.snapshot_id.desc())
            .limit(max(1, limit))
        ).all()
        return [data_snapshot_from_row(row) for row in rows]


class SqlAlchemyPortfolioSnapshotRepository(_SessionRepository):
    """Immutable portfolio valuation snapshots with their positions."""

    def save(self, snapshot: PortfolioSnapshot) -> None:
        existing = self.get(snapshot.snapshot_id)
        if existing is not None:
            if existing == snapshot:
                return
            raise IdempotencyConflictError(
                "portfolio snapshot id reused with different content",
                snapshot_id=snapshot.snapshot_id,
            )
        try:
            with self._session.begin_nested():
                self._session.add(
                    make_row(PortfolioSnapshotRow, portfolio_snapshot_to_values(snapshot))
                )
                self._flush()
                for index, position in enumerate(snapshot.positions):
                    self._session.add(
                        make_row(
                            PortfolioPositionRow,
                            portfolio_position_to_values(snapshot.snapshot_id, index, position),
                        )
                    )
                self._flush()
        except IntegrityError as exc:
            raise IdempotencyConflictError(
                "portfolio snapshot conflicts with an existing record",
                snapshot_id=snapshot.snapshot_id,
            ) from exc

    def get(self, snapshot_id: str) -> PortfolioSnapshot | None:
        row = self._session.get(PortfolioSnapshotRow, snapshot_id)
        if row is None:
            return None
        return portfolio_snapshot_from_row(row, self._position_rows(snapshot_id))

    def latest_for_account(self, account_id: str) -> PortfolioSnapshot | None:
        row = self._session.scalars(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.account_id == account_id)
            .order_by(
                PortfolioSnapshotRow.as_of.desc(),
                PortfolioSnapshotRow.snapshot_id.desc(),
            )
            .limit(1)
        ).first()
        if row is None:
            return None
        return portfolio_snapshot_from_row(row, self._position_rows(row.snapshot_id))

    def _position_rows(self, snapshot_id: str) -> list[PortfolioPositionRow]:
        rows = self._session.scalars(
            select(PortfolioPositionRow)
            .where(PortfolioPositionRow.snapshot_id == snapshot_id)
            .order_by(PortfolioPositionRow.position_index)
        ).all()
        return list(rows)


class SqlAlchemyMarketBarRepository(_SessionRepository):
    """Daily OHLCV persistence keyed by ``(symbol, trade_date)``."""

    def upsert_many(self, bars: list[MarketBar]) -> int:
        """Store bars idempotently; returns the number of distinct bars written."""
        unique: dict[tuple[str, date], MarketBar] = {}
        for bar in bars:
            unique[(bar.symbol, bar.trade_date)] = bar
        for (symbol, trade_date), bar in unique.items():
            values = market_bar_to_values(bar)
            row = self._session.get(MarketBarRow, (symbol, trade_date))
            if row is None:
                self._session.add(make_row(MarketBarRow, values))
            else:
                apply_values(row, values)
        self._flush()
        return len(unique)

    def get(self, symbol: str, trade_date: date) -> MarketBar | None:
        row = self._session.get(MarketBarRow, (symbol, trade_date))
        return None if row is None else market_bar_from_row(row)

    def list_range(self, symbol: str, start: date, end: date) -> list[MarketBar]:
        rows = self._session.scalars(
            select(MarketBarRow)
            .where(
                MarketBarRow.symbol == symbol,
                MarketBarRow.trade_date >= start,
                MarketBarRow.trade_date <= end,
            )
            .order_by(MarketBarRow.trade_date)
        ).all()
        return [market_bar_from_row(row) for row in rows]

    def latest(self, symbol: str) -> MarketBar | None:
        row = self._session.scalars(
            select(MarketBarRow)
            .where(MarketBarRow.symbol == symbol)
            .order_by(MarketBarRow.trade_date.desc())
            .limit(1)
        ).first()
        return None if row is None else market_bar_from_row(row)


# ---------------------------------------------------------------------------
# Data foundation (PR-001): immutable ingestion, provenance and as-of reads
# ---------------------------------------------------------------------------


class SqlAlchemyDataSourceRepository(_SessionRepository):
    """Registry of known data origins (registration is append-only)."""

    def register(self, source: DataSource) -> bool:
        return _insert_immutable(
            self._session,
            row=make_row(DataSourceRow, data_source_to_values(source)),
            existing=lambda: self.get(source.source_id),
            incoming=source,
            message="data source id already registered with different content",
            details={"source_id": source.source_id},
        )

    def get(self, source_id: str) -> DataSource | None:
        row = self._session.get(DataSourceRow, source_id)
        return None if row is None else data_source_from_row(row)

    def exists(self, source_id: str) -> bool:
        return self._session.get(DataSourceRow, source_id) is not None

    def list_all(self) -> list[DataSource]:
        rows = self._session.scalars(select(DataSourceRow).order_by(DataSourceRow.source_id)).all()
        return [data_source_from_row(row) for row in rows]


class SqlAlchemyRawPayloadRepository(_SessionRepository):
    """Append-only raw payload archive with content-hash deduplication."""

    def append(self, payload: RawPayload) -> bool:
        existing = self.get(payload.payload_id) or self.get_by_content_hash(payload.content_hash)
        if existing is not None:
            return self._replay_or_conflict(existing, payload)
        row = make_row(RawPayloadRow, raw_payload_to_values(payload))
        try:
            with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError:
            duplicate = self.get_by_content_hash(payload.content_hash) or self.get(
                payload.payload_id
            )
            if duplicate is not None:
                return self._replay_or_conflict(duplicate, payload)
            raise
        return True

    @staticmethod
    def _replay_or_conflict(existing: RawPayload, payload: RawPayload) -> bool:
        """Content-addressed duplicate detection.

        Identical bytes are the duplicate key, so re-retrieving the same payload is
        harmless even when the retrieval timestamps differ: the archive keeps the
        first-seen record. A payload id reused for *different* content is a hard
        conflict.
        """
        if existing.content_hash == payload.content_hash:
            return False
        raise IdempotencyConflictError(
            "raw payload id already archived with different content",
            payload_id=payload.payload_id,
            content_hash=payload.content_hash,
            archived_content_hash=existing.content_hash,
        )

    def get(self, payload_id: str) -> RawPayload | None:
        row = self._session.get(RawPayloadRow, payload_id)
        return None if row is None else raw_payload_from_row(row)

    def get_by_content_hash(self, content_hash: str) -> RawPayload | None:
        row = self._session.scalars(
            select(RawPayloadRow).where(RawPayloadRow.content_hash == content_hash).limit(1)
        ).first()
        return None if row is None else raw_payload_from_row(row)

    def exists_hash(self, content_hash: str) -> bool:
        found = self._session.scalar(
            select(RawPayloadRow.payload_id)
            .where(RawPayloadRow.content_hash == content_hash)
            .limit(1)
        )
        return found is not None

    def list_by_dataset(self, dataset: str, *, limit: int = 100) -> list[RawPayload]:
        rows = self._session.scalars(
            select(RawPayloadRow)
            .where(RawPayloadRow.dataset == dataset)
            .order_by(RawPayloadRow.retrieved_at.desc(), RawPayloadRow.payload_id)
            .limit(max(1, limit))
        ).all()
        return [raw_payload_from_row(row) for row in rows]


class SqlAlchemyDataProvenanceRepository(_SessionRepository):
    """Append-only provenance store."""

    def append(self, provenance: DataProvenance) -> bool:
        existing = self.get(provenance.provenance_id)
        if existing is not None:
            return _replay_or_conflict(
                existing,
                provenance,
                message="provenance id already stored with different content",
                provenance_id=provenance.provenance_id,
            )
        row = make_row(DataProvenanceRow, data_provenance_to_values(provenance))
        try:
            with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError as exc:
            duplicate = self.get(provenance.provenance_id)
            if duplicate is not None:
                return _replay_or_conflict(
                    duplicate,
                    provenance,
                    message="provenance id already stored with different content",
                    provenance_id=provenance.provenance_id,
                )
            raise IdempotencyConflictError(
                "provenance identity already exists with different content",
                provenance_id=provenance.provenance_id,
                source_id=provenance.source_id,
                dataset=provenance.dataset,
                source_record_id=provenance.source_record_id,
                source_revision=provenance.source_revision,
                payload_hash=provenance.payload_hash,
            ) from exc
        return True

    def get(self, provenance_id: str) -> DataProvenance | None:
        row = self._session.get(DataProvenanceRow, provenance_id)
        return None if row is None else data_provenance_from_row(row)

    def list_for_source(
        self, source_id: str, *, dataset: str | None = None, limit: int = 100
    ) -> list[DataProvenance]:
        statement = select(DataProvenanceRow).where(DataProvenanceRow.source_id == source_id)
        if dataset is not None:
            statement = statement.where(DataProvenanceRow.dataset == dataset)
        rows = self._session.scalars(
            statement.order_by(
                DataProvenanceRow.ingested_at.desc(),
                DataProvenanceRow.provenance_id,
            ).limit(max(1, limit))
        ).all()
        return [data_provenance_from_row(row) for row in rows]


class SqlAlchemyDataRevisionRepository(_SessionRepository):
    """Append-only normalized revision store with as-of candidate queries."""

    def append(self, revision: DataRevision) -> bool:
        """Append a normalized revision; ``False`` for a repeated ingestion attempt.

        Revision identity deliberately excludes the provenance id, so a repeated
        attempt (new provenance row, same fact content) maps to the same revision
        id. The first stored revision wins; the later attempt is preserved in the
        provenance/report history instead of creating a duplicate fact.
        """
        if self.get(revision.revision_id) is not None:
            return False
        row = make_row(DataRevisionRow, data_revision_to_values(revision))
        try:
            with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError as exc:
            if self.get(revision.revision_id) is not None:
                return False
            raise IdempotencyConflictError(
                "revision identity slot is already occupied by a different revision",
                revision_id=revision.revision_id,
                logical_key=revision.logical_key,
                revision_sequence=revision.revision_sequence,
                source_revision=revision.source_revision,
                payload_hash=revision.payload_hash,
            ) from exc
        return True

    def get(self, revision_id: str) -> DataRevision | None:
        row = self._session.get(DataRevisionRow, revision_id)
        return None if row is None else data_revision_from_row(row)

    def get_with_provenance(self, revision_id: str) -> LoadedRevision | None:
        revision_row = self._session.get(DataRevisionRow, revision_id)
        if revision_row is None:
            return None
        provenance_row = self._session.get(DataProvenanceRow, revision_row.provenance_id)
        if provenance_row is None:  # pragma: no cover - FK makes this impossible
            return None
        return loaded_revision_from_rows(revision_row, provenance_row)

    def list_for_logical_key(self, logical_key: str) -> list[DataRevision]:
        rows = self._session.scalars(
            select(DataRevisionRow)
            .where(DataRevisionRow.logical_key == logical_key)
            .order_by(DataRevisionRow.revision_sequence, DataRevisionRow.revision_id)
        ).all()
        return [data_revision_from_row(row) for row in rows]

    def list_for_logical_key_loaded(self, logical_key: str) -> list[LoadedRevision]:
        rows = self._session.scalars(
            select(DataRevisionRow)
            .where(DataRevisionRow.logical_key == logical_key)
            .order_by(DataRevisionRow.revision_sequence, DataRevisionRow.revision_id)
        ).all()
        return self._hydrate_loaded(rows)

    def next_revision_sequence(self, logical_key: str) -> int:
        value = self._session.scalar(
            select(func.max(DataRevisionRow.revision_sequence)).where(
                DataRevisionRow.logical_key == logical_key
            )
        )
        return int(value or 0) + 1

    def list_candidates(self, query: DataRevisionQuery) -> list[LoadedRevision]:
        """Revision rows whose provenance is strategy-available by ``query.as_of``.

        Only mechanical prefilters are applied (dataset, availability cutoff,
        identity/period columns); invalidation and business-time semantics stay in
        the application service.
        """
        statement = (
            select(DataRevisionRow)
            .join(
                DataProvenanceRow,
                DataRevisionRow.provenance_id == DataProvenanceRow.provenance_id,
            )
            .where(
                DataRevisionRow.dataset == query.dataset,
                DataProvenanceRow.strategy_usable_at.is_not(None),
                DataProvenanceRow.strategy_usable_at <= query.as_of,
            )
        )
        if query.source_id is not None:
            statement = statement.where(DataRevisionRow.source_id == query.source_id)
        if query.logical_key is not None:
            statement = statement.where(DataRevisionRow.logical_key == query.logical_key)
        if query.logical_key_prefix is not None:
            statement = statement.where(
                DataRevisionRow.logical_key.startswith(query.logical_key_prefix, autoescape=True)
            )
        if query.subject_symbol is not None:
            statement = statement.where(DataRevisionRow.subject_symbol == query.subject_symbol)
        if query.period_end_from is not None:
            # Mirrors RevisionQueryFilters.matches: an unknown period end cannot
            # satisfy "ends on/after this date".
            statement = statement.where(DataRevisionRow.period_end >= query.period_end_from)
        if query.period_end_to is not None:
            statement = statement.where(
                or_(
                    DataRevisionRow.period_end.is_(None),
                    DataRevisionRow.period_end <= query.period_end_to,
                )
            )
        rows = self._session.scalars(
            statement.order_by(
                DataProvenanceRow.strategy_usable_at,
                DataRevisionRow.revision_sequence,
                DataRevisionRow.revision_id,
            ).limit(max(1, query.limit))
        ).all()
        return self._hydrate_loaded(rows)

    def list_all_for_dataset(self, dataset: str, *, limit: int = 10_000) -> list[LoadedRevision]:
        rows = self._session.scalars(
            select(DataRevisionRow)
            .where(DataRevisionRow.dataset == dataset)
            .order_by(
                DataRevisionRow.logical_key,
                DataRevisionRow.revision_sequence,
                DataRevisionRow.revision_id,
            )
            .limit(max(1, limit))
        ).all()
        return self._hydrate_loaded(rows)

    def _hydrate_loaded(self, revision_rows: Sequence[DataRevisionRow]) -> list[LoadedRevision]:
        if not revision_rows:
            return []
        provenance_ids = {row.provenance_id for row in revision_rows}
        provenance_rows = self._session.scalars(
            select(DataProvenanceRow).where(
                DataProvenanceRow.provenance_id.in_(list(provenance_ids))
            )
        ).all()
        by_id = {row.provenance_id: row for row in provenance_rows}
        loaded: list[LoadedRevision] = []
        for revision_row in revision_rows:
            provenance_row = by_id.get(revision_row.provenance_id)
            if provenance_row is None:  # pragma: no cover - FK makes this impossible
                continue
            loaded.append(loaded_revision_from_rows(revision_row, provenance_row))
        return loaded


class SqlAlchemyRevisionInvalidationRepository(_SessionRepository):
    """Append-only invalidation records."""

    def append(self, invalidation: RevisionInvalidation) -> bool:
        return _insert_immutable(
            self._session,
            row=make_row(
                DataRevisionInvalidationRow,
                revision_invalidation_to_values(invalidation),
            ),
            existing=lambda: self._get(invalidation.invalidation_id),
            incoming=invalidation,
            message="invalidation id already stored with different content",
            details={"invalidation_id": invalidation.invalidation_id},
        )

    def list_for_revision(self, revision_id: str) -> list[RevisionInvalidation]:
        rows = self._session.scalars(
            select(DataRevisionInvalidationRow)
            .where(DataRevisionInvalidationRow.revision_id == revision_id)
            .order_by(
                DataRevisionInvalidationRow.effective_at,
                DataRevisionInvalidationRow.invalidation_id,
            )
        ).all()
        return [revision_invalidation_from_row(row) for row in rows]

    def list_for_revisions(self, revision_ids: Sequence[str]) -> list[RevisionInvalidation]:
        if not revision_ids:
            return []
        rows = self._session.scalars(
            select(DataRevisionInvalidationRow)
            .where(DataRevisionInvalidationRow.revision_id.in_(list(dict.fromkeys(revision_ids))))
            .order_by(
                DataRevisionInvalidationRow.revision_id,
                DataRevisionInvalidationRow.effective_at,
                DataRevisionInvalidationRow.invalidation_id,
            )
        ).all()
        return [revision_invalidation_from_row(row) for row in rows]

    def _get(self, invalidation_id: str) -> RevisionInvalidation | None:
        row = self._session.get(DataRevisionInvalidationRow, invalidation_id)
        return None if row is None else revision_invalidation_from_row(row)


class SqlAlchemyIngestionRunRepository(_SessionRepository):
    """Append-only ingestion attempt/report log (header + issues atomically)."""

    def append(self, report: IngestionReport) -> bool:
        existing = self.get(report.ingestion_id)
        if existing is not None:
            return _replay_or_conflict(
                existing,
                report,
                message="ingestion run id already stored with different content",
                ingestion_id=report.ingestion_id,
            )
        try:
            with self._session.begin_nested():
                self._session.add(make_row(IngestionRunRow, ingestion_run_to_values(report)))
                self._flush()
                for ordinal, issue in enumerate(report.issues):
                    self._session.add(
                        make_row(
                            IngestionIssueRow,
                            ingestion_issue_to_values(report.ingestion_id, ordinal, issue),
                        )
                    )
                self._flush()
        except IntegrityError as exc:
            duplicate = self.get(report.ingestion_id)
            if duplicate is not None:
                return _replay_or_conflict(
                    duplicate,
                    report,
                    message="ingestion run id already stored with different content",
                    ingestion_id=report.ingestion_id,
                )
            raise IdempotencyConflictError(
                "ingestion report conflicts with an existing record",
                ingestion_id=report.ingestion_id,
            ) from exc
        return True

    def get(self, ingestion_id: str) -> IngestionReport | None:
        row = self._session.get(IngestionRunRow, ingestion_id)
        if row is None:
            return None
        issue_rows = self._session.scalars(
            select(IngestionIssueRow)
            .where(IngestionIssueRow.ingestion_id == ingestion_id)
            .order_by(IngestionIssueRow.ordinal)
        ).all()
        return ingestion_report_from_row(row, issue_rows)

    def list_recent(
        self,
        *,
        dataset: str | None = None,
        source_id: str | None = None,
        limit: int = 50,
    ) -> list[IngestionReport]:
        statement = select(IngestionRunRow)
        if dataset is not None:
            statement = statement.where(IngestionRunRow.dataset == dataset)
        if source_id is not None:
            statement = statement.where(IngestionRunRow.source_id == source_id)
        rows = self._session.scalars(
            statement.order_by(
                IngestionRunRow.started_at.desc(),
                IngestionRunRow.ingestion_id.desc(),
            ).limit(max(1, limit))
        ).all()
        return self._hydrate(rows)

    def _hydrate(self, rows: Sequence[IngestionRunRow]) -> list[IngestionReport]:
        if not rows:
            return []
        ingestion_ids = [row.ingestion_id for row in rows]
        issue_rows = self._session.scalars(
            select(IngestionIssueRow)
            .where(IngestionIssueRow.ingestion_id.in_(ingestion_ids))
            .order_by(IngestionIssueRow.ingestion_id, IngestionIssueRow.ordinal)
        ).all()
        grouped: dict[str, list[IngestionIssueRow]] = {
            ingestion_id: [] for ingestion_id in ingestion_ids
        }
        for issue_row in issue_rows:
            grouped.setdefault(issue_row.ingestion_id, []).append(issue_row)
        return [ingestion_report_from_row(row, grouped.get(row.ingestion_id, [])) for row in rows]


class SqlAlchemyProviderCapabilityRepository(_SessionRepository):
    """Append-only provider capability declarations."""

    @staticmethod
    def _same_declaration(left: ProviderCapabilities, right: ProviderCapabilities) -> bool:
        """Content equality for dedupe, ignoring the free-form notes.

        ``declared_at`` is compared as an instant (decided by the datetime type),
        so the same moment expressed in ``+08:00`` and in UTC is identical.
        """
        return (
            left.provider_id == right.provider_id
            and left.supported == right.supported
            and left.declaration_reason == right.declaration_reason
            and left.dataset_versions == right.dataset_versions
            and left.declared_at == right.declared_at
        )

    def append(self, capabilities: ProviderCapabilities) -> bool:
        latest = self.latest(capabilities.provider_id)
        if latest is not None and self._same_declaration(latest, capabilities):
            return False
        existing = self._get(capabilities.declaration_id)
        if existing is not None:
            if self._same_declaration(existing, capabilities):
                return False
            raise IdempotencyConflictError(
                "capability declaration id already stored with different content",
                declaration_id=capabilities.declaration_id,
            )
        try:
            with self._session.begin_nested():
                self._session.add(
                    make_row(
                        ProviderCapabilityRow,
                        provider_capabilities_to_values(capabilities),
                    )
                )
        except IntegrityError as exc:
            duplicate = self._get(capabilities.declaration_id)
            if duplicate is not None:
                if self._same_declaration(duplicate, capabilities):
                    return False
                raise IdempotencyConflictError(
                    "capability declaration id already stored with different content",
                    declaration_id=capabilities.declaration_id,
                ) from exc
            raise IdempotencyConflictError(
                "capability declaration conflicts with an existing record",
                declaration_id=capabilities.declaration_id,
            ) from exc
        return True

    def latest(self, provider_id: str) -> ProviderCapabilities | None:
        row = self._session.scalars(
            select(ProviderCapabilityRow)
            .where(ProviderCapabilityRow.provider_id == provider_id)
            .order_by(
                ProviderCapabilityRow.declared_at.desc(),
                ProviderCapabilityRow.declaration_id.desc(),
            )
            .limit(1)
        ).first()
        return None if row is None else provider_capabilities_from_row(row)

    def list_declarations(self, provider_id: str, *, limit: int = 50) -> list[ProviderCapabilities]:
        rows = self._session.scalars(
            select(ProviderCapabilityRow)
            .where(ProviderCapabilityRow.provider_id == provider_id)
            .order_by(
                ProviderCapabilityRow.declared_at.desc(),
                ProviderCapabilityRow.declaration_id.desc(),
            )
            .limit(max(1, limit))
        ).all()
        return [provider_capabilities_from_row(row) for row in rows]

    def _get(self, declaration_id: str) -> ProviderCapabilities | None:
        row = self._session.get(ProviderCapabilityRow, declaration_id)
        return None if row is None else provider_capabilities_from_row(row)
