"""Deterministic ledger replay.

This module is the single source of truth for account reconstruction:
acceptance criterion 3 requires that rebuilding from the opening event plus the
ledger/fill history reproduces stored cash and positions *exactly*.

Design notes
------------
* Replay is a pure function of persisted, immutable entries.
* Every input carries an idempotency key; duplicate deliveries are collapsed,
  and a conflicting reuse of the same key with different content is a hard
  error (acceptance criterion 4).
* Cash may never go negative in a Phase 0 CNY cash account (no margin). A
  negative intermediate balance is an accounting invariant violation.
* Lots are consumed FIFO by ``(available_date, acquired_trade_date, lot_id)`` so
  that reconstructions are stable and auditable.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from janus.domain.common.errors import (
    DataQualityError,
    IdempotencyConflictError,
    InvariantViolation,
)
from janus.domain.common.money import ZERO_CNY, Money
from janus.domain.ledger.models import (
    CashLedgerEntry,
    LedgerEntryType,
    PositionEvent,
    PositionEventType,
    RemainingLot,
)

__all__ = [
    "CashReplay",
    "RebuiltAccount",
    "RebuiltPosition",
    "available_quantity_on",
    "rebuild_account",
    "replay_cash",
    "replay_positions",
    "sellable_lots_on",
]


_LedgerRow = CashLedgerEntry | PositionEvent


@dataclass(frozen=True, slots=True)
class CashReplay:
    """Result of replaying cash entries."""

    balance: Money
    entry_count: int
    total_debits: Money
    total_credits: Money


@dataclass(frozen=True, slots=True)
class RebuiltPosition:
    """A position reconstructed from position events."""

    symbol: str
    quantity: int
    available_quantity: int
    cost_basis: Money
    lots: tuple[RemainingLot, ...]

    def describe(self) -> str:
        return (
            f"{self.symbol}: qty={self.quantity} available={self.available_quantity} "
            f"cost={self.cost_basis}"
        )


@dataclass(frozen=True, slots=True)
class RebuiltAccount:
    """Fully rebuilt account state."""

    opening_capital: Money
    cash: Money
    positions: Mapping[str, RebuiltPosition]
    realized_pnl: Money
    cash_entry_count: int
    position_event_count: int
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_cost_basis(self) -> Money:
        total = sum((p.cost_basis.amount for p in self.positions.values()), Decimal("0"))
        return Money(amount=total)


def _check_sequences(rows: Sequence[int], kind: str) -> None:
    if len(rows) != len(set(rows)):
        raise DataQualityError(f"duplicate {kind} sequence numbers in ledger history")
    ordered = sorted(rows)
    for previous, current in itertools.pairwise(ordered):
        if current != previous + 1:
            raise DataQualityError(
                f"{kind} sequence has a gap; ledger history is incomplete",
                previous=previous,
                current=current,
            )


def _dedupe[RowT: (CashLedgerEntry, PositionEvent)](rows: Sequence[RowT]) -> list[RowT]:
    """Collapse duplicated deliveries, erroring on conflicting reuse of a key."""
    seen: dict[str, dict[str, object]] = {}
    result: list[RowT] = []
    for row in rows:
        key = row.idempotency_key
        keyed = row.model_dump(mode="json")
        if key in seen:
            if seen[key] != keyed:
                raise IdempotencyConflictError(
                    "idempotency key reused with different content", idempotency_key=key
                )
            continue
        seen[key] = keyed
        result.append(row)
    return result


def replay_cash(
    entries: Iterable[CashLedgerEntry],
    *,
    allow_negative: bool = False,
) -> CashReplay:
    """Replay cash entries in sequence order and return the closing balance."""
    ordered = sorted(entries, key=lambda e: e.sequence)
    # Duplicate delivery is collapsed *before* the sequence check: a re-delivered
    # event legitimately carries the same sequence number.
    deduped = _dedupe(ordered)
    _check_sequences([e.sequence for e in deduped], "cash ledger entry")

    balance = ZERO_CNY
    debits = ZERO_CNY
    credits = ZERO_CNY
    for entry in deduped:
        balance = balance + entry.amount
        if entry.amount.is_negative():
            debits = debits + (-entry.amount)
        else:
            credits = credits + entry.amount
        if entry.balance_after is not None and entry.balance_after.amount != balance.amount:
            raise InvariantViolation(
                "cached balance_after disagrees with replayed balance",
                entry_id=entry.entry_id,
                cached=str(entry.balance_after),
                replayed=str(balance),
            )
        if not allow_negative and balance.amount < 0:
            raise InvariantViolation(
                "cash balance would become negative; a CNY cash account cannot borrow",
                entry_id=entry.entry_id,
                balance=str(balance),
            )
    return CashReplay(
        balance=balance,
        entry_count=len(deduped),
        total_debits=debits,
        total_credits=credits,
    )


def replay_positions(
    events: Iterable[PositionEvent],
    *,
    as_of: date | None = None,
    allow_negative: bool = False,
) -> dict[str, RebuiltPosition]:
    """Replay position events into lots and aggregate per symbol.

    ``as_of`` selects the availability date used for ``available_quantity``.
    When omitted, every held lot is reported as available (the caller then only
    gets a truthful total, not a settlement statement).
    """
    ordered = sorted(events, key=lambda e: e.sequence)
    deduped = _dedupe(ordered)
    _check_sequences([e.sequence for e in deduped], "position event")

    lots_by_symbol: dict[str, list[RemainingLot]] = {}
    realized = ZERO_CNY

    for event in deduped:
        lots = lots_by_symbol.setdefault(event.symbol, [])
        if event.is_acquisition:
            if event.available_date is None or event.unit_cost is None:
                raise DataQualityError(
                    "acquisition event is missing lot metadata",
                    event_id=event.event_id,
                )
            lots.append(
                RemainingLot(
                    lot_id=f"{event.event_id}-lot",
                    symbol=event.symbol,
                    quantity=event.quantity_delta,
                    remaining_quantity=event.quantity_delta,
                    acquired_trade_date=event.trade_date,
                    available_date=event.available_date,
                    unit_cost=event.unit_cost,
                    source_event_id=event.event_id,
                )
            )
            continue

        lots_by_symbol[event.symbol] = _consume_fifo(lots, -event.quantity_delta, event)
        realized = realized + event.realized_pnl

    positions: dict[str, RebuiltPosition] = {}
    for symbol, lots in lots_by_symbol.items():
        total_qty = sum(lot.remaining_quantity for lot in lots)
        if total_qty == 0:
            continue
        if total_qty < 0 and not allow_negative:  # pragma: no cover - defensive
            raise InvariantViolation("negative position quantity", symbol=symbol)
        cost = sum((lot.remaining_cost_basis.amount for lot in lots), Decimal("0"))
        if as_of is None:
            available = total_qty
        else:
            available = sum(lot.remaining_quantity for lot in lots if lot.is_available_on(as_of))
        positions[symbol] = RebuiltPosition(
            symbol=symbol,
            quantity=total_qty,
            available_quantity=min(available, total_qty),
            cost_basis=Money(amount=cost),
            lots=tuple(lots),
        )
    return positions


def _consume_fifo(
    lots: list[RemainingLot], needed: int, event: PositionEvent
) -> list[RemainingLot]:
    if needed <= 0:  # pragma: no cover - guarded by model validation
        raise InvariantViolation("disposal quantity must be positive", needed=needed)
    ordered = sorted(
        lots,
        key=lambda lot: (lot.available_date, lot.acquired_trade_date, lot.lot_id),
    )
    remaining_needed = needed
    updated: list[RemainingLot] = []
    for lot in ordered:
        if remaining_needed == 0 or lot.remaining_quantity == 0:
            updated.append(lot)
            continue
        consumed = min(lot.remaining_quantity, remaining_needed)
        remaining_needed -= consumed
        updated.append(
            lot.model_copy(update={"remaining_quantity": lot.remaining_quantity - consumed})
        )
    if remaining_needed > 0:
        raise InvariantViolation(
            "position event disposes more shares than were held",
            symbol=event.symbol,
            unfulfilled=remaining_needed,
            event_id=event.event_id,
        )
    return updated


def available_quantity_on(lots: Sequence[RemainingLot], as_of: date) -> int:
    """Quantity sellable on ``as_of`` honouring per-lot availability dates."""
    return sum(lot.remaining_quantity for lot in lots if lot.is_available_on(as_of))


def sellable_lots_on(lots: Sequence[RemainingLot], as_of: date) -> tuple[RemainingLot, ...]:
    """Lots contributing to sellable quantity, in FIFO order."""
    usable = [lot for lot in lots if lot.is_available_on(as_of)]
    return tuple(sorted(usable, key=lambda lot: (lot.acquired_trade_date, lot.lot_id)))


def rebuild_account(
    *,
    opening_capital: Money,
    cash_entries: Sequence[CashLedgerEntry],
    position_events: Sequence[PositionEvent],
    as_of: date | None = None,
    allow_negative: bool = False,
) -> RebuiltAccount:
    """Rebuild the complete account state from immutable history."""
    cash = replay_cash(cash_entries, allow_negative=allow_negative)
    positions = replay_positions(position_events, as_of=as_of, allow_negative=allow_negative)

    opening_entries = [e for e in cash_entries if e.entry_type is LedgerEntryType.OPENING_CAPITAL]
    if not opening_entries:
        raise DataQualityError("ledger has no opening capital entry")
    replayed_opening = replay_cash(opening_entries)
    if replayed_opening.balance.amount != opening_capital.amount:
        raise InvariantViolation(
            "opening ledger entry does not match the configured initial capital",
            configured=str(opening_capital),
            replayed=str(replayed_opening.balance),
        )

    # Realised P&L must be derived from the *deduplicated* history, for exactly
    # the same reason positions are: a re-delivered event must not double-count.
    ordered_events = sorted(position_events, key=lambda e: e.sequence)
    deduped_events = _dedupe(ordered_events)
    realized = ZERO_CNY
    for event in deduped_events:
        if event.event_type is PositionEventType.SELL_FILL:
            realized = realized + event.realized_pnl

    return RebuiltAccount(
        opening_capital=opening_capital,
        cash=cash.balance,
        positions=positions,
        realized_pnl=realized,
        cash_entry_count=cash.entry_count,
        position_event_count=len(position_events),
    )
