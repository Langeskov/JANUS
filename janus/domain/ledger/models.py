"""Append-only cash and position ledger models.

Phase 0 spec s5.2: cash and holdings are *derived* by replaying immutable
ledger entries. A materialised balance may be cached but must be reproducible
from the opening event and the entry history.

Every entry carries an ``idempotency_key``; replaying the same event twice must
be harmless (acceptance criterion 4).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from janus.domain.common.errors import InvariantViolation, ValidationError
from janus.domain.common.ids import ensure_id
from janus.domain.common.money import Money
from janus.domain.common.time import ensure_aware
from janus.domain.instruments.symbol import validate_symbol

__all__ = [
    "CashLedgerEntry",
    "LedgerEntryType",
    "OpenLot",
    "PositionEvent",
    "PositionEventType",
    "RemainingLot",
]


class LedgerEntryType(StrEnum):
    """Why cash moved. One enumeration value per permitted reason."""

    OPENING_CAPITAL = "OPENING_CAPITAL"
    FILL_SETTLEMENT = "FILL_SETTLEMENT"
    FILL_FEE = "FILL_FEE"
    CORPORATE_ACTION_CASH = "CORPORATE_ACTION_CASH"
    ADMIN_RESET = "ADMIN_RESET"
    CORRECTION = "CORRECTION"

    @property
    def is_administrative(self) -> bool:
        return self in {LedgerEntryType.OPENING_CAPITAL, LedgerEntryType.ADMIN_RESET}


class PositionEventType(StrEnum):
    """Why a holding changed."""

    BUY_FILL = "BUY_FILL"
    SELL_FILL = "SELL_FILL"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    CORRECTION = "CORRECTION"


class CashLedgerEntry(BaseModel):
    """One immutable signed cash movement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str
    account_id: str
    sequence: int = Field(ge=1)
    """Monotonic per-account sequence number; uniqueness is enforced by storage."""

    entry_type: LedgerEntryType
    amount: Money
    """Signed CNY delta. Positive credits cash, negative debits it."""

    trade_date: date
    occurred_at: datetime
    source_ref: str
    """Entity that caused the entry (fill id, decision id, admin action id)."""

    idempotency_key: str = Field(min_length=1, max_length=200)
    description: str = ""
    balance_after: Money | None = None
    """Optional cached running balance. Replay verifies it when present."""

    @model_validator(mode="after")
    def _validate(self) -> CashLedgerEntry:
        ensure_id(self.entry_id, "entry_id")
        ensure_id(self.account_id, "account_id")
        ensure_aware(self.occurred_at, "occurred_at")
        if self.amount.is_zero():
            raise ValidationError("a cash ledger entry must move a non-zero amount")
        if self.entry_type is not LedgerEntryType.OPENING_CAPITAL and self.amount.is_positive():
            pass  # credits are fine for any type (sells, dividends)
        return self

    @property
    def is_debit(self) -> bool:
        return self.amount.is_negative()


class PositionEvent(BaseModel):
    """One immutable signed position change.

    Buys record ``available_date`` and ``unit_cost`` so that lots can be rebuilt
    from history alone -- replay must not need today's rules or calendar.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    account_id: str
    sequence: int = Field(ge=1)
    event_type: PositionEventType
    symbol: str
    quantity_delta: int
    """Signed share/unit delta. Positive = acquisition, negative = disposal."""

    trade_date: date
    occurred_at: datetime
    source_ref: str
    idempotency_key: str = Field(min_length=1, max_length=200)

    available_date: date | None = None
    """Earliest date the acquired quantity becomes sellable (buys only)."""

    unit_cost: Decimal | None = Field(default=None, ge=Decimal("0"))
    """Per-share acquisition cost including buy-side fees (buys only)."""

    cost_basis_delta: Money
    """Signed cost-basis change: positive for buys, negative on disposal."""

    realized_pnl: Money
    """Realised P&L recognised by this event (non-zero on disposals)."""

    description: str = ""

    @model_validator(mode="after")
    def _validate(self) -> PositionEvent:
        ensure_id(self.event_id, "event_id")
        ensure_id(self.account_id, "account_id")
        ensure_aware(self.occurred_at, "occurred_at")
        if self.symbol != "CASH.CNY":
            validate_symbol(self.symbol)
        if self.quantity_delta == 0:
            raise ValidationError("a position event must change quantity")
        acquisition = self.event_type in {
            PositionEventType.BUY_FILL,
            PositionEventType.CORPORATE_ACTION,
        }
        if acquisition:
            if self.quantity_delta < 0:
                raise ValidationError("acquisition events require a positive quantity_delta")
            if self.available_date is None:
                raise ValidationError("acquisition events must declare available_date")
            if self.unit_cost is None:
                raise ValidationError("acquisition events must declare unit_cost")
            if self.cost_basis_delta.amount < 0:
                raise ValidationError("acquisitions must not release cost basis")
        else:
            if self.quantity_delta > 0:
                raise ValidationError("disposal events require a negative quantity_delta")
            if self.cost_basis_delta.amount > 0:
                raise ValidationError("disposals must release, not add, cost basis")
        return self

    @property
    def is_acquisition(self) -> bool:
        return self.quantity_delta > 0


class OpenLot(BaseModel):
    """Instruction to open a new position lot (input to the accounting service)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lot_id: str
    symbol: str
    quantity: int = Field(gt=0)
    acquired_trade_date: date
    available_date: date
    unit_cost: Decimal = Field(ge=Decimal("0"))

    @model_validator(mode="after")
    def _validate(self) -> OpenLot:
        validate_symbol(self.symbol)
        if self.available_date < self.acquired_trade_date:
            raise InvariantViolation("available_date precedes acquired_trade_date")
        return self


class RemainingLot(BaseModel):
    """A lot as reconstructed during replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lot_id: str
    symbol: str
    quantity: int = Field(gt=0)
    remaining_quantity: int = Field(ge=0)
    acquired_trade_date: date
    available_date: date
    unit_cost: Decimal = Field(ge=Decimal("0"))
    source_event_id: str

    @model_validator(mode="after")
    def _validate(self) -> RemainingLot:
        if self.remaining_quantity > self.quantity:
            raise ValidationError("remaining_quantity exceeds quantity")
        return self

    @property
    def is_available(self) -> bool:
        return self.remaining_quantity > 0

    def is_available_on(self, as_of: date) -> bool:
        return self.remaining_quantity > 0 and self.available_date <= as_of

    @property
    def remaining_cost_basis(self) -> Money:
        return Money(amount=self.unit_cost * Decimal(self.remaining_quantity))
