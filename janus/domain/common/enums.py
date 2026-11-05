"""Enumerations shared across domain boundaries.

Keeping these in one module avoids import cycles and guarantees that the
persistence layer and the API layer serialise the *same* vocabulary.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AccountState",
    "AdjustmentMode",
    "Board",
    "DataQualityStatus",
    "Exchange",
    "FeeComponent",
    "InstrumentType",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "OrderValidity",
    "QuoteSource",
    "RiskOutcome",
    "SecurityStatus",
    "SessionType",
    "SettlementRule",
    "TradingDayType",
]


class Exchange(StrEnum):
    """Supported venue identifiers."""

    SSE = "SSE"
    """Shanghai Stock Exchange."""

    SZSE = "SZSE"
    """Shenzhen Stock Exchange."""

    CASH = "CASH"
    """Pseudo-venue for the CNY cash line (``CASH.CNY``)."""


class Board(StrEnum):
    """Board / market segment. Only the Phase 0 eligible subset is supported."""

    SSE_MAIN = "SSE_MAIN"
    """Shanghai main board (A-shares and exchange-listed equity ETFs)."""

    SZSE_MAIN = "SZSE_MAIN"
    """Shenzhen main board (A-shares and exchange-listed equity ETFs)."""

    SZSE_CHINEXT = "SZSE_CHINEXT"
    """ChiNext -- excluded in Phase 0."""

    SSE_STAR = "SSE_STAR"
    """STAR Market -- excluded in Phase 0."""

    BSE = "BSE"
    """Beijing Stock Exchange -- excluded in Phase 0."""

    CASH = "CASH"
    """Pseudo board for the cash line."""


class InstrumentType(StrEnum):
    """Instrument taxonomy (Phase 0 subset plus the excluded families)."""

    A_SHARE = "A_SHARE"
    DOMESTIC_EQUITY_ETF = "DOMESTIC_EQUITY_ETF"
    CASH = "CASH"

    # Explicitly out of scope in Phase 0. Present so the universe validator can
    # name the exclusion instead of silently ignoring it.
    CROSS_BORDER_ETF = "CROSS_BORDER_ETF"
    BOND_ETF = "BOND_ETF"
    MONEY_MARKET_ETF = "MONEY_MARKET_ETF"
    GOLD_ETF = "GOLD_ETF"
    COMMODITY_ETF = "COMMODITY_ETF"
    LEVERAGED_ETF = "LEVERAGED_ETF"
    INVERSE_ETF = "INVERSE_ETF"
    CONVERTIBLE_BOND = "CONVERTIBLE_BOND"
    OPTION = "OPTION"
    FUTURE = "FUTURE"

    @property
    def is_supported(self) -> bool:
        """True when Phase 0 may trade this instrument type."""
        return self in {
            InstrumentType.A_SHARE,
            InstrumentType.DOMESTIC_EQUITY_ETF,
            InstrumentType.CASH,
        }


class SecurityStatus(StrEnum):
    """Trading status of a listed security."""

    ACTIVE = "ACTIVE"
    ST = "ST"
    STAR_ST = "STAR_ST"
    """``*ST`` delisting-risk warning."""

    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    PENDING_LISTING = "PENDING_LISTING"

    @property
    def is_risk_warning(self) -> bool:
        return self in {SecurityStatus.ST, SecurityStatus.STAR_ST}

    @property
    def blocks_new_buys(self) -> bool:
        return self is not SecurityStatus.ACTIVE


class SettlementRule(StrEnum):
    """Resale/settlement treatment for a purchased position."""

    T_PLUS_1 = "T_PLUS_1"
    """Shares bought on day T become sellable on the next trading day."""

    T_PLUS_0 = "T_PLUS_0"
    """Same-day resale permitted (apply only where a rule explicitly allows it)."""


class SessionType(StrEnum):
    """Market session phases that JANUS models explicitly."""

    PRE_OPEN_AUCTION = "PRE_OPEN_AUCTION"
    CONTINUOUS = "CONTINUOUS"
    CLOSING_AUCTION = "CLOSING_AUCTION"
    AFTER_HOURS_FIXED_PRICE = "AFTER_HOURS_FIXED_PRICE"
    CLOSED = "CLOSED"


class TradingDayType(StrEnum):
    """Calendar day classification."""

    TRADING = "TRADING"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"

    @property
    def is_supported(self) -> bool:
        """Phase 0 simulates limit orders only (market orders need a book)."""
        return self is OrderType.LIMIT


class OrderValidity(StrEnum):
    """Order validity window/type."""

    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    """Paper order lifecycle (Phase 0 spec s6.4)."""

    PROPOSED = "PROPOSED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED_PAPER = "SUBMITTED_PAPER"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.RISK_REJECTED,
            OrderStatus.FILLED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELLED,
        }

    @property
    def is_live(self) -> bool:
        """An order that still holds cash/position reservations."""
        return self in {
            OrderStatus.RISK_APPROVED,
            OrderStatus.SUBMITTED_PAPER,
            OrderStatus.PARTIALLY_FILLED,
        }


class RiskOutcome(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"


class AccountState(StrEnum):
    """Account lifecycle. Phase 0 has no live state by design."""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"
    RESET = "RESET"


class DataQualityStatus(StrEnum):
    PENDING = "PENDING"
    OK = "OK"
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"


class AdjustmentMode(StrEnum):
    """Price-adjustment convention attached to a market bar."""

    UNADJUSTED = "UNADJUSTED"
    """Raw traded price. The only mode allowed for simulated execution."""

    FORWARD_ADJUSTED = "FORWARD_ADJUSTED"
    BACKWARD_ADJUSTED = "BACKWARD_ADJUSTED"


class QuoteSource(StrEnum):
    FIXTURE = "FIXTURE"
    TUSHARE = "TUSHARE"
    AKSHARE = "AKSHARE"
    EXCHANGE = "EXCHANGE"


class FeeComponent(StrEnum):
    """Separately represented cost components (Phase 0 spec s6.3)."""

    BROKER_COMMISSION = "BROKER_COMMISSION"
    MIN_COMMISSION = "MIN_COMMISSION"
    STAMP_DUTY = "STAMP_DUTY"
    TRANSFER_FEE = "TRANSFER_FEE"
    EXCHANGE_HANDLING_FEE = "EXCHANGE_HANDLING_FEE"
    REGULATORY_FEE = "REGULATORY_FEE"
    SLIPPAGE = "SLIPPAGE"
