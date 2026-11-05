"""Acceptance criterion 3 and 4 proof: rebuild the account from persisted history.

Coherent deterministic timeline (all fixture data, offline):

* 2026-11-05: paper account opens with CNY 100,000.00 (opening ledger event);
  buy 100 @ 100.00 (fill 1, fees 5.10), available 2026-11-06 (T+1);
* 2026-11-06: buy 100 @ 101.00 as a **partial fill** of a 200-share order
  (fill 2, fees 5.10, available 2026-11-09); sell 80 @ 105.00 from the settled
  lot (fill 3, fees: 5.00 commission + 4.20 stamp duty).

Expected ledger after settlement: cash 88,280.60, holding 120 shares,
remaining cost basis 12,106.12, realised P&L 386.72.

The tests then prove that replaying the *persisted* ledger reproduces a stored
portfolio snapshot exactly, that duplicate delivery returns ``False`` and never
double-counts, and that a conflicting reuse of an idempotency key is rejected.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from janus.domain.common.enums import FeeComponent, OrderSide, OrderStatus
from janus.domain.common.errors import (
    IdempotencyConflictError,
    InvariantViolation,
)
from janus.domain.common.money import Money
from janus.domain.common.time import MARKET_TZ
from janus.domain.ledger.models import LedgerEntryType
from janus.domain.ledger.replay import rebuild_account
from janus.domain.market_rules.models import (
    FeeBasis,
    FeeBreakdown,
    FeeLine,
)
from janus.infrastructure.database import SqlAlchemyUnitOfWorkFactory
from tests.integration import scenario

ACCOUNT_ID = scenario.ACCOUNT_ID
SYMBOL = scenario.SYMBOL

BUY1_UNIT_COST = Decimal("100.051")
BUY2_UNIT_COST = Decimal("101.051")
FINAL_CASH = Money(amount=Decimal("88280.60"))
REMAINING_COST_BASIS = Money(amount=Decimal("12106.12"))
REALIZED_PNL = Money(amount=Decimal("386.72"))
CASH_ENTRY_COUNT = 7
POSITION_EVENT_COUNT = 3

BUY1_DATE = date(2026, 11, 5)
BUY1_SETTLEMENT_DATE = date(2026, 11, 6)
BUY2_DATE = date(2026, 11, 6)
BUY2_SETTLEMENT_DATE = date(2026, 11, 9)
SETTLED_AT = datetime(2026, 11, 9, 15, 0, tzinfo=MARKET_TZ)
FILL2_EXECUTED_AT = datetime(2026, 11, 6, 9, 30, tzinfo=MARKET_TZ)
FILL3_EXECUTED_AT = datetime(2026, 11, 6, 14, 55, tzinfo=MARKET_TZ)


def _sell_fee_breakdown() -> FeeBreakdown:
    return FeeBreakdown(
        fee_schedule_id=scenario.FEE_SCHEDULE_ID,
        fee_schedule_version=scenario.FEE_SCHEDULE_VERSION,
        side=OrderSide.SELL,
        notional=Decimal("8400.00"),
        lines=(
            FeeLine(
                component=FeeComponent.BROKER_COMMISSION,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.0005"),
                amount=Decimal("5.00"),
            ),
            FeeLine(
                component=FeeComponent.STAMP_DUTY,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.0005"),
                amount=Decimal("4.20"),
            ),
        ),
    )


def _seed_history(uow_factory: SqlAlchemyUnitOfWorkFactory) -> dict[str, Any]:
    """Persist the complete deterministic history; returns the domain objects."""
    account = scenario.make_account()
    opening = scenario.make_opening_entry(account, trade_date=BUY1_DATE, occurred_at=scenario.T0)

    order1 = scenario.make_order(
        side=OrderSide.BUY,
        quantity=100,
        status=OrderStatus.FILLED,
        filled_quantity=100,
        target_trade_date=BUY1_DATE,
        terminal_at=scenario.T0,
    )
    order2 = scenario.make_order(
        side=OrderSide.BUY,
        quantity=200,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_quantity=100,
        target_trade_date=BUY2_DATE,
    )
    order3 = scenario.make_order(
        side=OrderSide.SELL,
        quantity=80,
        status=OrderStatus.FILLED,
        filled_quantity=80,
        target_trade_date=BUY2_DATE,
        terminal_at=FILL3_EXECUTED_AT,
    )

    fill1 = scenario.make_fill(
        order1,
        quantity=100,
        price=Decimal("100.00"),
        trade_date=BUY1_DATE,
        executed_at=scenario.T0,
        idempotency_key="fill-1:delivery",
    )
    fill2 = scenario.make_fill(
        order2,
        quantity=100,
        price=Decimal("101.00"),
        is_partial=True,
        trade_date=BUY2_DATE,
        executed_at=FILL2_EXECUTED_AT,
        idempotency_key="fill-2:delivery",
    )
    fill3 = scenario.make_fill(
        order3,
        quantity=80,
        price=Decimal("105.00"),
        fees=_sell_fee_breakdown(),
        trade_date=BUY2_DATE,
        executed_at=FILL3_EXECUTED_AT,
        idempotency_key="fill-3:delivery",
    )

    entries = [
        opening,
        scenario.make_cash_entry(
            ACCOUNT_ID,
            sequence=2,
            amount=Money(amount=Decimal("-10000.00")),
            entry_type=LedgerEntryType.FILL_SETTLEMENT,
            idempotency_key="fill-1:settlement",
            source_ref=fill1.fill_id,
            trade_date=BUY1_DATE,
            balance_after=Money(amount=Decimal("90000.00")),
        ),
        scenario.make_cash_entry(
            ACCOUNT_ID,
            sequence=3,
            amount=Money(amount=Decimal("-5.10")),
            entry_type=LedgerEntryType.FILL_FEE,
            idempotency_key="fill-1:fee",
            source_ref=fill1.fill_id,
            trade_date=BUY1_DATE,
            balance_after=Money(amount=Decimal("89994.90")),
        ),
        scenario.make_cash_entry(
            ACCOUNT_ID,
            sequence=4,
            amount=Money(amount=Decimal("-10100.00")),
            entry_type=LedgerEntryType.FILL_SETTLEMENT,
            idempotency_key="fill-2:settlement",
            source_ref=fill2.fill_id,
            trade_date=BUY2_DATE,
            occurred_at=FILL2_EXECUTED_AT,
            balance_after=Money(amount=Decimal("79894.90")),
        ),
        scenario.make_cash_entry(
            ACCOUNT_ID,
            sequence=5,
            amount=Money(amount=Decimal("-5.10")),
            entry_type=LedgerEntryType.FILL_FEE,
            idempotency_key="fill-2:fee",
            source_ref=fill2.fill_id,
            trade_date=BUY2_DATE,
            occurred_at=FILL2_EXECUTED_AT,
            balance_after=Money(amount=Decimal("79889.80")),
        ),
        scenario.make_cash_entry(
            ACCOUNT_ID,
            sequence=6,
            amount=Money(amount=Decimal("8400.00")),
            entry_type=LedgerEntryType.FILL_SETTLEMENT,
            idempotency_key="fill-3:settlement",
            source_ref=fill3.fill_id,
            trade_date=BUY2_DATE,
            occurred_at=FILL3_EXECUTED_AT,
            balance_after=Money(amount=Decimal("88289.80")),
        ),
        scenario.make_cash_entry(
            ACCOUNT_ID,
            sequence=7,
            amount=Money(amount=Decimal("-9.20")),
            entry_type=LedgerEntryType.FILL_FEE,
            idempotency_key="fill-3:fee",
            source_ref=fill3.fill_id,
            trade_date=BUY2_DATE,
            occurred_at=FILL3_EXECUTED_AT,
            balance_after=FINAL_CASH,
        ),
    ]

    events = [
        scenario.make_position_acquisition(
            ACCOUNT_ID,
            sequence=1,
            symbol=SYMBOL,
            quantity=100,
            unit_cost=BUY1_UNIT_COST,
            trade_date=BUY1_DATE,
            available_date=BUY1_SETTLEMENT_DATE,
            idempotency_key="fill-1:position",
            source_ref=fill1.fill_id,
            occurred_at=scenario.T0,
        ),
        scenario.make_position_acquisition(
            ACCOUNT_ID,
            sequence=2,
            symbol=SYMBOL,
            quantity=100,
            unit_cost=BUY2_UNIT_COST,
            trade_date=BUY2_DATE,
            available_date=BUY2_SETTLEMENT_DATE,
            idempotency_key="fill-2:position",
            source_ref=fill2.fill_id,
            occurred_at=FILL2_EXECUTED_AT,
        ),
        scenario.make_position_disposal(
            ACCOUNT_ID,
            sequence=3,
            symbol=SYMBOL,
            quantity=80,
            unit_cost=BUY1_UNIT_COST,
            realized_pnl=REALIZED_PNL,
            trade_date=BUY2_DATE,
            idempotency_key="fill-3:position",
            source_ref=fill3.fill_id,
            occurred_at=FILL3_EXECUTED_AT,
        ),
    ]

    stored_position = scenario.make_position(
        quantity=120,
        available_quantity=120,
        cost_basis="12106.12",
        market_price="105.00",
    )
    snapshot = scenario.make_portfolio_snapshot(
        cash="88280.60",
        position=stored_position,
        realized_pnl="386.72",
        as_of=SETTLED_AT,
        valuation_date=BUY2_SETTLEMENT_DATE,
    )

    with uow_factory() as uow:
        uow.instruments.upsert_many([scenario.make_instrument()])
        uow.accounts.save(account)
        for order in (order1, order2, order3):
            uow.orders.save(order)
        for fill in (fill1, fill2, fill3):
            assert uow.fills.append(fill) is True
        for entry in entries:
            assert uow.cash_ledger.append(entry) is True
        for event in events:
            assert uow.position_ledger.append(event) is True
        uow.portfolio_snapshots.save(snapshot)
        uow.commit()

    return {
        "account": account,
        "opening": opening,
        "orders": (order1, order2, order3),
        "fills": (fill1, fill2, fill3),
        "entries": entries,
        "events": events,
        "snapshot": snapshot,
    }


def _rebuild(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    history: dict[str, Any],
    *,
    as_of: date | None = BUY2_SETTLEMENT_DATE,
) -> tuple[Any, Any]:
    with uow_factory() as uow:
        entries = uow.cash_ledger.list_for_account(ACCOUNT_ID)
        events = uow.position_ledger.list_for_account(ACCOUNT_ID)
        stored = uow.portfolio_snapshots.latest_for_account(ACCOUNT_ID)
    assert stored is not None
    rebuilt = rebuild_account(
        opening_capital=history["account"].initial_capital,
        cash_entries=entries,
        position_events=events,
        as_of=as_of,
    )
    return rebuilt, stored


def test_rebuild_reproduces_stored_cash_and_positions(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    history = _seed_history(uow_factory)
    rebuilt, stored = _rebuild(uow_factory, history)

    assert stored.cash == FINAL_CASH
    assert rebuilt.cash == stored.cash
    assert rebuilt.opening_capital == scenario.INITIAL_CAPITAL

    rebuilt_position = rebuilt.positions[SYMBOL]
    stored_position = stored.position(SYMBOL)
    assert stored_position is not None
    assert rebuilt_position.quantity == stored_position.quantity == 120
    assert rebuilt_position.available_quantity == stored_position.available_quantity == 120
    assert rebuilt_position.cost_basis == stored_position.cost_basis == REMAINING_COST_BASIS
    assert rebuilt.realized_pnl == stored.realized_pnl == REALIZED_PNL

    assert rebuilt.cash_entry_count == CASH_ENTRY_COUNT
    assert rebuilt.position_event_count == POSITION_EVENT_COUNT


def test_available_quantity_respects_t_plus_one(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    history = _seed_history(uow_factory)

    buy_day, _ = _rebuild(uow_factory, history, as_of=BUY1_DATE)
    assert buy_day.positions[SYMBOL].quantity == 120
    assert buy_day.positions[SYMBOL].available_quantity == 0

    after_first_settlement, _ = _rebuild(uow_factory, history, as_of=BUY1_SETTLEMENT_DATE)
    assert after_first_settlement.positions[SYMBOL].available_quantity == 20

    after_second_settlement, _ = _rebuild(uow_factory, history, as_of=BUY2_SETTLEMENT_DATE)
    assert after_second_settlement.positions[SYMBOL].available_quantity == 120


def test_duplicate_delivery_returns_false_and_does_not_double_count(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    history = _seed_history(uow_factory)

    with uow_factory() as uow:
        assert uow.fills.append(history["fills"][0]) is False
        assert uow.cash_ledger.append(history["opening"]) is False
        assert uow.position_ledger.append(history["events"][0]) is False

        assert len(uow.fills.list_by_account(ACCOUNT_ID)) == 3
        assert len(uow.cash_ledger.list_for_account(ACCOUNT_ID)) == CASH_ENTRY_COUNT
        assert len(uow.position_ledger.list_for_account(ACCOUNT_ID)) == POSITION_EVENT_COUNT
        assert uow.cash_ledger.next_sequence(ACCOUNT_ID) == CASH_ENTRY_COUNT + 1
        assert uow.position_ledger.next_sequence(ACCOUNT_ID) == POSITION_EVENT_COUNT + 1
        uow.commit()

    rebuilt, stored = _rebuild(uow_factory, history)
    assert rebuilt.cash == stored.cash == FINAL_CASH
    assert rebuilt.positions[SYMBOL].quantity == 120


def test_replay_deduplicates_identical_history_rows(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """Duplicate delivery is *harmless* (spec s5.2, acceptance criterion 4).

    Defense in depth: the repositories dedupe on write (``append`` returns
    ``False`` for a known idempotency key) **and** ``replay_*`` collapses an
    identical re-delivered row instead of double-counting it. A row whose
    idempotency key matches but whose content differs is still a hard error --
    see ``test_conflicting_idempotency_key_is_rejected``.
    """
    history = _seed_history(uow_factory)
    entries = history["entries"]
    events = history["events"]

    clean = rebuild_account(
        opening_capital=history["account"].initial_capital,
        cash_entries=entries,
        position_events=events,
        as_of=BUY2_SETTLEMENT_DATE,
    )
    with_duplicates = rebuild_account(
        opening_capital=history["account"].initial_capital,
        cash_entries=[*entries, entries[3]],
        position_events=[*events, events[2]],
        as_of=BUY2_SETTLEMENT_DATE,
    )

    assert with_duplicates.cash.amount == clean.cash.amount
    assert with_duplicates.realized_pnl.amount == clean.realized_pnl.amount
    assert set(with_duplicates.positions) == set(clean.positions)
    for symbol, position in clean.positions.items():
        assert with_duplicates.positions[symbol].quantity == position.quantity
        assert with_duplicates.positions[symbol].cost_basis.amount == position.cost_basis.amount
    assert with_duplicates.cash_entry_count == clean.cash_entry_count


def test_conflicting_idempotency_key_is_rejected(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    history = _seed_history(uow_factory)
    conflicting = history["opening"].model_copy(update={"amount": Money(amount=Decimal("1.00"))})
    with uow_factory() as uow:
        with pytest.raises(IdempotencyConflictError):
            uow.cash_ledger.append(conflicting)


def test_ledger_sequence_conflict_is_rejected(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    _seed_history(uow_factory)
    clashing = scenario.make_cash_entry(
        ACCOUNT_ID,
        sequence=2,
        amount=Money(amount=Decimal("-1.00")),
        entry_type=LedgerEntryType.CORRECTION,
        idempotency_key="clash-key",
        source_ref="clash",
    )
    with uow_factory() as uow:
        with pytest.raises(InvariantViolation):
            uow.cash_ledger.append(clashing)
