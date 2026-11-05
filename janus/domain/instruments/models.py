"""Instrument master data.

An :class:`Instrument` holds *identity and lifecycle* facts only. Trading rules
(tick size, quantity increments, price limits, settlement) are deliberately NOT
stored here: they are resolved per trade date by the versioned
:class:`~janus.domain.market_rules.engine.MarketRulesEngine` from effective-dated
rule-sets (Phase 0 spec s2.2).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from janus.domain.common.enums import (
    Board,
    Exchange,
    InstrumentType,
    SecurityStatus,
)
from janus.domain.common.errors import ValidationError
from janus.domain.instruments.symbol import (
    CASH_SYMBOL,
    parse_symbol,
    validate_symbol,
)

__all__ = ["Instrument", "is_supported_instrument_type"]


class Instrument(BaseModel):
    """Immutable canonical security record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    """Canonical symbol, e.g. ``600519.SH``."""

    name: str
    exchange: Exchange
    board: Board
    instrument_type: InstrumentType
    status: SecurityStatus = SecurityStatus.ACTIVE

    listing_date: date | None = None
    delisting_date: date | None = None

    lot_quantity: int | None = None
    """Legacy/advisory lot size from the provider, if any.

    The *authoritative* quantity increment comes from the resolved rule-set.
    """

    is_etf: bool = False
    """Convenience flag; authoritative type is ``instrument_type``."""

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return validate_symbol(value)

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValidationError("instrument name must not be empty")
        if len(value) > 200:
            raise ValidationError("instrument name is unreasonably long")
        return value.strip()

    @field_validator("lot_quantity")
    @classmethod
    def _positive_lot(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValidationError("lot_quantity must be positive when present")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> Instrument:
        parsed = parse_symbol(self.symbol)
        if parsed.exchange is not self.exchange:
            raise ValidationError(
                "symbol suffix disagrees with exchange",
                symbol=self.symbol,
                exchange=self.exchange.value,
            )
        if self.exchange is Exchange.CASH:
            if self.instrument_type is not InstrumentType.CASH:
                raise ValidationError("cash symbol must have instrument_type CASH")
            if self.board is not Board.CASH:
                raise ValidationError("cash symbol must have board CASH")
            return self
        if self.instrument_type is InstrumentType.CASH:
            raise ValidationError("only CASH.CNY may have instrument_type CASH")
        if self.listing_date and self.delisting_date and self.delisting_date < self.listing_date:
            raise ValidationError("delisting_date precedes listing_date", symbol=self.symbol)
        if self.listing_date is None and self.status is not SecurityStatus.PENDING_LISTING:
            raise ValidationError("a listed instrument requires a listing_date", symbol=self.symbol)
        return self

    # -- convenience ----------------------------------------------------
    @property
    def is_cash(self) -> bool:
        return self.symbol == CASH_SYMBOL

    @property
    def is_risk_warning(self) -> bool:
        return self.status.is_risk_warning

    def is_listed_on(self, as_of: date) -> bool:
        """True when the instrument exists as a listed security on ``as_of``."""
        if self.status in {SecurityStatus.PENDING_LISTING, SecurityStatus.DELISTED}:
            return False
        if self.listing_date is not None and as_of < self.listing_date:
            return False
        return not (self.delisting_date is not None and as_of >= self.delisting_date)

    def venue_suffix(self) -> str:
        return validate_symbol(self.symbol).split(".")[1]

    @classmethod
    def cash(cls) -> Instrument:
        """The canonical CNY cash line pseudo-instrument."""
        return cls(
            symbol=CASH_SYMBOL,
            name="CNY Cash",
            exchange=Exchange.CASH,
            board=Board.CASH,
            instrument_type=InstrumentType.CASH,
            status=SecurityStatus.ACTIVE,
            is_etf=False,
        )


def is_supported_instrument_type(instrument_type: InstrumentType) -> bool:
    """Phase 0 tradable instrument-type guard."""
    return instrument_type.is_supported
