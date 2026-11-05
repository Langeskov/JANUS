"""Portfolio account, positions and valuation snapshots.

Accounting invariants (Phase 0 spec s5.1) are enforced by the ledger layer; this
module defines the *views* an operator or API sees.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from janus.domain.common.enums import AccountState
from janus.domain.common.errors import UnsupportedEntityError, ValidationError
from janus.domain.common.ids import new_id
from janus.domain.common.money import CNY, ZERO_CNY, Money, as_decimal
from janus.domain.common.time import ensure_aware
from janus.domain.instruments.symbol import validate_symbol

__all__ = [
    "AccountMode",
    "PortfolioAccount",
    "PortfolioSnapshot",
    "Position",
    "PositionView",
    "TargetAllocation",
    "TargetWeight",
]


class AccountMode(StrEnum):
    """Execution mode. Phase 0 only ever creates ``PAPER``."""

    PAPER = "PAPER"
    LIVE = "LIVE"


class PortfolioAccount(BaseModel):
    """Fixed-capital paper account.

    The principal is set exactly once at creation. Phase 0 exposes no top-up,
    withdrawal, or manual balance edit to any agent (spec s5.1, s8.3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    mode: AccountMode = AccountMode.PAPER
    currency: str = CNY
    initial_capital: Money
    initial_cash_ratio: Decimal = Decimal("1.0")
    allow_external_flows: bool = False
    reset_requires_admin: bool = True
    state: AccountState = AccountState.ACTIVE
    created_at: datetime
    version: int = Field(default=1, ge=1)

    pause_reason: str | None = None
    paused_at: datetime | None = None

    @field_validator("mode")
    @classmethod
    def _paper_only(cls, value: AccountMode) -> AccountMode:
        if value is not AccountMode.PAPER:
            raise UnsupportedEntityError(
                "Phase 0 supports paper trading only; live account creation is refused",
                requested_mode=value.value,
            )
        return value

    @field_validator("initial_capital")
    @classmethod
    def _positive_capital(cls, value: Money) -> Money:
        if not value.is_positive():
            raise ValidationError("initial_capital must be strictly positive", amount=str(value))
        return value

    @field_validator("initial_cash_ratio")
    @classmethod
    def _full_cash(cls, value: Decimal) -> Decimal:
        if as_decimal(value) != Decimal("1.0"):
            raise UnsupportedEntityError(
                "Phase 0 accounts must start 100% in cash (initial_cash_ratio = 1.0)",
                initial_cash_ratio=str(value),
            )
        return Decimal("1.0")

    @field_validator("allow_external_flows")
    @classmethod
    def _no_flows(cls, value: bool) -> bool:
        if value:
            raise UnsupportedEntityError(
                "external deposits/withdrawals are not permitted in Phase 0"
            )
        return value

    @field_validator("created_at", "paused_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_aware(value, "timestamp")

    @model_validator(mode="after")
    def _pause_consistency(self) -> PortfolioAccount:
        if self.state is AccountState.PAUSED and self.pause_reason is None:
            raise ValidationError("a PAUSED account must record a pause_reason")
        if self.state is AccountState.ACTIVE and self.pause_reason is not None:
            raise ValidationError("an ACTIVE account must not carry a pause_reason")
        return self

    @property
    def is_active(self) -> bool:
        return self.state is AccountState.ACTIVE

    @property
    def accepts_new_orders(self) -> bool:
        return self.state is AccountState.ACTIVE

    def paused(self, reason: str, at: datetime, *, new_version: int) -> PortfolioAccount:
        """Return a paused copy (kill switch). Never liquidates positions."""
        return self.model_copy(
            update={
                "state": AccountState.PAUSED,
                "pause_reason": reason,
                "paused_at": ensure_aware(at, "at"),
                "version": new_version,
            }
        )

    def resumed(self, at: datetime, *, new_version: int) -> PortfolioAccount:
        ensure_aware(at, "at")
        return self.model_copy(
            update={
                "state": AccountState.ACTIVE,
                "pause_reason": None,
                "paused_at": None,
                "version": new_version,
            }
        )

    @classmethod
    def create_paper_account(
        cls,
        *,
        account_id: str,
        initial_capital: Money,
        created_at: datetime,
    ) -> PortfolioAccount:
        return cls(
            account_id=account_id,
            mode=AccountMode.PAPER,
            currency=CNY,
            initial_capital=initial_capital,
            initial_cash_ratio=Decimal("1.0"),
            allow_external_flows=False,
            reset_requires_admin=True,
            state=AccountState.ACTIVE,
            created_at=created_at,
            version=1,
        )


class PositionView(BaseModel):
    """A position as derived from ledger lots (not a mutable balance)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: int = Field(ge=0)
    available_quantity: int = Field(ge=0)
    """Quantity sellable on the valuation date (T+1 availability applied)."""

    cost_basis: Money
    """Total acquisition cost of the remaining lots (fees included)."""

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return validate_symbol(value)

    @model_validator(mode="after")
    def _available_le_total(self) -> PositionView:
        if self.available_quantity > self.quantity:
            raise ValidationError(
                "available_quantity cannot exceed total quantity",
                symbol=self.symbol,
                available=self.available_quantity,
                total=self.quantity,
            )
        return self

    @property
    def average_cost(self) -> Decimal:
        if self.quantity == 0:
            return Decimal("0")
        return (self.cost_basis.amount / Decimal(self.quantity)).quantize(Decimal("0.0001"))


class Position(BaseModel):
    """A valued position inside a :class:`PortfolioSnapshot`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: int = Field(ge=0)
    available_quantity: int = Field(ge=0)
    cost_basis: Money
    market_price: Decimal = Field(ge=0)
    market_value: Money
    price_as_of: datetime | None = None
    price_is_stale: bool = False

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return validate_symbol(value)

    @model_validator(mode="after")
    def _value_consistency(self) -> Position:
        expected = Money(amount=self.market_price * Decimal(self.quantity))
        if expected.amount != self.market_value.amount:
            raise ValidationError(
                "market_value must equal market_price * quantity",
                symbol=self.symbol,
                expected=str(expected),
                actual=str(self.market_value),
            )
        return self

    @property
    def unrealized_pnl(self) -> Money:
        return Money(amount=self.market_value.amount - self.cost_basis.amount)


class PortfolioSnapshot(BaseModel):
    """Valuation and exposure at a point in time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str
    account_id: str
    as_of: datetime
    valuation_date: date
    cash: Money
    positions: tuple[Position, ...] = ()
    nav: Money
    unrealized_pnl: Money = ZERO_CNY
    realized_pnl: Money = ZERO_CNY

    # -- staleness / provenance -----------------------------------------
    oldest_price_as_of: datetime | None = None
    any_price_stale: bool = False
    data_snapshot_id: str | None = None
    rule_set_id: str | None = None

    @field_validator("as_of")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "as_of")

    @model_validator(mode="after")
    def _nav_consistent(self) -> PortfolioSnapshot:
        gross = sum((p.market_value.amount for p in self.positions), Decimal("0"))
        expected = self.cash.amount + gross
        if expected != self.nav.amount:
            raise ValidationError(
                "nav must equal cash + sum(market_value)",
                expected=str(expected),
                actual=str(self.nav.amount),
            )
        symbols = [p.symbol for p in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValidationError("duplicate symbol in portfolio snapshot")
        return self

    @property
    def gross_exposure(self) -> Money:
        return Money(amount=sum((p.market_value.amount for p in self.positions), Decimal("0")))

    def position(self, symbol: str) -> Position | None:
        for position in self.positions:
            if position.symbol == symbol:
                return position
        return None

    def weight_of(self, symbol: str) -> Decimal:
        if self.nav.amount == 0:
            return Decimal("0")
        position = self.position(symbol)
        if position is None:
            return Decimal("0")
        return (position.market_value.amount / self.nav.amount).quantize(Decimal("0.000001"))

    @property
    def cash_weight(self) -> Decimal:
        if self.nav.amount == 0:
            return Decimal("0")
        return (self.cash.amount / self.nav.amount).quantize(Decimal("0.000001"))

    @classmethod
    def empty(
        cls,
        *,
        account_id: str,
        cash: Money,
        as_of: datetime,
        valuation_date: date,
    ) -> PortfolioSnapshot:
        return cls(
            snapshot_id=new_id("psnap"),
            account_id=account_id,
            as_of=ensure_aware(as_of, "as_of"),
            valuation_date=valuation_date,
            cash=cash,
            positions=(),
            nav=cash,
        )


class TargetWeight(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    weight: Decimal

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return validate_symbol(value)

    @field_validator("weight")
    @classmethod
    def _range(cls, value: Decimal) -> Decimal:
        weight = as_decimal(value, "weight")
        if weight < 0 or weight > 1:
            raise ValidationError("weight must be within [0, 1]", weight=str(value))
        return weight


class TargetAllocation(BaseModel):
    """A validated, duplicate-free target weight vector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    weights: tuple[TargetWeight, ...]
    tolerance: Decimal = Decimal("0.0001")

    @model_validator(mode="after")
    def _validate(self) -> TargetAllocation:
        symbols = [w.symbol for w in self.weights]
        if len(symbols) != len(set(symbols)):
            raise ValidationError("duplicate symbol in target allocation", symbols=symbols)
        total = sum((w.weight for w in self.weights), Decimal("0"))
        if abs(total - Decimal("1")) > self.tolerance:
            raise ValidationError(
                "target weights must sum to 1 within tolerance",
                total=str(total),
                tolerance=str(self.tolerance),
            )
        return self

    @property
    def total_weight(self) -> Decimal:
        return sum((w.weight for w in self.weights), Decimal("0"))

    def as_dict(self) -> dict[str, Decimal]:
        return {w.symbol: w.weight for w in self.weights}
