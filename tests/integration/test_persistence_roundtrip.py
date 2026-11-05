"""Lossless persistence round-trip for every persisted JANUS aggregate.

Acceptance criterion 3/4 groundwork: a domain model written through its
repository must read back identical -- exact ``Decimal`` money and prices,
timezone-aware datetimes, enum values, nested ``FeeBreakdown`` and the ordered
position/check/fee child collections.

The fixtures live in :mod:`tests.integration.scenario` and are also used by the
ledger rebuild tests.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from janus.domain.common.enums import OrderSide
from janus.domain.common.errors import IdempotencyConflictError
from janus.domain.common.money import Money
from janus.infrastructure.database import Database, SqlAlchemyUnitOfWorkFactory
from tests.integration import scenario


def _seed_account(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    """Orders/fills carry an ``account_id`` FK, so the account must exist first."""
    with uow_factory() as uow:
        uow.accounts.save(scenario.make_account())
        uow.commit()


def test_every_aggregate_round_trips(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    instrument = scenario.make_instrument()
    etf = scenario.make_etf_instrument()
    account = scenario.make_account()
    opening = scenario.make_opening_entry(account)
    order = scenario.make_order()
    fill = scenario.make_fill(order)
    risk = scenario.make_risk_decision()
    decision = scenario.make_decision_record()
    data_snapshot = scenario.make_data_snapshot()
    portfolio_snapshot = scenario.make_portfolio_snapshot()
    bar = scenario.make_market_bar()

    with uow_factory() as uow:
        uow.instruments.upsert_many([instrument, etf])
        assert uow.instruments.get(scenario.SYMBOL) == instrument
        uow.accounts.save(account)
        assert uow.cash_ledger.append(opening) is True
        uow.orders.save(order)
        assert uow.fills.append(fill) is True
        uow.risk_decisions.append(risk)
        uow.decisions.append(decision)
        uow.data_snapshots.save(data_snapshot)
        uow.portfolio_snapshots.save(portfolio_snapshot)
        assert uow.market_bars.upsert_many([bar]) == 1
        uow.commit()

    with uow_factory() as uow:
        assert uow.instruments.get(scenario.SYMBOL) == instrument
        assert uow.instruments.get(scenario.ETF_SYMBOL) == etf
        assert uow.instruments.list_all() == [etf, instrument]

        assert uow.accounts.get(scenario.ACCOUNT_ID) == account
        assert uow.accounts.list_all() == [account]

        assert uow.cash_ledger.list_for_account(scenario.ACCOUNT_ID) == [opening]
        assert uow.cash_ledger.get_by_idempotency_key(opening.idempotency_key) == opening
        assert uow.cash_ledger.next_sequence(scenario.ACCOUNT_ID) == 2

        assert uow.orders.get(order.order_id) == order
        assert uow.orders.list_open(scenario.ACCOUNT_ID) == [order]
        assert uow.orders.list_by_account(scenario.ACCOUNT_ID) == [order]
        assert uow.orders.count_orders_on(scenario.ACCOUNT_ID, scenario.TRADE_DATE) == 1

        assert uow.fills.get(fill.fill_id) == fill
        assert uow.fills.get_by_idempotency_key(fill.idempotency_key) == fill
        assert uow.fills.list_by_order(order.order_id) == [fill]
        assert uow.fills.list_by_account(scenario.ACCOUNT_ID) == [fill]
        assert uow.fills.list_by_trade_date(scenario.ACCOUNT_ID, scenario.TRADE_DATE) == [fill]

        assert uow.risk_decisions.get(risk.risk_decision_id) == risk
        assert uow.risk_decisions.list_for_intent(risk.order_intent_id) == [risk]

        assert uow.decisions.get(decision.decision_id) == decision
        assert uow.decisions.list_recent() == [decision]

        assert uow.data_snapshots.get(data_snapshot.snapshot_id) == data_snapshot
        assert uow.data_snapshots.list_recent() == [data_snapshot]

        assert uow.portfolio_snapshots.get(portfolio_snapshot.snapshot_id) == (portfolio_snapshot)
        assert uow.portfolio_snapshots.latest_for_account(scenario.ACCOUNT_ID) == (
            portfolio_snapshot
        )

        assert uow.market_bars.get(bar.symbol, bar.trade_date) == bar
        assert uow.market_bars.latest(bar.symbol) == bar
        assert uow.market_bars.list_range(bar.symbol, scenario.BAR_DATE, scenario.BAR_DATE) == [bar]


def test_money_and_prices_are_exact_decimals(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    amount = Decimal("123456.78")
    account = scenario.make_account(initial_capital=Money(amount=amount))
    order = scenario.make_order(quantity=300, limit_price=Decimal("100.07"))
    fill = scenario.make_fill(order, price=Decimal("100.07"), quantity=300)

    with uow_factory() as uow:
        uow.accounts.save(account)
        uow.orders.save(order)
        assert uow.fills.append(fill) is True
        uow.commit()

    with uow_factory() as uow:
        loaded_account = uow.accounts.get(scenario.ACCOUNT_ID)
        loaded_order = uow.orders.get(order.order_id)
        loaded_fill = uow.fills.get(fill.fill_id)
        assert loaded_account is not None
        assert loaded_order is not None
        assert loaded_fill is not None

        assert isinstance(loaded_account.initial_capital.amount, Decimal)
        assert loaded_account.initial_capital.amount.as_tuple() == amount.as_tuple()
        assert loaded_order.limit_price == Decimal("100.07")
        assert isinstance(loaded_order.limit_price, Decimal)
        assert loaded_fill.price == Decimal("100.07")
        assert loaded_fill.notional.amount == Decimal("30021.00")
        assert loaded_fill.fees.total == Decimal("5.10")


def test_datetimes_round_trip_timezone_aware(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    account = scenario.make_account()
    with uow_factory() as uow:
        uow.accounts.save(account)
        uow.commit()

    with uow_factory() as uow:
        loaded = uow.accounts.get(scenario.ACCOUNT_ID)
        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        assert loaded.created_at == account.created_at
        # Stored as UTC ISO-8601 by contract.
        assert loaded.created_at.utcoffset() == timedelta(0)


def test_enums_persist_as_text_values(
    database: Database, uow_factory: SqlAlchemyUnitOfWorkFactory
) -> None:
    _seed_account(uow_factory)
    order = scenario.make_order()
    fill = scenario.make_fill(order)
    with uow_factory() as uow:
        uow.orders.save(order)
        assert uow.fills.append(fill) is True
        uow.commit()
    with database.engine.connect() as connection:
        status_text = connection.exec_driver_sql("SELECT status FROM orders").scalar_one()
        component_text = connection.exec_driver_sql(
            "SELECT component FROM fee_lines ORDER BY line_index"
        ).scalars()
        assert status_text == "SUBMITTED_PAPER"
        assert list(component_text) == ["BROKER_COMMISSION", "TRANSFER_FEE"]
    with uow_factory() as uow:
        loaded = uow.orders.get(order.order_id)
        loaded_fill = uow.fills.get(fill.fill_id)
        assert loaded is not None
        assert loaded_fill is not None
        assert loaded.status.value == "SUBMITTED_PAPER"
        assert loaded_fill.fees.lines[0].component.value == "BROKER_COMMISSION"


def test_account_versions_are_immutable(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    account = scenario.make_account()
    paused = account.paused("kill switch exercised by test", scenario.T0, new_version=2)

    with uow_factory() as uow:
        uow.accounts.save(account)
        uow.accounts.save(paused)
        uow.commit()

    with uow_factory() as uow:
        latest = uow.accounts.get(scenario.ACCOUNT_ID)
        assert latest == paused
        assert uow.accounts.list_all() == [paused]
        # Re-saving an identical version is a harmless no-op.
        uow.accounts.save(paused)
        with pytest.raises(IdempotencyConflictError):
            uow.accounts.save(account.paused("different reason", scenario.T0, new_version=2))


def test_fee_breakdown_round_trips_line_by_line(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    _seed_account(uow_factory)
    order = scenario.make_order(side=OrderSide.SELL, quantity=250)
    fill = scenario.make_fill(order, quantity=250, price=Decimal("101.13"))
    with uow_factory() as uow:
        uow.orders.save(order)
        assert uow.fills.append(fill) is True
        uow.commit()
    with uow_factory() as uow:
        loaded = uow.fills.get(fill.fill_id)
        assert loaded is not None
        assert loaded.fees == fill.fees
        assert [line.component for line in loaded.fees.lines] == [
            line.component for line in fill.fees.lines
        ]
        assert [line.amount for line in loaded.fees.lines] == [
            line.amount for line in fill.fees.lines
        ]
        assert loaded.fees.notional == Decimal("25282.50")
