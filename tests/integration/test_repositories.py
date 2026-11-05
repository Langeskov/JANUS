"""Behavioural tests for the concrete repositories beyond pure round-trip.

Focused on the contract details the application services depend on:
transaction discipline, idempotent appends, order queries, market-bar upserts
and immutable-record conflicts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from janus.domain.common.enums import OrderSide, OrderStatus
from janus.domain.common.errors import (
    IdempotencyConflictError,
    ValidationError,
)
from janus.domain.common.money import Money
from janus.domain.instruments.models import Instrument
from janus.domain.ledger.models import LedgerEntryType
from janus.infrastructure.database import Database, SqlAlchemyUnitOfWorkFactory
from janus.infrastructure.database.seeding import seed_cash_instrument, seed_instruments
from tests.integration import scenario


def _seed_account(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    """Orders/fills carry an ``account_id`` FK, so the account must exist first."""
    with uow_factory() as uow:
        uow.accounts.save(scenario.make_account())
        uow.commit()


def test_uncommitted_unit_of_work_is_rolled_back(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    with uow_factory() as uow:
        uow.accounts.save(scenario.make_account())

    with uow_factory() as uow:
        assert uow.accounts.get(scenario.ACCOUNT_ID) is None


def test_explicit_rollback_discards_writes(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    with uow_factory() as uow:
        uow.accounts.save(scenario.make_account())
        uow.commit()
        uow.accounts.save(scenario.make_account().paused("test pause", scenario.T0, new_version=2))
        uow.rollback()

    with uow_factory() as uow:
        assert uow.accounts.get(scenario.ACCOUNT_ID) == scenario.make_account()


def test_order_queries_and_counts(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    _seed_account(uow_factory)
    submitted = scenario.make_order(order_id="order-submitted", status=OrderStatus.SUBMITTED_PAPER)
    filled = scenario.make_order(
        order_id="order-filled",
        status=OrderStatus.FILLED,
        filled_quantity=100,
        terminal_at=scenario.T0,
    )
    approved_next_day = scenario.make_order(
        order_id="order-approved",
        status=OrderStatus.RISK_APPROVED,
        target_trade_date=scenario.NEXT_TRADE_DATE,
    )
    proposed = scenario.make_order(order_id="order-proposed", status=OrderStatus.PROPOSED)

    with uow_factory() as uow:
        for order in (submitted, filled, approved_next_day, proposed):
            uow.orders.save(order)
        uow.commit()

    with uow_factory() as uow:
        assert {order.order_id for order in uow.orders.list_by_account(scenario.ACCOUNT_ID)} == {
            "order-submitted",
            "order-filled",
            "order-approved",
            "order-proposed",
        }
        assert {order.order_id for order in uow.orders.list_open(scenario.ACCOUNT_ID)} == {
            "order-submitted",
            "order-approved",
        }
        assert uow.orders.count_orders_on(scenario.ACCOUNT_ID, scenario.TRADE_DATE) == 3
        assert {
            order.order_id
            for order in uow.orders.list_by_trade_date(
                scenario.ACCOUNT_ID, scenario.NEXT_TRADE_DATE
            )
        } == {"order-approved"}
        assert {
            order.order_id
            for order in uow.orders.list_by_status(scenario.ACCOUNT_ID, ("FILLED", "RISK_APPROVED"))
        } == {"order-filled", "order-approved"}
        with pytest.raises(ValidationError):
            uow.orders.list_by_status(scenario.ACCOUNT_ID, ("NOT_A_STATUS",))


def test_order_state_transition_is_persisted(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    _seed_account(uow_factory)
    proposed = scenario.make_order(status=OrderStatus.PROPOSED)
    with uow_factory() as uow:
        uow.orders.save(proposed)
        uow.commit()

    approved = proposed.model_copy(
        update={"status": OrderStatus.RISK_APPROVED, "updated_at": scenario.T0}
    )
    with uow_factory() as uow:
        uow.orders.save(approved)
        uow.commit()
    with uow_factory() as uow:
        assert uow.orders.get(proposed.order_id) == approved


def test_instrument_upsert_updates_existing_row(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    instrument = scenario.make_instrument()
    renamed = instrument.model_copy(update={"name": "Renamed Instrument"})
    with uow_factory() as uow:
        uow.instruments.upsert(instrument)
        uow.commit()
    with uow_factory() as uow:
        uow.instruments.upsert(renamed)
        uow.commit()
    with uow_factory() as uow:
        assert uow.instruments.get(instrument.symbol) == renamed


def test_fill_idempotency_conflict_is_rejected(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    _seed_account(uow_factory)
    order = scenario.make_order()
    fill = scenario.make_fill(order, fill_id="fill-fixed", idempotency_key="fill-key")
    conflicting = fill.model_copy(update={"price": Decimal("99.00")})

    with uow_factory() as uow:
        uow.orders.save(order)
        assert uow.fills.append(fill) is True
        uow.commit()

    with uow_factory() as uow:
        assert uow.fills.append(fill) is False
        with pytest.raises(IdempotencyConflictError):
            uow.fills.append(conflicting)


def test_market_bar_upsert_is_idempotent(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    bar = scenario.make_market_bar()
    updated = bar.model_copy(update={"close": Decimal("101.50"), "high": Decimal("102.50")})

    with uow_factory() as uow:
        assert uow.market_bars.upsert_many([bar, bar]) == 1
        uow.commit()
    with uow_factory() as uow:
        assert uow.market_bars.upsert_many([updated]) == 1
        uow.commit()
    with uow_factory() as uow:
        assert uow.market_bars.get(scenario.SYMBOL, scenario.BAR_DATE) == updated
        assert uow.market_bars.list_range(
            scenario.SYMBOL, scenario.BAR_DATE, scenario.BAR_DATE
        ) == [updated]
        assert uow.market_bars.upsert_many([]) == 0


def test_immutable_records_reject_conflicting_replay(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    record = scenario.make_decision_record()
    snapshot = scenario.make_data_snapshot()
    conflicting_record = record.model_copy(update={"thesis": "Different thesis"})

    with uow_factory() as uow:
        uow.decisions.append(record)
        uow.data_snapshots.save(snapshot)
        uow.commit()

    with uow_factory() as uow:
        uow.decisions.append(record)
        uow.data_snapshots.save(snapshot)
        with pytest.raises(IdempotencyConflictError):
            uow.decisions.append(conflicting_record)


def test_ledger_next_sequence_after_appends(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    account = scenario.make_account()
    opening = scenario.make_opening_entry(account)
    follow_up = scenario.make_cash_entry(
        scenario.ACCOUNT_ID,
        sequence=2,
        amount=Money(amount=Decimal("-10.00")),
        entry_type=LedgerEntryType.FILL_FEE,
        idempotency_key="fee-1",
        source_ref="fill-1",
    )
    acquisition = scenario.make_position_acquisition(
        scenario.ACCOUNT_ID,
        sequence=1,
        symbol=scenario.SYMBOL,
        quantity=100,
        unit_cost=Decimal("100.00"),
        idempotency_key="fill-1:position",
        source_ref="fill-1",
    )

    with uow_factory() as uow:
        uow.accounts.save(account)
        assert uow.cash_ledger.next_sequence(scenario.ACCOUNT_ID) == 1
        assert uow.cash_ledger.append(opening) is True
        assert uow.cash_ledger.append(follow_up) is True
        assert uow.cash_ledger.next_sequence(scenario.ACCOUNT_ID) == 3
        assert uow.position_ledger.next_sequence(scenario.ACCOUNT_ID) == 1
        assert uow.position_ledger.append(acquisition) is True
        assert uow.position_ledger.next_sequence(scenario.ACCOUNT_ID) == 2
        assert [
            entry.sequence for entry in uow.cash_ledger.list_for_account(scenario.ACCOUNT_ID)
        ] == [1, 2]
        uow.commit()


def test_risk_and_decision_lineage_survive_unknown_account(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """Fail-closed rejections must be auditable even when the account is absent.

    ``RiskService`` persists an ``ACCOUNT_ACTIVE`` rejection (and its lineage)
    when the requested account does not exist, so those two tables deliberately
    carry no foreign key to the account master.
    """
    unknown_account = "acct-does-not-exist"
    decision = scenario.make_risk_decision(account_id=unknown_account)
    record = scenario.make_decision_record(account_id=unknown_account)

    with uow_factory() as uow:
        uow.risk_decisions.append(decision)
        uow.decisions.append(record)
        uow.commit()

    with uow_factory() as uow:
        assert uow.risk_decisions.get(decision.risk_decision_id) == decision
        loaded = uow.decisions.get(record.decision_id)
        assert loaded is not None
        assert loaded.account_id == unknown_account


def test_fills_can_be_queried_by_trade_date(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    _seed_account(uow_factory)
    order = scenario.make_order(side=OrderSide.BUY)
    fill = scenario.make_fill(order, quantity=50)
    next_day_order = scenario.make_order(
        side=OrderSide.BUY,
        quantity=50,
        order_id="order-next-day",
        target_trade_date=scenario.NEXT_TRADE_DATE,
    )
    with uow_factory() as uow:
        uow.orders.save(order)
        uow.orders.save(next_day_order)
        assert uow.fills.append(fill) is True
        assert (
            uow.fills.append(
                scenario.make_fill(
                    next_day_order,
                    quantity=50,
                    idempotency_key="fill-next-day:delivery",
                ).model_copy(update={"trade_date": scenario.NEXT_TRADE_DATE})
            )
            is True
        )
        uow.commit()

    with uow_factory() as uow:
        assert [
            executed.fill_id
            for executed in uow.fills.list_by_trade_date(scenario.ACCOUNT_ID, scenario.TRADE_DATE)
        ] == [fill.fill_id]
        assert len(uow.fills.list_by_trade_date(scenario.ACCOUNT_ID, date(2026, 11, 7))) == 0


def test_seeding_helpers_are_idempotent(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    instrument = scenario.make_instrument()
    assert seed_instruments(uow_factory, [instrument]) == 1
    assert seed_instruments(uow_factory, [instrument]) == 1
    assert seed_cash_instrument(uow_factory) == 1

    with uow_factory() as uow:
        assert uow.instruments.get(scenario.SYMBOL) == instrument
        assert uow.instruments.get("CASH.CNY") == Instrument.cash()


def test_in_memory_database_is_shared_across_sessions() -> None:
    database = Database("sqlite://")
    try:
        database.create_all()
        factory = SqlAlchemyUnitOfWorkFactory(database)
        account = scenario.make_account()
        with factory() as uow:
            uow.accounts.save(account)
            uow.commit()
        with factory() as uow:
            assert uow.accounts.get(scenario.ACCOUNT_ID) == account
    finally:
        database.dispose()
