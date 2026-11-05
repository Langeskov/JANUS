"""Deterministic monetary and quantity value objects.

Money is held as :class:`decimal.Decimal` -- never ``float`` -- because paper
accounting must reconcile exactly (Phase 0 spec s5.1, acceptance criterion 3).
Quantities are integral share/unit counts.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from janus.domain.common.errors import ValidationError

CNY: Final[str] = "CNY"
CASH_SYMBOL: Final[str] = "CASH.CNY"
"""Canonical pseudo-symbol that represents uninvested cash in a portfolio."""

CNY_QUANTUM: Final[Decimal] = Decimal("0.01")
"""CNY minor unit. All persisted money is rounded half-up to this quantum."""

ONE_HUNDRED: Final[Decimal] = Decimal("100")

__all__ = [
    "CASH_SYMBOL",
    "CNY",
    "CNY_QUANTUM",
    "Money",
    "as_decimal",
    "bps_to_rate",
    "quantize_cny",
    "weight_to_decimal",
]


def as_decimal(value: Any, field: str = "value") -> Decimal:
    """Coerce int/str/Decimal to Decimal without touching float precision."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValidationError(f"{field} must be a finite decimal", field=field)
        return value
    if isinstance(value, bool):
        raise ValidationError(f"{field} must not be a bool", field=field)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValidationError(f"{field} is not a decimal: {value!r}", field=field) from exc
        if not parsed.is_finite():
            raise ValidationError(f"{field} must be a finite decimal", field=field)
        return parsed
    raise ValidationError(
        f"{field} must be int/str/Decimal, got {type(value).__name__}", field=field
    )


def quantize_cny(value: Decimal, rounding: str = "ROUND_HALF_UP") -> Decimal:
    """Quantize a Decimal to the CNY minor unit deterministically."""
    return value.quantize(CNY_QUANTUM, rounding=rounding)


def bps_to_rate(basis_points: Decimal | int | str) -> Decimal:
    """Convert basis points to a rate (``25`` bps -> ``0.0025``)."""
    return as_decimal(basis_points, "basis_points") / Decimal(10_000)


def weight_to_decimal(value: Decimal | int | str, field: str = "weight") -> Decimal:
    """Parse and range-check a portfolio weight in ``[0, 1]``."""
    weight = as_decimal(value, field)
    if weight < 0 or weight > 1:
        raise ValidationError(f"{field} must be within [0, 1], got {weight}", field=field)
    return weight


class Money(BaseModel):
    """An exact CNY amount.

    ``Money`` allows negative amounts so that ledger deltas and P&L can be
    represented; individual domain objects decide whether negativity is legal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", ser_json_timedelta="float")

    amount: Decimal
    currency: str = CNY

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, value: object) -> Decimal:
        if isinstance(value, Money):  # pragma: no cover - defensive
            return value.amount
        return as_decimal(value, "amount")

    @field_validator("amount")
    @classmethod
    def _quantize(cls, value: Decimal) -> Decimal:
        return quantize_cny(value)

    @field_validator("currency")
    @classmethod
    def _cny_only(cls, value: str) -> str:
        if value != CNY:
            raise ValidationError("Phase 0 supports a CNY cash account only", currency=value)
        return value

    @model_validator(mode="after")
    def _finite(self) -> Self:
        return self

    # -- arithmetic -----------------------------------------------------
    def _assert_same_currency(self, other: Money) -> None:
        if other.currency != self.currency:
            raise ValidationError(
                "money arithmetic requires equal currencies",
                left=self.currency,
                right=other.currency,
            )

    def __add__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(amount=self.amount + other.amount)

    def __sub__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(amount=self.amount - other.amount)

    def __neg__(self) -> Money:
        return Money(amount=-self.amount)

    def __abs__(self) -> Money:
        return Money(amount=abs(self.amount))

    def scaled(self, factor: Decimal) -> Money:
        """Multiply by a dimensionless Decimal factor and re-quantize."""
        return Money(amount=self.amount * as_decimal(factor, "factor"))

    def __mul__(self, factor: Decimal) -> Money:
        return self.scaled(factor)

    def __truediv__(self, divisor: Decimal) -> Money:
        divisor = as_decimal(divisor, "divisor")
        if divisor == 0:
            raise ValidationError("division by zero")
        return Money(amount=self.amount / divisor)

    # -- predicates -----------------------------------------------------
    def is_zero(self) -> bool:
        return self.amount == 0

    def is_negative(self) -> bool:
        return self.amount < 0

    def is_positive(self) -> bool:
        return self.amount > 0

    def __lt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount >= other.amount

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.currency}"


ZERO_CNY: Final[Money] = Money(amount=Decimal("0"))
"""Convenient immutable zero amount."""
