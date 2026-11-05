"""Componentised, deterministic fee calculation (Phase 0 spec s6.3).

Fees are *configuration data*, never hard-coded percentages: the numbers live in
``config/market_rules/fees_*.yaml`` and reach this module as an effective-dated
:class:`~janus.domain.market_rules.models.FeeSchedule`.

Representation rules
--------------------
* Every component is a separate :class:`~janus.domain.market_rules.models.FeeLine`
  (broker commission, minimum commission, stamp duty, transfer fee, exchange
  handling fee). Costs are never collapsed into one unexplained percentage.
* Slippage is **not** a fee: it belongs to the versioned fill model
  (spec s6.2). A fee schedule that declares a ``SLIPPAGE`` line is rejected.
* Commission floors stay visible: the rate-derived amount is recorded as
  ``BROKER_COMMISSION`` and the amount added on top to reach the contractual
  floor is recorded as a separate ``MIN_COMMISSION`` line. That keeps the total
  equal to ``max(rate * notional, minimum)`` while preserving both facts.
* All arithmetic is :class:`decimal.Decimal`; every amount is rounded half-up to
  the CNY minor unit (``0.01``) at the line level, so a breakdown is exactly
  reproducible and sums deterministically.
"""

from __future__ import annotations

from decimal import Decimal

from janus.domain.common.enums import FeeComponent, OrderSide
from janus.domain.common.errors import ConfigurationError, ValidationError
from janus.domain.common.money import as_decimal, quantize_cny
from janus.domain.market_rules.models import (
    FeeBasis,
    FeeBreakdown,
    FeeLine,
    FeeRate,
    FeeSchedule,
)

__all__ = [
    "compute_fee_breakdown",
    "trade_notional",
]


def trade_notional(price: Decimal, quantity: int) -> Decimal:
    """Executed trade value ``price * quantity``, rounded to the CNY minor unit.

    Quantity must be a positive integer and price a positive Decimal; both are
    validated here so callers cannot smuggle a float or a negative amount into
    the fee math.
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValidationError("quantity must be an integer", quantity=repr(quantity))
    if quantity <= 0:
        raise ValidationError("quantity must be positive", quantity=quantity)
    unit_price = as_decimal(price, "price")
    if unit_price <= 0:
        raise ValidationError("price must be positive", price=str(unit_price))
    return quantize_cny(unit_price * Decimal(quantity))


def _raw_amount(line: FeeRate, *, notional: Decimal, quantity: int) -> Decimal:
    """Un-rounded component amount implied by the line's basis and rate."""
    if line.basis is FeeBasis.NOTIONAL:
        return notional * line.rate
    if line.basis is FeeBasis.QUANTITY:
        return Decimal(quantity) * line.rate
    return line.rate


def _bounded_amount(line: FeeRate, raw: Decimal, *, fee_schedule_id: str) -> Decimal:
    """Apply the line's contractual floor/cap, then round to the CNY minor unit."""
    amount = raw
    if line.min_amount is not None and amount < line.min_amount:
        amount = line.min_amount
    if line.max_amount is not None and amount > line.max_amount:
        amount = line.max_amount
    if amount < 0:  # pragma: no cover - rates are validated non-negative
        raise ConfigurationError(
            "a fee line produced a negative amount",
            fee_schedule_id=fee_schedule_id,
            component=line.component.value,
        )
    return quantize_cny(amount)


def _component_lines(
    schedule: FeeSchedule,
    line: FeeRate,
    *,
    notional: Decimal,
    quantity: int,
) -> tuple[FeeLine, ...]:
    """Convert one configured :class:`FeeRate` into one or more computed lines."""
    raw = _raw_amount(line, notional=notional, quantity=quantity)
    bounded = _bounded_amount(line, raw, fee_schedule_id=schedule.fee_schedule_id)
    if line.component is not FeeComponent.BROKER_COMMISSION:
        return (
            FeeLine(
                component=line.component,
                basis=line.basis,
                rate=line.rate,
                amount=bounded,
            ),
        )
    # Commission floor/cap handling keeps the rate-derived amount visible.
    rate_derived = quantize_cny(raw)
    if bounded == rate_derived:
        return (
            FeeLine(
                component=FeeComponent.BROKER_COMMISSION,
                basis=line.basis,
                rate=line.rate,
                amount=rate_derived,
            ),
        )
    if bounded < rate_derived:
        # An explicit maximum cap reduced the rate-derived commission.
        return (
            FeeLine(
                component=FeeComponent.BROKER_COMMISSION,
                basis=line.basis,
                rate=line.rate,
                amount=bounded,
            ),
        )
    top_up = bounded - rate_derived
    return (
        FeeLine(
            component=FeeComponent.BROKER_COMMISSION,
            basis=line.basis,
            rate=line.rate,
            amount=rate_derived,
        ),
        FeeLine(
            component=FeeComponent.MIN_COMMISSION,
            basis=FeeBasis.PER_ORDER,
            rate=top_up,
            amount=top_up,
        ),
    )


def compute_fee_breakdown(
    schedule: FeeSchedule,
    *,
    side: OrderSide,
    quantity: int,
    price: Decimal,
) -> FeeBreakdown:
    """Itemised fee breakdown for one simulated execution.

    Only lines applicable to ``side`` are charged, so sell-only stamp duty never
    appears on a buy. The notional recorded on the breakdown is the quantized
    ``price * quantity`` value that the ledger will see.
    """
    notional = trade_notional(price, quantity)
    lines: list[FeeLine] = []
    for line in schedule.lines_for(side):
        if line.component is FeeComponent.SLIPPAGE:
            raise ConfigurationError(
                "SLIPPAGE must not be modelled as a fee line; "
                "slippage is part of the versioned fill model (spec s6.2/s6.3)",
                fee_schedule_id=schedule.fee_schedule_id,
                component=line.component.value,
            )
        lines.extend(_component_lines(schedule, line, notional=notional, quantity=quantity))
    return FeeBreakdown(
        fee_schedule_id=schedule.fee_schedule_id,
        fee_schedule_version=schedule.version,
        side=side,
        notional=notional,
        lines=tuple(lines),
    )
