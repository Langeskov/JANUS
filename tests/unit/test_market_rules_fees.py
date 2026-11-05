"""Unit tests for componentised fee calculation (P0-D, spec s6.3)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from janus.domain.common.enums import FeeComponent, OrderSide
from janus.domain.common.errors import ConfigurationError, ValidationError
from janus.domain.common.time import MARKET_TZ
from janus.domain.market_rules.fees import compute_fee_breakdown, trade_notional
from janus.domain.market_rules.models import (
    FeeBasis,
    FeeRate,
    FeeSchedule,
    FeeScheduleStatus,
)

EFFECTIVE_FROM = datetime(2023, 1, 1, tzinfo=MARKET_TZ)


def _equity_schedule() -> FeeSchedule:
    return FeeSchedule(
        fee_schedule_id="CN-EQUITY-DEFAULT",
        version="test-1",
        status=FeeScheduleStatus.ESTIMATED,
        effective_from=EFFECTIVE_FROM,
        lines=(
            FeeRate(
                component=FeeComponent.BROKER_COMMISSION,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.00025"),
                min_amount=Decimal("5.00"),
            ),
            FeeRate(
                component=FeeComponent.STAMP_DUTY,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.0005"),
                applicable_sides=(OrderSide.SELL,),
            ),
            FeeRate(
                component=FeeComponent.TRANSFER_FEE,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.00001"),
            ),
            FeeRate(
                component=FeeComponent.EXCHANGE_HANDLING_FEE,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.0000341"),
            ),
        ),
    )


def _etf_schedule() -> FeeSchedule:
    return FeeSchedule(
        fee_schedule_id="CN-ETF-DEFAULT",
        version="test-1",
        status=FeeScheduleStatus.ESTIMATED,
        effective_from=EFFECTIVE_FROM,
        lines=(
            FeeRate(
                component=FeeComponent.BROKER_COMMISSION,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.00025"),
                min_amount=Decimal("5.00"),
            ),
            FeeRate(
                component=FeeComponent.EXCHANGE_HANDLING_FEE,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.0000341"),
            ),
        ),
    )


def test_buy_equity_breakdown_is_componentised() -> None:
    breakdown = compute_fee_breakdown(
        _equity_schedule(),
        side=OrderSide.BUY,
        quantity=1000,
        price=Decimal("10.00"),
    )

    assert breakdown.notional == Decimal("10000.00")
    assert breakdown.side is OrderSide.BUY
    assert breakdown.total == Decimal("5.44")

    components = breakdown.by_component()
    assert components[FeeComponent.BROKER_COMMISSION] == Decimal("2.50")
    assert components[FeeComponent.MIN_COMMISSION] == Decimal("2.50")
    assert components[FeeComponent.TRANSFER_FEE] == Decimal("0.10")
    assert components[FeeComponent.EXCHANGE_HANDLING_FEE] == Decimal("0.34")
    assert FeeComponent.STAMP_DUTY not in components


def test_commission_floor_keeps_rate_and_top_up_visible() -> None:
    breakdown = compute_fee_breakdown(
        _equity_schedule(),
        side=OrderSide.BUY,
        quantity=1000,
        price=Decimal("10.00"),
    )

    commission_lines = [
        line for line in breakdown.lines if line.component is FeeComponent.BROKER_COMMISSION
    ]
    minimum_lines = [
        line for line in breakdown.lines if line.component is FeeComponent.MIN_COMMISSION
    ]

    assert len(commission_lines) == 1
    assert len(minimum_lines) == 1
    assert commission_lines[0].rate == Decimal("0.00025")
    assert commission_lines[0].amount == Decimal("2.50")
    assert minimum_lines[0].basis is FeeBasis.PER_ORDER
    assert minimum_lines[0].amount == Decimal("2.50")
    assert commission_lines[0].amount + minimum_lines[0].amount == Decimal("5.00")


def test_sell_equity_charges_stamp_duty() -> None:
    breakdown = compute_fee_breakdown(
        _equity_schedule(),
        side=OrderSide.SELL,
        quantity=1000,
        price=Decimal("10.00"),
    )

    assert breakdown.by_component()[FeeComponent.STAMP_DUTY] == Decimal("5.00")
    assert breakdown.total == Decimal("10.44")


def test_large_order_does_not_hit_commission_floor() -> None:
    breakdown = compute_fee_breakdown(
        _equity_schedule(),
        side=OrderSide.BUY,
        quantity=100_000,
        price=Decimal("10.00"),
    )

    components = breakdown.by_component()
    assert breakdown.notional == Decimal("1000000.00")
    assert components[FeeComponent.BROKER_COMMISSION] == Decimal("250.00")
    assert FeeComponent.MIN_COMMISSION not in components
    assert components[FeeComponent.TRANSFER_FEE] == Decimal("10.00")
    assert components[FeeComponent.EXCHANGE_HANDLING_FEE] == Decimal("34.10")
    assert breakdown.total == Decimal("294.10")


def test_etf_schedule_has_no_stamp_duty_on_either_side() -> None:
    buy = compute_fee_breakdown(
        _etf_schedule(), side=OrderSide.BUY, quantity=1000, price=Decimal("3.500")
    )
    sell = compute_fee_breakdown(
        _etf_schedule(), side=OrderSide.SELL, quantity=1000, price=Decimal("3.500")
    )

    assert FeeComponent.STAMP_DUTY not in buy.by_component()
    assert FeeComponent.STAMP_DUTY not in sell.by_component()
    assert buy.notional == Decimal("3500.00")
    assert buy.total == Decimal("5.12")
    assert sell.total == Decimal("5.12")


def test_every_line_amount_is_quantized_to_cents() -> None:
    breakdown = compute_fee_breakdown(
        _equity_schedule(),
        side=OrderSide.BUY,
        quantity=333,
        price=Decimal("7.77"),
    )

    for line in breakdown.lines:
        assert line.amount == line.amount.quantize(Decimal("0.01"))
        assert line.amount >= Decimal("0")


def test_breakdown_total_equals_sum_of_lines() -> None:
    breakdown = compute_fee_breakdown(
        _equity_schedule(),
        side=OrderSide.SELL,
        quantity=250,
        price=Decimal("31.25"),
    )

    assert breakdown.total == sum(line.amount for line in breakdown.lines)


def test_slippage_is_rejected_as_fee_line() -> None:
    schedule = FeeSchedule(
        fee_schedule_id="CN-EQUITY-DEFAULT",
        version="bad",
        effective_from=EFFECTIVE_FROM,
        lines=(
            FeeRate(
                component=FeeComponent.SLIPPAGE,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.001"),
            ),
        ),
    )

    with pytest.raises(ConfigurationError, match="SLIPPAGE"):
        compute_fee_breakdown(schedule, side=OrderSide.BUY, quantity=100, price=Decimal("10.00"))


def test_quantity_and_price_are_validated() -> None:
    with pytest.raises(ValidationError, match="quantity"):
        compute_fee_breakdown(
            _equity_schedule(), side=OrderSide.BUY, quantity=0, price=Decimal("10.00")
        )
    with pytest.raises(ValidationError, match="price"):
        compute_fee_breakdown(
            _equity_schedule(), side=OrderSide.BUY, quantity=100, price=Decimal("0")
        )


def test_float_price_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Decimal"):
        compute_fee_breakdown(
            _equity_schedule(),
            side=OrderSide.BUY,
            quantity=100,
            price=10.0,  # type: ignore[arg-type]
        )


def test_per_order_and_quantity_bases_are_supported() -> None:
    schedule = FeeSchedule(
        fee_schedule_id="CN-TEST",
        version="test",
        effective_from=EFFECTIVE_FROM,
        lines=(
            FeeRate(
                component=FeeComponent.REGULATORY_FEE,
                basis=FeeBasis.PER_ORDER,
                rate=Decimal("1.00"),
            ),
            FeeRate(
                component=FeeComponent.EXCHANGE_HANDLING_FEE,
                basis=FeeBasis.QUANTITY,
                rate=Decimal("0.0002"),
            ),
        ),
    )

    breakdown = compute_fee_breakdown(
        schedule, side=OrderSide.BUY, quantity=1000, price=Decimal("10.00")
    )

    components = breakdown.by_component()
    assert components[FeeComponent.REGULATORY_FEE] == Decimal("1.00")
    assert components[FeeComponent.EXCHANGE_HANDLING_FEE] == Decimal("0.20")


def test_trade_notional_rounds_half_up() -> None:
    assert trade_notional(Decimal("10.005"), 1) == Decimal("10.01")
    assert trade_notional(Decimal("3.500"), 1000) == Decimal("3500.00")
