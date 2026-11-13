"""SQLAlchemy 2.0 declarative schema for every persisted JANUS aggregate.

Design rules (Phase 0 spec s5, s10):

* Append-only ledgers: ``cash_ledger_entries``, ``position_events``, ``fills``,
  ``fee_lines``, ``risk_decisions``/``risk_checks`` and ``decision_records`` are
  written once and never updated. Their natural keys plus idempotency keys are
  UNIQUE so a replayed delivery cannot double-count.
* Account state is **versioned**, not mutated: ``accounts`` keeps one immutable
  row per ``(account_id, version)`` and the stable identity lives in
  ``account_ids`` so child tables can enforce a real ``ON DELETE RESTRICT``
  foreign key while history is preserved.
* Money is exact TEXT (see ``columns.py``); datetimes are aware UTC TEXT; enum
  columns store enum values.
* Foreign keys use ``ON DELETE RESTRICT``: parents that own immutable history
  can never be silently deleted.

Required UNIQUE constraints are declared explicitly::

    cash_ledger_entries(account_id, sequence), cash_ledger_entries(account_id, idempotency_key)
    position_events(account_id, sequence),     position_events(account_id, idempotency_key)
    orders(order_id), fills(fill_id), fills(idempotency_key)
    market_bars(symbol, trade_date), accounts(account_id, version)
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from janus.domain.common.enums import (
    AccountState,
    AdjustmentMode,
    Board,
    DataQualityStatus,
    Exchange,
    FeeComponent,
    InstrumentType,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidity,
    QuoteSource,
    RiskOutcome,
    SecurityStatus,
)
from janus.domain.common.money import Money
from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPrecision,
)
from janus.domain.data_foundation.capabilities import DataSourceKind
from janus.domain.data_foundation.quality import QualityIssueCode, QualityState
from janus.domain.data_foundation.revisions import ValueKind
from janus.domain.ledger.models import LedgerEntryType, PositionEventType
from janus.domain.market_rules.models import FeeBasis
from janus.domain.portfolio.models import AccountMode
from janus.domain.risk.models import RiskRuleId, RiskVerdict
from janus.domain.strategy.decision import DecisionType
from janus.infrastructure.database.columns import (
    DecimalText,
    JsonText,
    MoneyText,
    UtcDateTime,
    enum_column,
)
from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "AccountIdentityRow",
    "AccountRow",
    "Base",
    "CashLedgerEntryRow",
    "DataProvenanceRow",
    "DataRevisionInvalidationRow",
    "DataRevisionRow",
    "DataSnapshotRow",
    "DataSourceRow",
    "DecisionRecordRow",
    "FeeLineRow",
    "FillRow",
    "IngestionIssueRow",
    "IngestionRunRow",
    "InstrumentRow",
    "MarketBarRow",
    "OrderRow",
    "PortfolioPositionRow",
    "PortfolioSnapshotRow",
    "PositionEventRow",
    "ProviderCapabilityRow",
    "RawPayloadRow",
    "RiskCheckRow",
    "RiskDecisionRow",
    "table_names",
]

_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base; every table below shares one naming convention."""

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Security master and accounts
# ---------------------------------------------------------------------------


class InstrumentRow(Base):
    """Persisted :class:`janus.domain.instruments.models.Instrument`."""

    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    exchange: Mapped[Exchange] = mapped_column(enum_column(Exchange), nullable=False)
    board: Mapped[Board] = mapped_column(enum_column(Board), nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        enum_column(InstrumentType), nullable=False
    )
    status: Mapped[SecurityStatus] = mapped_column(enum_column(SecurityStatus), nullable=False)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    lot_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_etf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AccountIdentityRow(Base):
    """Stable account identity referenced by every account-scoped child row.

    ``accounts`` stores immutable versions, so ``account_id`` alone is not
    unique there. This master row exists only so foreign keys can enforce
    ``ON DELETE RESTRICT`` without losing version history.
    """

    __tablename__ = "account_ids"

    account_id: Mapped[str] = mapped_column(Text, primary_key=True)


class AccountRow(Base):
    """Persisted immutable version of :class:`PortfolioAccount`.

    The composite primary key is the ``accounts(account_id, version)``
    uniqueness guarantee: a version is written exactly once.
    """

    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("account_ids.account_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[AccountMode] = mapped_column(enum_column(AccountMode), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    initial_capital: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    initial_cash_ratio: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    allow_external_flows: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reset_requires_admin: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[AccountState] = mapped_column(enum_column(AccountState), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


# ---------------------------------------------------------------------------
# Append-only ledger
# ---------------------------------------------------------------------------


class CashLedgerEntryRow(Base):
    """Persisted immutable :class:`CashLedgerEntry`."""

    __tablename__ = "cash_ledger_entries"

    entry_id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("account_ids.account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        enum_column(LedgerEntryType), nullable=False
    )
    amount: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    balance_after: Mapped[Money | None] = mapped_column(MoneyText, nullable=True)

    __table_args__ = (
        UniqueConstraint("account_id", "sequence", name="uq_cash_ledger_entries_account_sequence"),
        UniqueConstraint(
            "account_id", "idempotency_key", name="uq_cash_ledger_entries_account_idempotency"
        ),
    )


class PositionEventRow(Base):
    """Persisted immutable :class:`PositionEvent`."""

    __tablename__ = "position_events"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("account_ids.account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[PositionEventType] = mapped_column(
        enum_column(PositionEventType), nullable=False
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text(200), nullable=False)
    available_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    unit_cost: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    cost_basis_delta: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    realized_pnl: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("account_id", "sequence", name="uq_position_events_account_sequence"),
        UniqueConstraint(
            "account_id", "idempotency_key", name="uq_position_events_account_idempotency"
        ),
        Index("ix_position_events_symbol", "symbol"),
    )


# ---------------------------------------------------------------------------
# Orders and fills
# ---------------------------------------------------------------------------


class OrderRow(Base):
    """Persisted :class:`PaperOrder` (mutable lifecycle, one row per order)."""

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("account_ids.account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[OrderSide] = mapped_column(enum_column(OrderSide), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    order_type: Mapped[OrderType] = mapped_column(enum_column(OrderType), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    validity: Mapped[OrderValidity] = mapped_column(enum_column(OrderValidity), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(enum_column(OrderStatus), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    target_trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    earliest_execution_date: Mapped[date] = mapped_column(Date, nullable=False)
    rule_set_id: Mapped[str] = mapped_column(Text, nullable=False)
    rule_set_version: Mapped[str] = mapped_column(Text, nullable=False)
    fee_schedule_id: Mapped[str] = mapped_column(Text, nullable=False)
    fee_schedule_version: Mapped[str] = mapped_column(Text, nullable=False)
    fill_model_version: Mapped[str] = mapped_column(Text, nullable=False)
    data_snapshot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        Index("ix_orders_account_status", "account_id", "status"),
        Index("ix_orders_account_target_date", "account_id", "target_trade_date"),
    )


class FillRow(Base):
    """Persisted immutable :class:`ExecutionFill` header.

    Itemised fee components live in ``fee_lines`` (individually queryable and
    losslessly reconstructed). The fee breakdown's own schedule references and
    notional are kept in ``fees_*`` columns so the nested value object round
    trips even if it ever differs from the fill header.
    """

    __tablename__ = "fills"

    fill_id: Mapped[str] = mapped_column(Text, primary_key=True)
    order_id: Mapped[str] = mapped_column(
        Text, ForeignKey("orders.order_id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("account_ids.account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[OrderSide] = mapped_column(enum_column(OrderSide), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    notional: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    slippage_bps: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fill_model_version: Mapped[str] = mapped_column(Text, nullable=False)
    rule_set_id: Mapped[str] = mapped_column(Text, nullable=False)
    rule_set_version: Mapped[str] = mapped_column(Text, nullable=False)
    fee_schedule_id: Mapped[str] = mapped_column(Text, nullable=False)
    fee_schedule_version: Mapped[str] = mapped_column(Text, nullable=False)
    data_snapshot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text(200), nullable=False, unique=True)
    # -- nested FeeBreakdown header -------------------------------------
    fees_schedule_id: Mapped[str] = mapped_column(Text, nullable=False)
    fees_schedule_version: Mapped[str] = mapped_column(Text, nullable=False)
    fees_notional: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)

    __table_args__ = (
        Index("ix_fills_order_id", "order_id"),
        Index("ix_fills_account_trade_date", "account_id", "trade_date"),
    )


class FeeLineRow(Base):
    """One itemised fee component of a fill, ordered by ``line_index``."""

    __tablename__ = "fee_lines"

    fill_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("fills.fill_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    line_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    component: Mapped[FeeComponent] = mapped_column(enum_column(FeeComponent), nullable=False)
    basis: Mapped[FeeBasis] = mapped_column(enum_column(FeeBasis), nullable=False)
    rate: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    amount: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)


# ---------------------------------------------------------------------------
# Risk and decision lineage
# ---------------------------------------------------------------------------


class RiskDecisionRow(Base):
    """Persisted immutable :class:`RiskDecision` header.

    ``account_id`` deliberately carries no foreign key: the risk controller is
    required to persist a fail-closed rejection even when the account itself is
    unknown (``RiskRuleId.ACCOUNT_ACTIVE``), and that lineage must not be
    discarded because of referential integrity.
    """

    __tablename__ = "risk_decisions"

    risk_decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    order_intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[RiskOutcome] = mapped_column(enum_column(RiskOutcome), nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    rule_set_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_set_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_snapshot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (Index("ix_risk_decisions_order_intent", "order_intent_id"),)


class RiskCheckRow(Base):
    """One deterministic check result attached to a risk decision."""

    __tablename__ = "risk_checks"

    risk_decision_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("risk_decisions.risk_decision_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    check_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[RiskRuleId] = mapped_column(enum_column(RiskRuleId), nullable=False)
    verdict: Mapped[RiskVerdict] = mapped_column(
        enum_column(RiskVerdict), nullable=False, server_default="PASS"
    )
    """Explicit PASS/FAIL/INDETERMINATE/NOT_APPLICABLE outcome.

    ``server_default`` keeps pre-existing Phase 0 rows readable after the
    ``0002_data_foundation`` migration adds the column.
    """

    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    measured: Mapped[str | None] = mapped_column(Text, nullable=True)
    threshold: Mapped[str | None] = mapped_column(Text, nullable=True)


class DecisionRecordRow(Base):
    """Persisted immutable :class:`DecisionRecord` (audit lineage).

    ``account_id`` has no foreign key: decision lineage is written for rejected
    and unauthenticated situations too (for example a proposal referencing an
    account that does not exist yet), and audit records must never be dropped
    because a referenced account is absent.
    """

    __tablename__ = "decision_records"

    decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_type: Mapped[DecisionType] = mapped_column(enum_column(DecisionType), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    as_of: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_snapshot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_set_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_set_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False, default=dict)
    thesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_refs: Mapped[list[str]] = mapped_column(JsonText, nullable=False, default=list)
    uncertainties: Mapped[list[str]] = mapped_column(JsonText, nullable=False, default=list)
    outcome: Mapped[str] = mapped_column(Text, nullable=False, default="")
    executable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_decision_records_created_at", "created_at"),)


# ---------------------------------------------------------------------------
# Provenance and valuation snapshots
# ---------------------------------------------------------------------------


class DataSnapshotRow(Base):
    """Persisted immutable :class:`DataSnapshot` provenance bundle."""

    __tablename__ = "data_snapshots"

    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    as_of: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    provider: Mapped[QuoteSource] = mapped_column(enum_column(QuoteSource), nullable=False)
    symbols: Mapped[list[str]] = mapped_column(JsonText, nullable=False)
    bar_dates: Mapped[list[str]] = mapped_column(JsonText, nullable=False)
    bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    quality: Mapped[DataQualityStatus] = mapped_column(
        enum_column(DataQualityStatus), nullable=False
    )
    rule_set_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (Index("ix_data_snapshots_created_at", "created_at"),)


class PortfolioSnapshotRow(Base):
    """Persisted :class:`PortfolioSnapshot` header (positions in child table)."""

    __tablename__ = "portfolio_snapshots"

    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("account_ids.account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    as_of: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    valuation_date: Mapped[date] = mapped_column(Date, nullable=False)
    cash: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    nav: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    unrealized_pnl: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    realized_pnl: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    oldest_price_as_of: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    any_price_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    data_snapshot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_set_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_portfolio_snapshots_account_as_of", "account_id", "as_of"),)


class PortfolioPositionRow(Base):
    """One valued position inside a persisted portfolio snapshot."""

    __tablename__ = "portfolio_positions"

    snapshot_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("portfolio_snapshots.snapshot_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    position_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    available_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_basis: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    market_price: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    market_value: Mapped[Money] = mapped_column(MoneyText, nullable=False)
    price_as_of: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    price_is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


class MarketBarRow(Base):
    """Persisted :class:`MarketBar` with its full point-in-time provenance.

    The composite primary key is the required ``market_bars(symbol,
    trade_date)`` uniqueness guarantee.
    """

    __tablename__ = "market_bars"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    high: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    low: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    close: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    adjustment: Mapped[AdjustmentMode] = mapped_column(enum_column(AdjustmentMode), nullable=False)
    quality: Mapped[DataQualityStatus] = mapped_column(
        enum_column(DataQualityStatus), nullable=False
    )
    source: Mapped[QuoteSource] = mapped_column(enum_column(QuoteSource), nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_payload_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_time: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    known_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (Index("ix_market_bars_trade_date", "trade_date"),)


# ---------------------------------------------------------------------------
# Data foundation (PR-001): immutable provenance, revisions and ingestion audit
# ---------------------------------------------------------------------------


class DataSourceRow(Base):
    """Registered origin of data (:class:`janus.domain.data_foundation.capabilities.DataSource`)."""

    __tablename__ = "data_sources"

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[DataSourceKind] = mapped_column(enum_column(DataSourceKind), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RawPayloadRow(Base):
    """Content-addressed archive of one provider response.

    ``content_hash`` is UNIQUE: identical bytes can never create a second archive
    entry, which is what makes raw-payload deduplication trustworthy.
    """

    __tablename__ = "raw_payloads"

    payload_id: Mapped[str] = mapped_column(Text, primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text(64), nullable=False, unique=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("data_sources.source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    request_scope: Mapped[dict[str, str]] = mapped_column(JsonText, nullable=False, default=dict)
    retrieved_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    payload_text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_raw_payloads_source_dataset", "source_id", "dataset"),)


class DataProvenanceRow(Base):
    """Immutable provenance for one ingested source record."""

    __tablename__ = "data_provenance"

    provenance_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("data_sources.source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    system_first_observed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    public_available_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    strategy_usable_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    availability_basis: Mapped[AvailabilityBasis] = mapped_column(
        enum_column(AvailabilityBasis), nullable=False
    )
    availability_precision: Mapped[AvailabilityPrecision] = mapped_column(
        enum_column(AvailabilityPrecision), nullable=False
    )
    strategy_usable_at_derivation: Mapped[AvailabilityBasis] = mapped_column(
        enum_column(AvailabilityBasis), nullable=False
    )
    source_revision: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text(64), nullable=False)

    __table_args__ = (
        # Provenance identity is **per ingestion attempt**: a re-ingestion of the
        # same bytes at a later instant records a new, auditable provenance row,
        # so ``ingested_at`` is part of the uniqueness key. An exact replay of the
        # same attempt still collides here (and on the deterministic
        # ``provenance_id`` primary key) and is deduplicated by the repository.
        UniqueConstraint(
            "source_id",
            "dataset",
            "source_record_id",
            "source_revision",
            "payload_hash",
            "ingested_at",
            name="uq_data_provenance_identity",
        ),
        Index(
            "ix_data_provenance_dataset_usable",
            "dataset",
            "strategy_usable_at",
        ),
        Index("ix_data_provenance_payload_hash", "payload_hash"),
    )


class DataRevisionRow(Base):
    """One immutable revision of one logical fact.

    Two uniqueness rules protect point-in-time correctness:

    * ``(logical_key, revision_sequence)`` — sequence slots are never reused;
    * ``(logical_key, source_revision, payload_hash)`` — re-ingesting the same
      source revision cannot create a phantom revision.
    """

    __tablename__ = "data_revisions"

    revision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    logical_key: Mapped[str] = mapped_column(Text, nullable=False)
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("data_sources.source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    subject_symbol: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    value_kind: Mapped[ValueKind] = mapped_column(enum_column(ValueKind), nullable=False)
    value_text: Mapped[str] = mapped_column(Text, nullable=False)
    value_unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_precision_digits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_from: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    provenance_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("data_provenance.provenance_id", ondelete="RESTRICT"),
        nullable=False,
    )
    payload_hash: Mapped[str] = mapped_column(Text(64), nullable=False)
    source_revision: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_revision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "logical_key", "revision_sequence", name="uq_data_revisions_logical_sequence"
        ),
        UniqueConstraint(
            "logical_key",
            "source_revision",
            "payload_hash",
            name="uq_data_revisions_identity",
        ),
        Index("ix_data_revisions_dataset_key", "dataset", "logical_key"),
        Index("ix_data_revisions_dataset_symbol", "dataset", "subject_symbol"),
        Index("ix_data_revisions_provenance", "provenance_id"),
    )


class DataRevisionInvalidationRow(Base):
    """Append-only record that a revision must not be used from a given instant."""

    __tablename__ = "data_revision_invalidations"

    invalidation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    revision_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("data_revisions.revision_id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_data_revision_invalidations_revision_effective",
            "revision_id",
            "effective_at",
        ),
    )


class IngestionRunRow(Base):
    """Append-only ingestion attempt/report header.

    ``source_id`` deliberately carries **no foreign key**: an ingestion run is an
    append-only audit log that must be able to record a rejected attempt naming an
    unregistered source (``UNKNOWN_SOURCE_IDENTITY``). Referential integrity for
    accepted data is enforced by the ``raw_payloads`` and ``data_provenance``
    foreign keys instead.
    """

    __tablename__ = "ingestion_runs"

    ingestion_id: Mapped[str] = mapped_column(Text, primary_key=True)
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    received_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    incomplete_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[QualityState] = mapped_column(enum_column(QualityState), nullable=False)
    provider_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload_hash: Mapped[str | None] = mapped_column(Text(64), nullable=True)

    __table_args__ = (Index("ix_ingestion_runs_dataset_started", "dataset", "started_at"),)


class IngestionIssueRow(Base):
    """One structured data-quality finding attached to an ingestion run."""

    __tablename__ = "ingestion_issues"

    issue_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ingestion_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("ingestion_runs.ingestion_id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[QualityIssueCode] = mapped_column(enum_column(QualityIssueCode), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    logical_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    field: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("ingestion_id", "ordinal", name="uq_ingestion_issues_ingestion_ordinal"),
    )


class ProviderCapabilityRow(Base):
    """One immutable provider capability declaration."""

    __tablename__ = "provider_capabilities"

    declaration_id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_id: Mapped[str] = mapped_column(Text, nullable=False)
    supported: Mapped[list[str]] = mapped_column(JsonText, nullable=False, default=list)
    dataset_versions: Mapped[dict[str, str]] = mapped_column(JsonText, nullable=False, default=dict)
    declared_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    declaration_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        Index(
            "ix_provider_capabilities_provider_declared",
            "provider_id",
            "declared_at",
        ),
    )


def table_names() -> tuple[str, ...]:
    """Every table in the declared schema (used by migration/CI checks)."""
    return tuple(sorted(Base.metadata.tables))
