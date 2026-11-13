"""Lossless domain-model <-> database-row converters.

Every mapper is a pure function so round-trip fidelity can be unit-tested
(``tests/integration/test_persistence_roundtrip.py``). Mappers are the only
place that knows the physical column layout; repositories never build domain
models inline.

``make_row``/``apply_values`` assign mapped columns by name. Attribution rather
than a keyword constructor is used deliberately: declarative ``__init__``
signatures are not statically typed without the SQLAlchemy mypy plugin (Phase 0
keeps ``plugins = []``), and every column is covered by the round-trip tests.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from janus.domain.common.time import PointInTime
from janus.domain.data_foundation.capabilities import (
    DataCapability,
    DataSource,
    ProviderCapabilities,
)
from janus.domain.data_foundation.provenance import (
    DataProvenance,
    ProvenanceTimestamps,
)
from janus.domain.data_foundation.quality import (
    IngestionReport,
    QualityIssue,
    QualityIssueCode,
)
from janus.domain.data_foundation.query import LoadedRevision
from janus.domain.data_foundation.raw import RawPayload
from janus.domain.data_foundation.revisions import (
    DataRevision,
    DataValue,
    RevisionInvalidation,
)
from janus.domain.instruments.models import Instrument
from janus.domain.ledger.models import CashLedgerEntry, PositionEvent
from janus.domain.market_data.models import DataSnapshot, MarketBar
from janus.domain.market_rules.models import FeeBreakdown, FeeLine
from janus.domain.orders.models import ExecutionFill, PaperOrder
from janus.domain.portfolio.models import PortfolioAccount, PortfolioSnapshot, Position
from janus.domain.risk.models import RiskCheck, RiskDecision
from janus.domain.strategy.decision import DecisionRecord
from janus.infrastructure.database.tables import (
    AccountRow,
    Base,
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

__all__ = [
    "account_from_row",
    "account_to_values",
    "cash_entry_from_row",
    "cash_entry_to_values",
    "data_provenance_from_row",
    "data_provenance_to_values",
    "data_revision_from_row",
    "data_revision_to_values",
    "data_snapshot_from_row",
    "data_snapshot_to_values",
    "data_source_from_row",
    "data_source_to_values",
    "data_value_from_row",
    "data_value_to_values",
    "decision_record_from_row",
    "decision_record_to_values",
    "fee_line_from_row",
    "fee_line_to_values",
    "fill_from_row",
    "fill_to_values",
    "ingestion_issue_from_row",
    "ingestion_issue_id",
    "ingestion_issue_to_values",
    "ingestion_report_from_row",
    "ingestion_run_to_values",
    "instrument_from_row",
    "instrument_to_values",
    "loaded_revision_from_rows",
    "make_row",
    "market_bar_from_row",
    "market_bar_to_values",
    "order_from_row",
    "order_to_values",
    "portfolio_position_from_row",
    "portfolio_position_to_values",
    "portfolio_snapshot_from_row",
    "portfolio_snapshot_to_values",
    "position_event_from_row",
    "position_event_to_values",
    "provider_capabilities_from_row",
    "provider_capabilities_to_values",
    "raw_payload_from_row",
    "raw_payload_to_values",
    "revision_invalidation_from_row",
    "revision_invalidation_to_values",
    "risk_check_from_row",
    "risk_check_to_values",
    "risk_decision_from_row",
    "risk_decision_to_values",
]


def make_row[RowT: Base](row_type: type[RowT], values: Mapping[str, Any]) -> RowT:
    """Instantiate a mapped row and assign every supplied column."""
    row = row_type()
    apply_values(row, values)
    return row


def apply_values(row: Any, values: Mapping[str, Any]) -> None:
    """Assign mapped column values onto an existing row (upsert path)."""
    for column, value in values.items():
        setattr(row, column, value)


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------


def instrument_to_values(instrument: Instrument) -> dict[str, Any]:
    return {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "exchange": instrument.exchange,
        "board": instrument.board,
        "instrument_type": instrument.instrument_type,
        "status": instrument.status,
        "listing_date": instrument.listing_date,
        "delisting_date": instrument.delisting_date,
        "lot_quantity": instrument.lot_quantity,
        "is_etf": instrument.is_etf,
    }


def instrument_from_row(row: InstrumentRow) -> Instrument:
    return Instrument(
        symbol=row.symbol,
        name=row.name,
        exchange=row.exchange,
        board=row.board,
        instrument_type=row.instrument_type,
        status=row.status,
        listing_date=row.listing_date,
        delisting_date=row.delisting_date,
        lot_quantity=row.lot_quantity,
        is_etf=row.is_etf,
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def account_to_values(account: PortfolioAccount) -> dict[str, Any]:
    return {
        "account_id": account.account_id,
        "version": account.version,
        "mode": account.mode,
        "currency": account.currency,
        "initial_capital": account.initial_capital,
        "initial_cash_ratio": account.initial_cash_ratio,
        "allow_external_flows": account.allow_external_flows,
        "reset_requires_admin": account.reset_requires_admin,
        "state": account.state,
        "created_at": account.created_at,
        "pause_reason": account.pause_reason,
        "paused_at": account.paused_at,
    }


def account_from_row(row: AccountRow) -> PortfolioAccount:
    return PortfolioAccount(
        account_id=row.account_id,
        version=row.version,
        mode=row.mode,
        currency=row.currency,
        initial_capital=row.initial_capital,
        initial_cash_ratio=row.initial_cash_ratio,
        allow_external_flows=row.allow_external_flows,
        reset_requires_admin=row.reset_requires_admin,
        state=row.state,
        created_at=row.created_at,
        pause_reason=row.pause_reason,
        paused_at=row.paused_at,
    )


# ---------------------------------------------------------------------------
# Append-only ledger
# ---------------------------------------------------------------------------


def cash_entry_to_values(entry: CashLedgerEntry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "account_id": entry.account_id,
        "sequence": entry.sequence,
        "entry_type": entry.entry_type,
        "amount": entry.amount,
        "trade_date": entry.trade_date,
        "occurred_at": entry.occurred_at,
        "source_ref": entry.source_ref,
        "idempotency_key": entry.idempotency_key,
        "description": entry.description,
        "balance_after": entry.balance_after,
    }


def cash_entry_from_row(row: CashLedgerEntryRow) -> CashLedgerEntry:
    return CashLedgerEntry(
        entry_id=row.entry_id,
        account_id=row.account_id,
        sequence=row.sequence,
        entry_type=row.entry_type,
        amount=row.amount,
        trade_date=row.trade_date,
        occurred_at=row.occurred_at,
        source_ref=row.source_ref,
        idempotency_key=row.idempotency_key,
        description=row.description,
        balance_after=row.balance_after,
    )


def position_event_to_values(event: PositionEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "account_id": event.account_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "symbol": event.symbol,
        "quantity_delta": event.quantity_delta,
        "trade_date": event.trade_date,
        "occurred_at": event.occurred_at,
        "source_ref": event.source_ref,
        "idempotency_key": event.idempotency_key,
        "available_date": event.available_date,
        "unit_cost": event.unit_cost,
        "cost_basis_delta": event.cost_basis_delta,
        "realized_pnl": event.realized_pnl,
        "description": event.description,
    }


def position_event_from_row(row: PositionEventRow) -> PositionEvent:
    return PositionEvent(
        event_id=row.event_id,
        account_id=row.account_id,
        sequence=row.sequence,
        event_type=row.event_type,
        symbol=row.symbol,
        quantity_delta=row.quantity_delta,
        trade_date=row.trade_date,
        occurred_at=row.occurred_at,
        source_ref=row.source_ref,
        idempotency_key=row.idempotency_key,
        available_date=row.available_date,
        unit_cost=row.unit_cost,
        cost_basis_delta=row.cost_basis_delta,
        realized_pnl=row.realized_pnl,
        description=row.description,
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def order_to_values(order: PaperOrder) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "intent_id": order.intent_id,
        "account_id": order.account_id,
        "symbol": order.symbol,
        "side": order.side,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "order_type": order.order_type,
        "limit_price": order.limit_price,
        "validity": order.validity,
        "status": order.status,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "target_trade_date": order.target_trade_date,
        "earliest_execution_date": order.earliest_execution_date,
        "rule_set_id": order.rule_set_id,
        "rule_set_version": order.rule_set_version,
        "fee_schedule_id": order.fee_schedule_id,
        "fee_schedule_version": order.fee_schedule_version,
        "fill_model_version": order.fill_model_version,
        "data_snapshot_id": order.data_snapshot_id,
        "decision_id": order.decision_id,
        "strategy_id": order.strategy_id,
        "strategy_version": order.strategy_version,
        "risk_decision_id": order.risk_decision_id,
        "rejection_reason": order.rejection_reason,
        "cancel_reason": order.cancel_reason,
        "approved_at": order.approved_at,
        "submitted_at": order.submitted_at,
        "terminal_at": order.terminal_at,
    }


def order_from_row(row: OrderRow) -> PaperOrder:
    return PaperOrder(
        order_id=row.order_id,
        intent_id=row.intent_id,
        account_id=row.account_id,
        symbol=row.symbol,
        side=row.side,
        quantity=row.quantity,
        filled_quantity=row.filled_quantity,
        order_type=row.order_type,
        limit_price=row.limit_price,
        validity=row.validity,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        target_trade_date=row.target_trade_date,
        earliest_execution_date=row.earliest_execution_date,
        rule_set_id=row.rule_set_id,
        rule_set_version=row.rule_set_version,
        fee_schedule_id=row.fee_schedule_id,
        fee_schedule_version=row.fee_schedule_version,
        fill_model_version=row.fill_model_version,
        data_snapshot_id=row.data_snapshot_id,
        decision_id=row.decision_id,
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        risk_decision_id=row.risk_decision_id,
        rejection_reason=row.rejection_reason,
        cancel_reason=row.cancel_reason,
        approved_at=row.approved_at,
        submitted_at=row.submitted_at,
        terminal_at=row.terminal_at,
    )


# ---------------------------------------------------------------------------
# Fills and their fee lines
# ---------------------------------------------------------------------------


def fill_to_values(fill: ExecutionFill) -> dict[str, Any]:
    return {
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "account_id": fill.account_id,
        "symbol": fill.symbol,
        "side": fill.side,
        "quantity": fill.quantity,
        "price": fill.price,
        "notional": fill.notional,
        "executed_at": fill.executed_at,
        "trade_date": fill.trade_date,
        "slippage_bps": fill.slippage_bps,
        "is_partial": fill.is_partial,
        "fill_model_version": fill.fill_model_version,
        "rule_set_id": fill.rule_set_id,
        "rule_set_version": fill.rule_set_version,
        "fee_schedule_id": fill.fee_schedule_id,
        "fee_schedule_version": fill.fee_schedule_version,
        "data_snapshot_id": fill.data_snapshot_id,
        "decision_id": fill.decision_id,
        "idempotency_key": fill.idempotency_key,
        "fees_schedule_id": fill.fees.fee_schedule_id,
        "fees_schedule_version": fill.fees.fee_schedule_version,
        "fees_notional": fill.fees.notional,
    }


def fee_line_to_values(fill_id: str, line_index: int, line: FeeLine) -> dict[str, Any]:
    return {
        "fill_id": fill_id,
        "line_index": line_index,
        "component": line.component,
        "basis": line.basis,
        "rate": line.rate,
        "amount": line.amount,
    }


def fee_line_from_row(row: FeeLineRow) -> FeeLine:
    return FeeLine(
        component=row.component,
        basis=row.basis,
        rate=row.rate,
        amount=row.amount,
    )


def fill_from_row(row: FillRow, fee_rows: Sequence[FeeLineRow]) -> ExecutionFill:
    fees = FeeBreakdown(
        fee_schedule_id=row.fees_schedule_id,
        fee_schedule_version=row.fees_schedule_version,
        side=row.side,
        notional=row.fees_notional,
        lines=tuple(fee_line_from_row(fee_row) for fee_row in fee_rows),
    )
    return ExecutionFill(
        fill_id=row.fill_id,
        order_id=row.order_id,
        account_id=row.account_id,
        symbol=row.symbol,
        side=row.side,
        quantity=row.quantity,
        price=row.price,
        notional=row.notional,
        fees=fees,
        executed_at=row.executed_at,
        trade_date=row.trade_date,
        slippage_bps=row.slippage_bps,
        is_partial=row.is_partial,
        fill_model_version=row.fill_model_version,
        rule_set_id=row.rule_set_id,
        rule_set_version=row.rule_set_version,
        fee_schedule_id=row.fee_schedule_id,
        fee_schedule_version=row.fee_schedule_version,
        data_snapshot_id=row.data_snapshot_id,
        decision_id=row.decision_id,
        idempotency_key=row.idempotency_key,
    )


# ---------------------------------------------------------------------------
# Risk decisions
# ---------------------------------------------------------------------------


def risk_decision_to_values(decision: RiskDecision) -> dict[str, Any]:
    return {
        "risk_decision_id": decision.risk_decision_id,
        "order_intent_id": decision.order_intent_id,
        "account_id": decision.account_id,
        "outcome": decision.outcome,
        "policy_version": decision.policy_version,
        "decided_at": decision.decided_at,
        "rule_set_id": decision.rule_set_id,
        "rule_set_version": decision.rule_set_version,
        "data_snapshot_id": decision.data_snapshot_id,
        "summary": decision.summary,
    }


def risk_check_to_values(
    risk_decision_id: str, check_index: int, check: RiskCheck
) -> dict[str, Any]:
    return {
        "risk_decision_id": risk_decision_id,
        "check_index": check_index,
        "rule_id": check.rule_id,
        "verdict": check.verdict,
        "passed": check.passed,
        "message": check.message,
        "measured": check.measured,
        "threshold": check.threshold,
    }


def risk_check_from_row(row: RiskCheckRow) -> RiskCheck:
    return RiskCheck(
        rule_id=row.rule_id,
        verdict=row.verdict,
        passed=row.passed,
        message=row.message,
        measured=row.measured,
        threshold=row.threshold,
    )


def risk_decision_from_row(
    row: RiskDecisionRow, check_rows: Sequence[RiskCheckRow]
) -> RiskDecision:
    return RiskDecision(
        risk_decision_id=row.risk_decision_id,
        order_intent_id=row.order_intent_id,
        account_id=row.account_id,
        outcome=row.outcome,
        checks=tuple(risk_check_from_row(check_row) for check_row in check_rows),
        policy_version=row.policy_version,
        decided_at=row.decided_at,
        rule_set_id=row.rule_set_id,
        rule_set_version=row.rule_set_version,
        data_snapshot_id=row.data_snapshot_id,
        summary=row.summary,
    )


# ---------------------------------------------------------------------------
# Decision lineage and data provenance
# ---------------------------------------------------------------------------


def decision_record_to_values(record: DecisionRecord) -> dict[str, Any]:
    return {
        "decision_id": record.decision_id,
        "decision_type": record.decision_type,
        "created_at": record.created_at,
        "as_of": record.as_of,
        "account_id": record.account_id,
        "proposal_id": record.proposal_id,
        "strategy_id": record.strategy_id,
        "strategy_version": record.strategy_version,
        "model_id": record.model_id,
        "prompt_version": record.prompt_version,
        "llm_request_id": record.llm_request_id,
        "data_snapshot_id": record.data_snapshot_id,
        "rule_set_id": record.rule_set_id,
        "rule_set_version": record.rule_set_version,
        "risk_decision_id": record.risk_decision_id,
        "inputs": dict(record.inputs),
        "thesis": record.thesis,
        "evidence_refs": list(record.evidence_refs),
        "uncertainties": list(record.uncertainties),
        "outcome": record.outcome,
        "executable": record.executable,
    }


def decision_record_from_row(row: DecisionRecordRow) -> DecisionRecord:
    return DecisionRecord(
        decision_id=row.decision_id,
        decision_type=row.decision_type,
        created_at=row.created_at,
        as_of=row.as_of,
        account_id=row.account_id,
        proposal_id=row.proposal_id,
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        model_id=row.model_id,
        prompt_version=row.prompt_version,
        llm_request_id=row.llm_request_id,
        data_snapshot_id=row.data_snapshot_id,
        rule_set_id=row.rule_set_id,
        rule_set_version=row.rule_set_version,
        risk_decision_id=row.risk_decision_id,
        inputs=dict(row.inputs),
        thesis=row.thesis,
        evidence_refs=tuple(row.evidence_refs),
        uncertainties=tuple(row.uncertainties),
        outcome=row.outcome,
        executable=row.executable,
    )


def data_snapshot_to_values(snapshot: DataSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "created_at": snapshot.created_at,
        "as_of": snapshot.as_of,
        "provider": snapshot.provider,
        "symbols": list(snapshot.symbols),
        "bar_dates": [bar_date.isoformat() for bar_date in snapshot.bar_dates],
        "bar_count": snapshot.bar_count,
        "content_hash": snapshot.content_hash,
        "quality": snapshot.quality,
        "rule_set_id": snapshot.rule_set_id,
        "notes": snapshot.notes,
    }


def data_snapshot_from_row(row: DataSnapshotRow) -> DataSnapshot:
    return DataSnapshot(
        snapshot_id=row.snapshot_id,
        created_at=row.created_at,
        as_of=row.as_of,
        provider=row.provider,
        symbols=tuple(row.symbols),
        bar_dates=tuple(date.fromisoformat(str(bar_date)) for bar_date in row.bar_dates),
        bar_count=row.bar_count,
        content_hash=row.content_hash,
        quality=row.quality,
        rule_set_id=row.rule_set_id,
        notes=row.notes,
    )


# ---------------------------------------------------------------------------
# Portfolio snapshots
# ---------------------------------------------------------------------------


def portfolio_snapshot_to_values(snapshot: PortfolioSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "account_id": snapshot.account_id,
        "as_of": snapshot.as_of,
        "valuation_date": snapshot.valuation_date,
        "cash": snapshot.cash,
        "nav": snapshot.nav,
        "unrealized_pnl": snapshot.unrealized_pnl,
        "realized_pnl": snapshot.realized_pnl,
        "oldest_price_as_of": snapshot.oldest_price_as_of,
        "any_price_stale": snapshot.any_price_stale,
        "data_snapshot_id": snapshot.data_snapshot_id,
        "rule_set_id": snapshot.rule_set_id,
    }


def portfolio_position_to_values(
    snapshot_id: str, position_index: int, position: Position
) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "position_index": position_index,
        "symbol": position.symbol,
        "quantity": position.quantity,
        "available_quantity": position.available_quantity,
        "cost_basis": position.cost_basis,
        "market_price": position.market_price,
        "market_value": position.market_value,
        "price_as_of": position.price_as_of,
        "price_is_stale": position.price_is_stale,
    }


def portfolio_position_from_row(row: PortfolioPositionRow) -> Position:
    return Position(
        symbol=row.symbol,
        quantity=row.quantity,
        available_quantity=row.available_quantity,
        cost_basis=row.cost_basis,
        market_price=row.market_price,
        market_value=row.market_value,
        price_as_of=row.price_as_of,
        price_is_stale=row.price_is_stale,
    )


def portfolio_snapshot_from_row(
    row: PortfolioSnapshotRow, position_rows: Sequence[PortfolioPositionRow]
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_id=row.snapshot_id,
        account_id=row.account_id,
        as_of=row.as_of,
        valuation_date=row.valuation_date,
        cash=row.cash,
        positions=tuple(
            portfolio_position_from_row(position_row) for position_row in position_rows
        ),
        nav=row.nav,
        unrealized_pnl=row.unrealized_pnl,
        realized_pnl=row.realized_pnl,
        oldest_price_as_of=row.oldest_price_as_of,
        any_price_stale=row.any_price_stale,
        data_snapshot_id=row.data_snapshot_id,
        rule_set_id=row.rule_set_id,
    )


# ---------------------------------------------------------------------------
# Market bars
# ---------------------------------------------------------------------------


def market_bar_to_values(bar: MarketBar) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "trade_date": bar.trade_date,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "amount": bar.amount,
        "adjustment": bar.adjustment,
        "quality": bar.quality,
        "source": bar.source,
        "source_ref": bar.source_ref,
        "raw_payload_hash": bar.raw_payload_hash,
        "event_time": bar.point_in_time.event_time,
        "published_at": bar.point_in_time.published_at,
        "retrieved_at": bar.point_in_time.retrieved_at,
        "known_at": bar.point_in_time.known_at,
        "effective_from": bar.point_in_time.effective_from,
        "effective_to": bar.point_in_time.effective_to,
    }


def market_bar_from_row(row: MarketBarRow) -> MarketBar:
    return MarketBar(
        symbol=row.symbol,
        trade_date=row.trade_date,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        amount=row.amount,
        adjustment=row.adjustment,
        quality=row.quality,
        source=row.source,
        source_ref=row.source_ref,
        raw_payload_hash=row.raw_payload_hash,
        point_in_time=PointInTime(
            event_time=row.event_time,
            published_at=row.published_at,
            retrieved_at=row.retrieved_at,
            known_at=row.known_at,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
        ),
    )


# ---------------------------------------------------------------------------
# Data foundation (PR-001): sources, raw payloads, provenance, revisions,
# invalidations, ingestion audit, capability declarations
# ---------------------------------------------------------------------------


def data_source_to_values(source: DataSource) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "kind": source.kind,
        "display_name": source.display_name,
        "registered_at": source.registered_at,
        "notes": source.notes,
    }


def data_source_from_row(row: DataSourceRow) -> DataSource:
    return DataSource(
        source_id=row.source_id,
        kind=row.kind,
        display_name=row.display_name,
        registered_at=row.registered_at,
        notes=row.notes,
    )


def raw_payload_to_values(payload: RawPayload) -> dict[str, Any]:
    return {
        "payload_id": payload.payload_id,
        "content_hash": payload.content_hash,
        "byte_size": payload.byte_size,
        "media_type": payload.media_type,
        "source_id": payload.source_id,
        "dataset": payload.dataset,
        "request_scope": dict(payload.request_scope),
        "retrieved_at": payload.retrieved_at,
        "ingested_at": payload.ingested_at,
        "payload_text": payload.payload_text,
    }


def raw_payload_from_row(row: RawPayloadRow) -> RawPayload:
    return RawPayload(
        payload_id=row.payload_id,
        content_hash=row.content_hash,
        byte_size=row.byte_size,
        media_type=row.media_type,
        source_id=row.source_id,
        dataset=row.dataset,
        request_scope=dict(row.request_scope),
        retrieved_at=row.retrieved_at,
        ingested_at=row.ingested_at,
        payload_text=row.payload_text,
    )


def data_provenance_to_values(provenance: DataProvenance) -> dict[str, Any]:
    timestamps = provenance.timestamps
    return {
        "provenance_id": provenance.provenance_id,
        "source_id": provenance.source_id,
        "dataset": provenance.dataset,
        "source_record_id": provenance.source_record_id,
        "event_at": timestamps.event_at,
        "published_at": timestamps.published_at,
        "retrieved_at": timestamps.retrieved_at,
        "ingested_at": timestamps.ingested_at,
        "system_first_observed_at": timestamps.system_first_observed_at,
        "public_available_at": provenance.public_available_at,
        "strategy_usable_at": provenance.strategy_usable_at,
        "availability_basis": provenance.availability_basis,
        "availability_precision": provenance.availability_precision,
        "strategy_usable_at_derivation": provenance.strategy_usable_at_derivation,
        "source_revision": provenance.source_revision,
        "knowledge_policy_version": provenance.knowledge_policy_version,
        "payload_hash": provenance.payload_hash,
    }


def data_provenance_from_row(row: DataProvenanceRow) -> DataProvenance:
    return DataProvenance(
        provenance_id=row.provenance_id,
        source_id=row.source_id,
        dataset=row.dataset,
        source_record_id=row.source_record_id,
        timestamps=ProvenanceTimestamps(
            event_at=row.event_at,
            published_at=row.published_at,
            retrieved_at=row.retrieved_at,
            ingested_at=row.ingested_at,
            system_first_observed_at=row.system_first_observed_at,
        ),
        public_available_at=row.public_available_at,
        strategy_usable_at=row.strategy_usable_at,
        availability_basis=row.availability_basis,
        availability_precision=row.availability_precision,
        strategy_usable_at_derivation=row.strategy_usable_at_derivation,
        source_revision=row.source_revision,
        knowledge_policy_version=row.knowledge_policy_version,
        payload_hash=row.payload_hash,
    )


def data_value_to_values(value: DataValue) -> dict[str, Any]:
    """Flatten a :class:`DataValue` onto the three ``data_revisions.value_*`` columns."""
    return {
        "value_kind": value.kind,
        "value_text": value.text,
        "value_unit": value.unit,
        "value_precision_digits": value.precision_digits,
    }


def data_value_from_row(row: DataRevisionRow) -> DataValue:
    return DataValue(
        kind=row.value_kind,
        text=row.value_text,
        unit=row.value_unit,
        precision_digits=row.value_precision_digits,
    )


def data_revision_to_values(revision: DataRevision) -> dict[str, Any]:
    return {
        "revision_id": revision.revision_id,
        "logical_key": revision.logical_key,
        "dataset": revision.dataset,
        "source_id": revision.source_id,
        "subject_symbol": revision.subject_symbol,
        "revision_sequence": revision.revision_sequence,
        **data_value_to_values(revision.value),
        "period_start": revision.period_start,
        "period_end": revision.period_end,
        "effective_from": revision.effective_from,
        "effective_to": revision.effective_to,
        "provenance_id": revision.provenance_id,
        "payload_hash": revision.payload_hash,
        "source_revision": revision.source_revision,
        "supersedes_revision_id": revision.supersedes_revision_id,
        "ingested_at": revision.ingested_at,
    }


def data_revision_from_row(row: DataRevisionRow) -> DataRevision:
    return DataRevision(
        revision_id=row.revision_id,
        logical_key=row.logical_key,
        dataset=row.dataset,
        source_id=row.source_id,
        subject_symbol=row.subject_symbol,
        revision_sequence=row.revision_sequence,
        value=data_value_from_row(row),
        period_start=row.period_start,
        period_end=row.period_end,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        provenance_id=row.provenance_id,
        payload_hash=row.payload_hash,
        source_revision=row.source_revision,
        supersedes_revision_id=row.supersedes_revision_id,
        ingested_at=row.ingested_at,
    )


def revision_invalidation_to_values(
    invalidation: RevisionInvalidation,
) -> dict[str, Any]:
    return {
        "invalidation_id": invalidation.invalidation_id,
        "revision_id": invalidation.revision_id,
        "reason": invalidation.reason,
        "effective_at": invalidation.effective_at,
        "recorded_at": invalidation.recorded_at,
        "policy_version": invalidation.policy_version,
        "decision_id": invalidation.decision_id,
    }


def revision_invalidation_from_row(
    row: DataRevisionInvalidationRow,
) -> RevisionInvalidation:
    return RevisionInvalidation(
        invalidation_id=row.invalidation_id,
        revision_id=row.revision_id,
        reason=row.reason,
        effective_at=row.effective_at,
        recorded_at=row.recorded_at,
        policy_version=row.policy_version,
        decision_id=row.decision_id,
    )


def ingestion_issue_id(
    ingestion_id: str, ordinal: int, code: QualityIssueCode, logical_key: str | None
) -> str:
    """Deterministic child-row id: ``issue-<sha256(ingestion|ordinal|code|key)[:32]>``."""
    material = f"{ingestion_id}|{ordinal}|{code.value}|{logical_key or ''}"
    return f"issue-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def ingestion_issue_to_values(
    ingestion_id: str, ordinal: int, issue: QualityIssue
) -> dict[str, Any]:
    return {
        "issue_id": ingestion_issue_id(ingestion_id, ordinal, issue.code, issue.logical_key),
        "ingestion_id": ingestion_id,
        "ordinal": ordinal,
        "code": issue.code,
        "message": issue.message,
        "logical_key": issue.logical_key,
        "field": issue.field,
    }


def ingestion_issue_from_row(row: IngestionIssueRow) -> QualityIssue:
    return QualityIssue(
        code=row.code,
        message=row.message,
        logical_key=row.logical_key,
        field=row.field,
    )


def ingestion_run_to_values(report: IngestionReport) -> dict[str, Any]:
    return {
        "ingestion_id": report.ingestion_id,
        "dataset": report.dataset,
        "source_id": report.source_id,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "received_rows": report.received_rows,
        "accepted_rows": report.accepted_rows,
        "incomplete_rows": report.incomplete_rows,
        "rejected_rows": report.rejected_rows,
        "duplicate_rows": report.duplicate_rows,
        "status": report.status,
        "provider_failure": report.provider_failure,
        "failure_detail": report.failure_detail,
        "payload_hash": report.payload_hash,
    }


def ingestion_report_from_row(
    row: IngestionRunRow, issue_rows: Sequence[IngestionIssueRow]
) -> IngestionReport:
    return IngestionReport(
        ingestion_id=row.ingestion_id,
        dataset=row.dataset,
        source_id=row.source_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        received_rows=row.received_rows,
        accepted_rows=row.accepted_rows,
        incomplete_rows=row.incomplete_rows,
        rejected_rows=row.rejected_rows,
        duplicate_rows=row.duplicate_rows,
        status=row.status,
        provider_failure=row.provider_failure,
        failure_detail=row.failure_detail,
        issues=tuple(ingestion_issue_from_row(issue_row) for issue_row in issue_rows),
        payload_hash=row.payload_hash,
    )


def provider_capabilities_to_values(
    capabilities: ProviderCapabilities,
) -> dict[str, Any]:
    return {
        "declaration_id": capabilities.declaration_id,
        "provider_id": capabilities.provider_id,
        "supported": sorted(item.value for item in capabilities.supported),
        "dataset_versions": dict(capabilities.dataset_versions),
        "declared_at": capabilities.declared_at,
        "declaration_reason": capabilities.declaration_reason,
        "notes": capabilities.notes,
    }


def provider_capabilities_from_row(
    row: ProviderCapabilityRow,
) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_id=row.provider_id,
        supported=frozenset(DataCapability(value) for value in row.supported),
        declared_at=row.declared_at,
        declaration_reason=row.declaration_reason,
        notes=row.notes,
        dataset_versions=dict(row.dataset_versions),
    )


def loaded_revision_from_rows(
    revision_row: DataRevisionRow, provenance_row: DataProvenanceRow
) -> LoadedRevision:
    """Bind a revision to its authorising provenance row."""
    return LoadedRevision(
        revision=data_revision_from_row(revision_row),
        provenance=data_provenance_from_row(provenance_row),
    )
