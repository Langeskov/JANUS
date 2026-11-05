"""Universe eligibility policy and evaluation.

Phase 0 spec s2.1/s13: exclude BSE, ChiNext, STAR, cross-border/commodity/gold/
bond/money-market/leveraged/inverse ETFs, convertible bonds, options, futures,
ST/*ST, suspended and delisted securities, and IPOs inside a configurable
seasoning window -- from **new buys**. Historical data is retained regardless.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from janus.domain.common.enums import (
    Board,
    InstrumentType,
    SecurityStatus,
)
from janus.domain.common.errors import ValidationError
from janus.domain.instruments.models import Instrument

__all__ = [
    "EligibilityPolicy",
    "EligibilityResult",
    "EligibilityViolation",
    "UniversePolicy",
    "evaluate_eligibility",
]

_SUFFIX_TO_EXCHANGE: dict[str, str] = {"SH": "SSE", "SZ": "SZSE", "CNY": "CASH"}
"""Canonical symbol suffixes mapped to venue identifiers.

Configuration uses exchange names (``SSE``/``SZSE``) while instrument symbols use
venue suffixes (``.SH``/``.SZ``). Both spellings are accepted so that one policy
document cannot silently reject the whole universe.
"""


class EligibilityViolation(StrEnum):
    """Stable rule identifiers for eligibility rejections (audit-friendly)."""

    UNSUPPORTED_BOARD = "UNSUPPORTED_BOARD"
    UNSUPPORTED_INSTRUMENT_TYPE = "UNSUPPORTED_INSTRUMENT_TYPE"
    DELISTED = "DELISTED"
    PENDING_LISTING = "PENDING_LISTING"
    NOT_YET_LISTED = "NOT_YET_LISTED"
    SUSPENDED = "SUSPENDED"
    RISK_WARNING = "RISK_WARNING"
    SEASONING_WINDOW = "SEASONING_WINDOW"
    UNKNOWN_STATUS = "UNKNOWN_STATUS"


class UniversePolicy(BaseModel):
    """Declarative universe policy, loaded from ``config/universe.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exchanges: tuple[str, ...] = ("SSE", "SZSE")
    boards_allowed: tuple[Board, ...] = (Board.SSE_MAIN, Board.SZSE_MAIN)
    instrument_types_allowed: tuple[InstrumentType, ...] = (
        InstrumentType.A_SHARE,
        InstrumentType.DOMESTIC_EQUITY_ETF,
    )

    exclude_st: bool = True
    exclude_suspended_for_new_orders: bool = True
    exclude_delisted: bool = True
    min_listing_trading_days_for_buy: int = Field(default=60, ge=0)

    def allows_board(self, board: Board) -> bool:
        return board in self.boards_allowed

    def allows_type(self, instrument_type: InstrumentType) -> bool:
        return instrument_type in self.instrument_types_allowed

    def allows_exchange_suffix(self, suffix: str) -> bool:
        """True when the venue of ``suffix`` is allowed by this policy.

        Accepts either canonical symbol suffixes (``SH``/``SZ``) or venue names
        (``SSE``/``SZSE``) because configuration and symbols use different
        spellings for the same venue.
        """
        normalised = suffix.strip().upper()
        if normalised in self.exchanges:
            return True
        exchange = _SUFFIX_TO_EXCHANGE.get(normalised)
        return exchange is not None and exchange in self.exchanges


class EligibilityResult(BaseModel):
    """Outcome of evaluating one instrument against the universe policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    as_of: date
    eligible: bool
    violations: tuple[EligibilityViolation, ...] = ()

    def rejection_reasons(self) -> list[str]:
        return [v.value for v in self.violations]


class EligibilityPolicy:
    """Stateless evaluator. Kept as a class for a stable call signature."""

    def __init__(self, policy: UniversePolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> UniversePolicy:
        return self._policy

    def evaluate(
        self,
        instrument: Instrument,
        as_of: date,
        *,
        listed_trading_days: int | None = None,
        suspended: bool | None = None,
    ) -> EligibilityResult:
        """Evaluate an instrument for a *new buy* on ``as_of``.

        ``listed_trading_days`` is supplied by the caller (computed from the
        trading calendar). ``suspended`` overrides the static status when a
        recent suspension fact exists; it never *relaxes* the status check.
        """
        violations: list[EligibilityViolation] = []
        policy = self._policy

        if not policy.allows_board(instrument.board):
            violations.append(EligibilityViolation.UNSUPPORTED_BOARD)
        if not policy.allows_type(instrument.instrument_type):
            violations.append(EligibilityViolation.UNSUPPORTED_INSTRUMENT_TYPE)
        if not policy.allows_exchange_suffix(instrument.venue_suffix()):
            violations.append(EligibilityViolation.UNSUPPORTED_BOARD)

        status = instrument.status
        if status is SecurityStatus.DELISTED:
            if policy.exclude_delisted:
                violations.append(EligibilityViolation.DELISTED)
        elif status is SecurityStatus.PENDING_LISTING:
            violations.append(EligibilityViolation.PENDING_LISTING)
        elif status is SecurityStatus.SUSPENDED:
            if policy.exclude_suspended_for_new_orders or (suspended is True):
                violations.append(EligibilityViolation.SUSPENDED)
        elif status.is_risk_warning:
            if policy.exclude_st:
                violations.append(EligibilityViolation.RISK_WARNING)
        elif status is not SecurityStatus.ACTIVE:
            violations.append(EligibilityViolation.UNKNOWN_STATUS)

        if suspended is True and EligibilityViolation.SUSPENDED not in violations:
            violations.append(EligibilityViolation.SUSPENDED)

        if instrument.listing_date is None:
            if status is not SecurityStatus.PENDING_LISTING:
                violations.append(EligibilityViolation.NOT_YET_LISTED)
        elif as_of < instrument.listing_date:
            violations.append(EligibilityViolation.NOT_YET_LISTED)

        min_days = policy.min_listing_trading_days_for_buy
        if min_days > 0:
            if listed_trading_days is None:
                # Fail closed: cannot prove the seasoning window was satisfied.
                violations.append(EligibilityViolation.SEASONING_WINDOW)
            elif listed_trading_days < min_days:
                violations.append(EligibilityViolation.SEASONING_WINDOW)

        deduped = tuple(dict.fromkeys(violations))
        return EligibilityResult(
            symbol=instrument.symbol,
            as_of=as_of,
            eligible=not deduped,
            violations=deduped,
        )


def evaluate_eligibility(
    instrument: Instrument,
    policy: UniversePolicy,
    as_of: date,
    *,
    listed_trading_days: int | None = None,
    suspended: bool | None = None,
) -> EligibilityResult:
    """Module-level convenience wrapper around :class:`EligibilityPolicy`."""
    if not isinstance(as_of, date):  # pragma: no cover - defensive
        raise ValidationError("as_of must be a date")
    return EligibilityPolicy(policy).evaluate(
        instrument, as_of, listed_trading_days=listed_trading_days, suspended=suspended
    )
