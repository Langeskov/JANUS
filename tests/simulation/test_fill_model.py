"""Daily-frequency fill-model simulation behaviour (spec s6.2).

These tests pin down the conservative execution semantics:

* next-open reference price plus adverse slippage;
* no fill on suspended / zero-volume / non-executable data;
* no fill on a locked limit board;
* limit-price and daily price-limit enforcement;
* participation cap producing a genuine partial fill;
* same-day signal bars can never fill;
* order-lifecycle guards around fills, expiry and cancellation.

Everything is deterministic and offline.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from janus.domain.common.enums import (
    AdjustmentMode,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidity,
    SettlementRule,
)
from janus.domain.common.errors import (
    InvalidTransitionError,
    SecurityViolationError,
)
from janus.domain.common.time import MARKET_TZ
from janus.domain.instruments.models import Instrument
from janus.domain.market_data.models import MarketBar
from janus.domain.market_rules.models import FeeBreakdown, ResolvedTradingRules
from janus.domain.orders.models import PaperOrder
from janus.domain.orders.state_machine import transition
from janus.infrastructure.market_data.fixture_provider import (
    FixtureProvider,
    load_instruments,
)
from janus.infrastructure.paper_execution.fill_model import (
    FillSkipReason,
    NextOpenWithSlippageV1,
    NextOpenWithSlippageV1Config,
)
from janus.infrastructure.paper_execution.gateway import PaperExecutionGateway
from janus.infrastructure.rulesets.registry import RuleSetRegistry

pytestmark = pytest.mark.simulation

ETF = "510300.SH"
SIGNAL_DAY = date(2026, 11, 5)
FILL_DAY = date(2026, 11, 6)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine() -> object:
    registry = RuleSetRegistry.from_default_config(config_dir=Path("config/market_rules"))
    return registry.engine()


@pytest.fixture(scope="module")
def instruments() -> dict[str, Instrument]:
    return {
        instrument.symbol: instrument
        for instrument in load_instruments(Path("data/fixtures/instruments.json"))
    }


@pytest.fixture(scope="module")
def provider(instruments: dict[str, Instrument]) -> FixtureProvider:
    return FixtureProvider(list(instruments.values()))


def _rules(
    engine: object, instrument: Instrument, trade_date: date = FILL_DAY
) -> ResolvedTradingRules:
    return engine.resolve(instrument, trade_date)  # type: ignore[attr-defined]


def _fee_calculator(
    engine: object, rules: ResolvedTradingRules
) -> Callable[[OrderSide, int, Decimal], FeeBreakdown]:
    def compute(side: OrderSide, quantity: int, price: Decimal) -> FeeBreakdown:
        return engine.compute_fees(  # type: ignore[attr-defined]
            rules=rules, side=side, quantity=quantity, price=price
        )

    return compute


def _order(
    *,
    rules: ResolvedTradingRules,
    side: OrderSide = OrderSide.BUY,
    quantity: int = 500,
    limit_price: Decimal | None = None,
    status: OrderStatus = OrderStatus.SUBMITTED_PAPER,
    earliest: date = FILL_DAY,
) -> PaperOrder:
    instant = datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ)
    return PaperOrder(
        order_id="order-sim-1",
        intent_id="intent-sim-1",
        account_id="janus-paper-cny-001",
        symbol=rules.symbol,
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=limit_price if limit_price is not None else rules.tick_size * Decimal("100000"),
        validity=OrderValidity.DAY,
        status=status,
        created_at=instant,
        updated_at=instant,
        target_trade_date=earliest,
        earliest_execution_date=earliest,
        rule_set_id=rules.rule_set_id,
        rule_set_version=rules.rule_set_version,
        fee_schedule_id=rules.fee_schedule_id,
        fee_schedule_version=rules.fee_schedule_version,
        risk_decision_id="riskdec-sim-1",
        decision_id="decision-sim-1",
        data_snapshot_id="dsnap-sim-1",
        strategy_id="broad_etf_core",
        strategy_version="0.1.0",
    )


def _locked_bar(
    provider: FixtureProvider, symbol: str, *, at_limit_up: bool
) -> tuple[MarketBar, Decimal]:
    """A bar locked at the daily price limit (low == high == limit price)."""
    prev = provider.get_daily_bar(symbol, SIGNAL_DAY)
    bar = provider.get_daily_bar(symbol, FILL_DAY)
    assert prev is not None
    assert bar is not None
    instrument = provider.instrument(symbol)
    assert instrument is not None
    registry = RuleSetRegistry.from_default_config(config_dir=Path("config/market_rules"))
    rules = registry.engine().resolve(instrument, FILL_DAY)
    limits = rules.compute_price_limits(prev.close, is_risk_warning=False)
    locked_price = limits.limit_up if at_limit_up else limits.limit_down
    assert locked_price is not None
    locked = bar.model_copy(
        update={
            "open": locked_price,
            "high": locked_price,
            "low": locked_price,
            "close": locked_price,
        }
    )
    return locked, prev.close


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_next_open_with_adverse_slippage(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    bar = provider.get_daily_bar(ETF, FILL_DAY)
    prev = provider.get_daily_bar(ETF, SIGNAL_DAY)
    assert bar is not None
    assert prev is not None

    model = NextOpenWithSlippageV1()
    proposal = model.propose(
        order=_order(rules=rules),
        bar=bar,
        prev_close=prev.close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
        signal_bar_date=SIGNAL_DAY,
    )
    assert proposal.filled is True
    fill = proposal.require_fill()
    assert fill.fill_model_version == "next_open_with_slippage_v1"
    assert fill.trade_date == FILL_DAY
    # Adverse slippage means a buy pays strictly more than the reference open.
    assert fill.price > bar.open
    assert fill.slippage_bps == Decimal("5")
    assert fill.quantity % rules.buy_quantity_increment == 0
    assert fill.fees.total > 0


# ---------------------------------------------------------------------------
# No-fill conditions
# ---------------------------------------------------------------------------
def test_zero_volume_bar_does_not_fill(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    bar = provider.get_daily_bar(ETF, FILL_DAY)
    assert bar is not None
    proposal = NextOpenWithSlippageV1().propose(
        order=_order(rules=rules),
        bar=bar.model_copy(update={"volume": 0}),
        prev_close=bar.close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    assert proposal.filled is False
    assert proposal.skip_reason is FillSkipReason.ZERO_VOLUME


def test_suspended_instrument_without_bar_does_not_fill(
    engine: object, instruments: dict[str, Instrument]
) -> None:
    rules = _rules(engine, instruments[ETF])
    proposal = NextOpenWithSlippageV1().propose(
        order=_order(rules=rules),
        bar=None,
        prev_close=Decimal("10"),
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    assert proposal.filled is False
    assert proposal.skip_reason is FillSkipReason.NO_BAR


def test_locked_limit_up_blocks_a_buy(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    locked, prev_close = _locked_bar(provider, ETF, at_limit_up=True)
    proposal = NextOpenWithSlippageV1().propose(
        order=_order(rules=rules),
        bar=locked,
        prev_close=prev_close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    assert proposal.filled is False
    assert proposal.skip_reason is FillSkipReason.LOCKED_AT_LIMIT_UP


def test_locked_limit_down_blocks_a_sell(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    locked, prev_close = _locked_bar(provider, ETF, at_limit_up=False)
    proposal = NextOpenWithSlippageV1().propose(
        order=_order(rules=rules, side=OrderSide.SELL),
        bar=locked,
        prev_close=prev_close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    assert proposal.filled is False
    assert proposal.skip_reason is FillSkipReason.LOCKED_AT_LIMIT_DOWN


def test_limit_price_not_met_skips(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    bar = provider.get_daily_bar(ETF, FILL_DAY)
    prev = provider.get_daily_bar(ETF, SIGNAL_DAY)
    assert bar is not None
    assert prev is not None
    proposal = NextOpenWithSlippageV1().propose(
        order=_order(rules=rules, limit_price=bar.open * Decimal("0.5")),
        bar=bar,
        prev_close=prev.close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    assert proposal.filled is False
    assert proposal.skip_reason is FillSkipReason.LIMIT_PRICE_NOT_MET


def test_quarantined_bar_does_not_fill(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    bar = provider.get_daily_bar(ETF, FILL_DAY)
    assert bar is not None
    from janus.domain.common.enums import DataQualityStatus

    proposal = NextOpenWithSlippageV1().propose(
        order=_order(rules=rules),
        bar=bar.model_copy(
            update={
                "quality": DataQualityStatus.QUARANTINED,
                "adjustment": AdjustmentMode.BACKWARD_ADJUSTED,
            }
        ),
        prev_close=bar.close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    assert proposal.filled is False
    assert proposal.skip_reason is FillSkipReason.DATA_NOT_EXECUTABLE


# ---------------------------------------------------------------------------
# Signal-day / look-ahead protection
# ---------------------------------------------------------------------------
def test_signal_bar_can_never_be_the_fill_bar(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    bar = provider.get_daily_bar(ETF, SIGNAL_DAY)
    assert bar is not None
    proposal = NextOpenWithSlippageV1().propose(
        order=_order(rules=rules, earliest=SIGNAL_DAY),
        bar=bar,
        prev_close=bar.close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
        signal_bar_date=SIGNAL_DAY,
    )
    assert proposal.filled is False
    assert proposal.skip_reason is FillSkipReason.SAME_DAY_SIGNAL


def test_fill_before_earliest_execution_date_is_refused(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    bar = provider.get_daily_bar(ETF, FILL_DAY)
    assert bar is not None
    proposal = NextOpenWithSlippageV1().propose(
        order=_order(rules=rules, earliest=date(2026, 11, 10)),
        bar=bar,
        prev_close=bar.close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    assert proposal.filled is False
    assert proposal.skip_reason is FillSkipReason.FILL_DATE_TOO_EARLY


def test_unsubmitted_order_is_never_filled(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    bar = provider.get_daily_bar(ETF, FILL_DAY)
    assert bar is not None
    proposal = NextOpenWithSlippageV1().propose(
        order=_order(rules=rules, status=OrderStatus.RISK_APPROVED),
        bar=bar,
        prev_close=bar.close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    assert proposal.filled is False
    assert proposal.skip_reason is FillSkipReason.ORDER_NOT_READY


# ---------------------------------------------------------------------------
# Partial fills and the order lifecycle
# ---------------------------------------------------------------------------
def test_participation_cap_produces_a_partial_fill(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    bar = provider.get_daily_bar(ETF, FILL_DAY)
    assert bar is not None
    tiny_volume_bar = bar.model_copy(update={"volume": 1_500})
    model = NextOpenWithSlippageV1(
        NextOpenWithSlippageV1Config(max_participation_rate=Decimal("0.10"))
    )
    proposal = model.propose(
        order=_order(rules=rules, quantity=5_000),
        bar=tiny_volume_bar,
        prev_close=bar.close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    assert proposal.filled is True
    fill = proposal.require_fill()
    assert fill.is_partial is True
    assert fill.quantity < 5_000
    assert fill.quantity % rules.buy_quantity_increment == 0
    # 10% of 1500 = 150 shares, rounded down to the 100-share increment.
    assert fill.quantity == 100

    submitted = _order(rules=rules, quantity=5_000)
    updated = PaperExecutionGateway().apply_fill(
        submitted, proposal, at=datetime(2026, 11, 6, 9, 31, tzinfo=MARKET_TZ)
    )
    assert updated.status is OrderStatus.PARTIALLY_FILLED
    assert updated.filled_quantity == 100
    assert updated.remaining_quantity == 4_900


def test_overfill_is_rejected(
    engine: object, instruments: dict[str, Instrument], provider: FixtureProvider
) -> None:
    rules = _rules(engine, instruments[ETF])
    bar = provider.get_daily_bar(ETF, FILL_DAY)
    assert bar is not None
    order = _order(rules=rules, quantity=100)
    proposal = NextOpenWithSlippageV1().propose(
        order=order,
        bar=bar.model_copy(update={"volume": 100_000_000}),
        prev_close=bar.close,
        rules=rules,
        fee_calculator=_fee_calculator(engine, rules),
        at=datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ),
        is_risk_warning=False,
    )
    # The model caps at the remaining quantity, so force an impossible fill.
    oversized = proposal.require_fill().model_copy(update={"quantity": 101})
    with pytest.raises(InvalidTransitionError):
        PaperExecutionGateway().apply_fill(
            order,
            dataclasses.replace(proposal, fill=oversized),
            at=datetime(2026, 11, 6, 9, 31, tzinfo=MARKET_TZ),
        )


def test_cancel_and_expire_are_terminal(engine: object, instruments: dict[str, Instrument]) -> None:
    rules = _rules(engine, instruments[ETF])
    order = _order(rules=rules)
    gateway = PaperExecutionGateway()
    at = datetime(2026, 11, 6, 9, 31, tzinfo=MARKET_TZ)

    cancelled = gateway.cancel(order, at=at, reason="operator cancelled")
    assert cancelled.status is OrderStatus.CANCELLED
    with pytest.raises(InvalidTransitionError):
        transition(cancelled, OrderStatus.FILLED, at=at)

    expired = gateway.expire(order, at=at)
    assert expired.status is OrderStatus.EXPIRED
    with pytest.raises(InvalidTransitionError):
        gateway.submit(expired, at=at)


def test_gateway_rejects_unapproved_and_foreign_objects(
    engine: object, instruments: dict[str, Instrument]
) -> None:
    rules = _rules(engine, instruments[ETF])
    at = datetime(2026, 11, 6, 9, 31, tzinfo=MARKET_TZ)
    gateway = PaperExecutionGateway()
    with pytest.raises(InvalidTransitionError):
        gateway.submit(_order(rules=rules, status=OrderStatus.PROPOSED), at=at)
    # The gateway accepts only internally generated PaperOrder objects, never
    # raw model text (spec s9.10).
    with pytest.raises(SecurityViolationError):
        gateway.submit("please buy 510300", at=at)  # type: ignore[arg-type]


def test_t_plus_zero_rule_profile_is_reachable(engine: object, provider: FixtureProvider) -> None:
    instrument = provider.instrument("510999.SH")
    assert instrument is not None
    rules = engine.resolve(instrument, SIGNAL_DAY)  # type: ignore[attr-defined]
    assert rules.settlement_rule is SettlementRule.T_PLUS_0
    stock = provider.instrument("600519.SH")
    assert stock is not None
    assert engine.resolve(stock, SIGNAL_DAY).settlement_rule is SettlementRule.T_PLUS_1  # type: ignore[attr-defined]
