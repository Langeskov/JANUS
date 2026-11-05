"""Order intent, paper order and execution fill models.

Order intent is what an agent/portfolio manager *wants*; it is never executable.
Only an order that passed the deterministic risk controller can reach the paper
gateway (Phase 0 spec s6.4, s8).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from janus.domain.common.enums import OrderSide, OrderStatus, OrderType, OrderValidity
from janus.domain.common.errors import (
    InvariantViolation,
    UnsupportedEntityError,
    ValidationError,
)
from janus.domain.common.ids import ensure_id
from janus.domain.common.money import Money, as_decimal
from janus.domain.common.time import ensure_aware
from janus.domain.instruments.symbol import validate_symbol
from janus.domain.market_rules.models import FeeBreakdown

FILL_MODEL_VERSION = "next_open_with_slippage_v1"
"""Versioned daily-frequency fill model (spec s6.2)."""

__all__ = [
    "FILL_MODEL_VERSION",
    "ExecutionFill",
    "OrderIntent",
    "PaperOrder",
]


class OrderIntent(BaseModel):
    """A desired order *before* deterministic risk validation.

    Traceability references are optional at construction on purpose: the risk
    controller must be able to reject an intent whose references are missing
    (Phase 0 spec s8.1, acceptance criterion 9).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str
    account_id: str
    symbol: str
    side: OrderSide
    quantity: int = Field(gt=0)
    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal | None = None
    validity: OrderValidity = OrderValidity.DAY
    created_at: datetime

    earliest_execution_date: date
    """Earliest trading date on which the order may fill.

    A signal derived from day T data must not execute on day T
    (spec s6.1, acceptance criterion 7).
    """

    signal_bar_date: date | None = None
    """Trading date of the bar that generated the signal."""

    # -- traceability (required for approval, checked by the risk controller) --
    decision_id: str | None = None
    data_snapshot_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    rule_set_id: str | None = None
    rule_set_version: str | None = None

    rationale: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _validate(self) -> OrderIntent:
        ensure_id(self.intent_id, "intent_id")
        ensure_id(self.account_id, "account_id")
        validate_symbol(self.symbol)
        ensure_aware(self.created_at, "created_at")
        if not self.order_type.is_supported:
            raise UnsupportedEntityError(
                "Phase 0 supports limit orders only; market orders require an order book",
                order_type=self.order_type.value,
            )
        if self.limit_price is not None and as_decimal(self.limit_price) <= 0:
            raise ValidationError("limit_price must be positive", price=str(self.limit_price))
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValidationError("a LIMIT order intent requires a limit_price")
        return self

    @property
    def notional_at_limit(self) -> Money:
        if self.limit_price is None:  # pragma: no cover - guarded above
            raise InvariantViolation("cannot price a limit-less intent")
        return Money(amount=self.limit_price * Decimal(self.quantity))


class PaperOrder(BaseModel):
    """An accepted, internally generated paper order.

    The paper gateway accepts only approved order ids; it never accepts raw model
    text (spec s9.10).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    intent_id: str
    account_id: str
    symbol: str
    side: OrderSide
    quantity: int = Field(gt=0)
    filled_quantity: int = Field(default=0, ge=0)
    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal | None = None
    validity: OrderValidity = OrderValidity.DAY
    status: OrderStatus = OrderStatus.PROPOSED

    created_at: datetime
    updated_at: datetime
    target_trade_date: date
    earliest_execution_date: date

    # -- immutable version references (acceptance criterion 9) ----------
    rule_set_id: str
    rule_set_version: str
    fee_schedule_id: str
    fee_schedule_version: str
    fill_model_version: str = FILL_MODEL_VERSION
    data_snapshot_id: str | None = None
    decision_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    risk_decision_id: str | None = None

    rejection_reason: str | None = None
    cancel_reason: str | None = None
    approved_at: datetime | None = None
    submitted_at: datetime | None = None
    terminal_at: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> PaperOrder:
        ensure_id(self.order_id, "order_id")
        ensure_id(self.intent_id, "intent_id")
        ensure_id(self.account_id, "account_id")
        validate_symbol(self.symbol)
        ensure_aware(self.created_at, "created_at")
        ensure_aware(self.updated_at, "updated_at")
        if self.filled_quantity > self.quantity:
            raise InvariantViolation(
                "filled_quantity cannot exceed quantity",
                filled=self.filled_quantity,
                quantity=self.quantity,
            )
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValidationError("a LIMIT paper order requires a limit_price")
        if self.status is OrderStatus.FILLED and self.filled_quantity != self.quantity:
            raise InvariantViolation("FILLED requires filled_quantity == quantity")
        if self.status is OrderStatus.PARTIALLY_FILLED and not (
            0 < self.filled_quantity < self.quantity
        ):
            raise InvariantViolation("PARTIALLY_FILLED requires 0 < filled < quantity")
        if self.status is OrderStatus.RISK_REJECTED and not self.rejection_reason:
            raise InvariantViolation("RISK_REJECTED requires a rejection_reason")
        if (
            self.status
            in {
                OrderStatus.SUBMITTED_PAPER,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
            }
            and self.risk_decision_id is None
        ):
            raise InvariantViolation("an executable order requires a risk_decision_id")
        if self.status.is_terminal and self.terminal_at is None:
            raise InvariantViolation("a terminal order requires terminal_at")
        return self

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    @property
    def is_open(self) -> bool:
        return self.status.is_live


class ExecutionFill(BaseModel):
    """One simulated execution. A fill -- not an order -- moves the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fill_id: str
    order_id: str
    account_id: str
    symbol: str
    side: OrderSide
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)
    notional: Money
    fees: FeeBreakdown

    executed_at: datetime
    trade_date: date
    slippage_bps: Decimal = Decimal("0")
    is_partial: bool = False

    # -- version references ---------------------------------------------
    fill_model_version: str = FILL_MODEL_VERSION
    rule_set_id: str
    rule_set_version: str
    fee_schedule_id: str
    fee_schedule_version: str
    data_snapshot_id: str | None = None
    decision_id: str | None = None

    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _validate(self) -> ExecutionFill:
        ensure_id(self.fill_id, "fill_id")
        ensure_id(self.order_id, "order_id")
        validate_symbol(self.symbol)
        ensure_aware(self.executed_at, "executed_at")
        expected = Money(amount=self.price * Decimal(self.quantity))
        if expected.amount != self.notional.amount:
            raise InvariantViolation(
                "fill notional must equal price * quantity",
                expected=str(expected),
                actual=str(self.notional),
            )
        if self.fees.side is not self.side:
            raise ValidationError("fee breakdown side must match the fill side")
        return self

    @property
    def gross_proceeds(self) -> Money:
        """Cash impact before fees (positive for sells, negative for buys)."""
        if self.side is OrderSide.SELL:
            return self.notional
        return -self.notional

    @property
    def total_fees(self) -> Money:
        return Money(amount=self.fees.total)

    @property
    def net_cash_impact(self) -> Money:
        """Signed cash delta including all simulated fees."""
        return self.gross_proceeds - self.total_fees
