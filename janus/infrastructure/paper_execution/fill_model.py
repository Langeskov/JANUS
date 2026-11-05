"""Versioned daily-frequency fill model.

Phase 0 spec s6.2 requires a *versioned* fill model, e.g.
``next_open_with_slippage_v1``:

* use the next valid trading session's **open** as the reference price;
* apply configurable adverse slippage in basis points;
* enforce the order's limit price and the applicable daily price limits;
* never fill when suspended, when no volume is reported, or when the instrument
  is not tradable;
* skip a fill when the daily bar indicates a locked limit state that makes
  execution implausible;
* cap simulated participation as a fraction of reported daily volume;
* round quantity to the instrument's quantity increment;
* preserve partial-fill / unfilled states;
* record the exact fill-model version and assumptions on every fill.

Daily OHLCV does **not** reveal intraday order sequence or queue priority. The
model never infers a fill merely because the day's range crossed a limit price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from janus.domain.common.enums import (
    AdjustmentMode,
    DataQualityStatus,
    OrderSide,
    OrderStatus,
    OrderType,
)
from janus.domain.common.errors import ValidationError
from janus.domain.common.money import Money
from janus.domain.common.time import ensure_aware
from janus.domain.market_data.models import MarketBar
from janus.domain.market_rules.models import FeeBreakdown, PriceLimits, ResolvedTradingRules
from janus.domain.orders.models import FILL_MODEL_VERSION, ExecutionFill, PaperOrder

__all__ = [
    "FILL_MODEL_VERSION",
    "FillModel",
    "FillProposal",
    "FillSkipReason",
    "NextOpenWithSlippageV1",
]


class FillSkipReason(StrEnum):
    """Why an order did not trade on a given session (audit-friendly)."""

    ORDER_NOT_READY = "ORDER_NOT_READY"
    NO_BAR = "NO_BAR"
    FILL_DATE_TOO_EARLY = "FILL_DATE_TOO_EARLY"
    SAME_DAY_SIGNAL = "SAME_DAY_SIGNAL"
    DATA_NOT_EXECUTABLE = "DATA_NOT_EXECUTABLE"
    SUSPENDED = "SUSPENDED"
    ZERO_VOLUME = "ZERO_VOLUME"
    LOCKED_AT_LIMIT_UP = "LOCKED_AT_LIMIT_UP"
    LOCKED_AT_LIMIT_DOWN = "LOCKED_AT_LIMIT_DOWN"
    LIMIT_PRICE_NOT_MET = "LIMIT_PRICE_NOT_MET"
    PRICE_OUTSIDE_DAILY_LIMITS = "PRICE_OUTSIDE_DAILY_LIMITS"
    PARTICIPATION_CAP = "PARTICIPATION_CAP"
    QUANTITY_BELOW_INCREMENT = "QUANTITY_BELOW_INCREMENT"
    ORDER_TYPE_UNSUPPORTED = "ORDER_TYPE_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class FillProposal:
    """Result of running the fill model for one order on one session."""

    order_id: str
    fill_model_version: str
    trade_date: date
    fill: ExecutionFill | None = None
    skip_reason: FillSkipReason | None = None
    notes: tuple[str, ...] = ()
    reference_price: Decimal | None = None

    @property
    def filled(self) -> bool:
        return self.fill is not None

    def require_fill(self) -> ExecutionFill:
        if self.fill is None:
            raise ValidationError(
                "fill proposal did not produce a fill", reason=str(self.skip_reason)
            )
        return self.fill


def _round_to_tick(value: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    rounding = "ROUND_CEILING" if up else "ROUND_FLOOR"
    quantized = value.quantize(tick, rounding=rounding)
    return quantized


@dataclass(frozen=True, slots=True)
class NextOpenWithSlippageV1Config:
    """Assumptions recorded on every fill produced by this model."""

    slippage_bps: Decimal = Decimal("5")
    """Adverse slippage applied to the reference open price."""

    max_participation_rate: Decimal = Decimal("0.10")
    """Cap on simulated participation as a fraction of reported daily volume."""

    allow_locked_limit_fills: bool = False
    """When False (default), a locked board cannot be traded."""


class FillModel(Protocol):
    """Port for a versioned fill model."""

    @property
    def version(self) -> str: ...

    def propose(
        self,
        *,
        order: PaperOrder,
        bar: MarketBar | None,
        prev_close: Decimal | None,
        rules: ResolvedTradingRules,
        fee_calculator: object,
        at: datetime,
        is_risk_warning: bool,
        signal_bar_date: date | None = None,
        data_snapshot_id: str | None = None,
    ) -> FillProposal: ...


class NextOpenWithSlippageV1:
    """The Phase 0 daily-frequency fill approximation."""

    __slots__ = ("_config",)

    def __init__(self, config: NextOpenWithSlippageV1Config | None = None) -> None:
        self._config = config or NextOpenWithSlippageV1Config()
        if not (0 <= self._config.max_participation_rate <= 1):
            raise ValidationError("max_participation_rate must be within [0, 1]")
        if self._config.slippage_bps < 0:
            raise ValidationError("slippage_bps must be non-negative")

    @property
    def version(self) -> str:
        return FILL_MODEL_VERSION

    @property
    def config(self) -> NextOpenWithSlippageV1Config:
        return self._config

    def propose(
        self,
        *,
        order: PaperOrder,
        bar: MarketBar | None,
        prev_close: Decimal | None,
        rules: ResolvedTradingRules,
        fee_calculator: object,
        at: datetime,
        is_risk_warning: bool,
        signal_bar_date: date | None = None,
        data_snapshot_id: str | None = None,
    ) -> FillProposal:
        from collections.abc import Callable

        ensure_aware(at, "at")
        if not callable(fee_calculator):  # pragma: no cover - defensive
            raise ValidationError("fee_calculator must be callable")
        compute_fees: Callable[[OrderSide, int, Decimal], FeeBreakdown] = fee_calculator

        def skip(reason: FillSkipReason, note: str = "") -> FillProposal:
            return FillProposal(
                order_id=order.order_id,
                fill_model_version=self.version,
                trade_date=bar.trade_date if bar is not None else order.target_trade_date,
                skip_reason=reason,
                notes=(note,) if note else (),
            )

        if order.status not in {OrderStatus.SUBMITTED_PAPER, OrderStatus.PARTIALLY_FILLED}:
            return skip(FillSkipReason.ORDER_NOT_READY, f"order status is {order.status.value}")
        if order.order_type is not OrderType.LIMIT:
            return skip(
                FillSkipReason.ORDER_TYPE_UNSUPPORTED,
                "the Phase 0 fill model simulates limit orders only",
            )
        if bar is None:
            return skip(FillSkipReason.NO_BAR, "no daily bar was available for the session")
        if bar.trade_date < order.earliest_execution_date:
            return skip(
                FillSkipReason.FILL_DATE_TOO_EARLY,
                f"bar date {bar.trade_date} precedes earliest execution date "
                f"{order.earliest_execution_date}",
            )
        if signal_bar_date is not None and bar.trade_date <= signal_bar_date:
            return skip(
                FillSkipReason.SAME_DAY_SIGNAL,
                "a signal bar may never also be the fill bar",
            )
        if bar.adjustment is not AdjustmentMode.UNADJUSTED:
            return skip(
                FillSkipReason.DATA_NOT_EXECUTABLE,
                "only unadjusted bars may back simulated execution",
            )
        if bar.quality is not DataQualityStatus.OK:
            return skip(FillSkipReason.DATA_NOT_EXECUTABLE, f"bar quality is {bar.quality.value}")
        if bar.volume <= 0:
            return skip(FillSkipReason.ZERO_VOLUME, "session reported no volume")

        limits = (
            rules.compute_price_limits(prev_close, is_risk_warning=is_risk_warning)
            if prev_close is not None
            else PriceLimits()
        )

        locked_up = limits.limit_up is not None and bar.low == bar.high == limits.limit_up
        locked_down = limits.limit_down is not None and bar.low == bar.high == limits.limit_down
        if not self._config.allow_locked_limit_fills:
            if order.side is OrderSide.BUY and locked_up:
                return skip(
                    FillSkipReason.LOCKED_AT_LIMIT_UP,
                    "the board was locked limit-up; buying is implausible",
                )
            if order.side is OrderSide.SELL and locked_down:
                return skip(
                    FillSkipReason.LOCKED_AT_LIMIT_DOWN,
                    "the board was locked limit-down; selling is implausible",
                )

        reference = bar.open
        slippage = self._config.slippage_bps / Decimal("10000")
        if order.side is OrderSide.BUY:
            raw_price = reference * (Decimal(1) + slippage)
            price = _round_to_tick(raw_price, rules.tick_size, up=True)
        else:
            raw_price = reference * (Decimal(1) - slippage)
            price = _round_to_tick(raw_price, rules.tick_size, up=False)

        if price <= 0:
            return skip(FillSkipReason.PRICE_OUTSIDE_DAILY_LIMITS, "non-positive fill price")

        if limits.has_limits:
            assert limits.limit_up is not None
            assert limits.limit_down is not None
            if price > limits.limit_up or price < limits.limit_down:
                return skip(
                    FillSkipReason.PRICE_OUTSIDE_DAILY_LIMITS,
                    f"fill price {price} is outside daily limits "
                    f"[{limits.limit_down}, {limits.limit_up}]",
                )

        if order.limit_price is None:  # pragma: no cover - model validated
            return skip(FillSkipReason.ORDER_TYPE_UNSUPPORTED, "limit order without a price")
        if order.side is OrderSide.BUY and price > order.limit_price:
            return skip(
                FillSkipReason.LIMIT_PRICE_NOT_MET,
                f"open+slippage {price} exceeds the buy limit {order.limit_price}",
            )
        if order.side is OrderSide.SELL and price < order.limit_price:
            return skip(
                FillSkipReason.LIMIT_PRICE_NOT_MET,
                f"open-slippage {price} is below the sell limit {order.limit_price}",
            )

        participation_cap = int(
            (Decimal(bar.volume) * self._config.max_participation_rate).to_integral_value(
                rounding="ROUND_FLOOR"
            )
        )
        if participation_cap <= 0:
            return skip(
                FillSkipReason.PARTICIPATION_CAP,
                f"reported volume {bar.volume} is too small for the participation cap",
            )
        raw_quantity = min(order.remaining_quantity, participation_cap)
        increment = (
            rules.buy_quantity_increment
            if order.side is OrderSide.BUY
            else rules.sell_quantity_increment
        )
        quantity = (raw_quantity // increment) * increment
        if quantity <= 0:
            return skip(
                FillSkipReason.QUANTITY_BELOW_INCREMENT,
                f"{raw_quantity} shares is below the {increment}-unit increment",
            )

        fees = compute_fees(order.side, quantity, price)
        notional = Money(amount=price * Decimal(quantity))
        is_partial = quantity < order.remaining_quantity
        fill = ExecutionFill(
            fill_id=f"{order.order_id}-fill-{bar.trade_date.isoformat()}",
            order_id=order.order_id,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            notional=notional,
            fees=fees,
            executed_at=at,
            trade_date=bar.trade_date,
            slippage_bps=self._config.slippage_bps,
            is_partial=is_partial,
            fill_model_version=self.version,
            rule_set_id=rules.rule_set_id,
            rule_set_version=rules.rule_set_version,
            fee_schedule_id=fees.fee_schedule_id,
            fee_schedule_version=fees.fee_schedule_version,
            data_snapshot_id=data_snapshot_id or order.data_snapshot_id,
            decision_id=order.decision_id,
            idempotency_key=f"{order.order_id}:{bar.trade_date.isoformat()}:{quantity}:{price}",
        )
        return FillProposal(
            order_id=order.order_id,
            fill_model_version=self.version,
            trade_date=bar.trade_date,
            fill=fill,
            notes=(
                f"fill model {self.version}",
                f"reference open {reference}",
                f"adverse slippage {self._config.slippage_bps} bps",
                f"participation cap {self._config.max_participation_rate} of {bar.volume} volume",
            ),
            reference_price=reference,
        )
