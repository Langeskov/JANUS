"""Canonical symbol handling.

JANUS stores every security under one canonical, provider-neutral symbol form:

    ``<CODE>.<SUFFIX>``   e.g. ``600519.SH``, ``000001.SZ``, ``510300.SH``

Suffixes: ``SH`` (Shanghai Stock Exchange), ``SZ`` (Shenzhen Stock Exchange),
``CNY`` (the cash line, ``CASH.CNY``).

Providers use many different conventions (``600519.XSHG``, ``sz000001``,
``600519``). Normalisation to this canonical form happens in the infrastructure
adapter, never in strategy or rule code (Phase 0 spec s7.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from janus.domain.common.enums import Board, Exchange, InstrumentType
from janus.domain.common.errors import UnsupportedEntityError, ValidationError

CASH_SYMBOL: Final[str] = "CASH.CNY"
"""Canonical symbol of the CNY cash line."""

CASH_CODE: Final[str] = "CASH"

_SUFFIX_TO_EXCHANGE: Final[dict[str, Exchange]] = {
    "SH": Exchange.SSE,
    "SZ": Exchange.SZSE,
    "CNY": Exchange.CASH,
}

_EXCHANGE_TO_SUFFIX: Final[dict[Exchange, str]] = {
    Exchange.SSE: "SH",
    Exchange.SZSE: "SZ",
    Exchange.CASH: "CNY",
}

_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<code>[A-Z0-9]{3,10})\.(?P<suffix>[A-Z]{2,3})$"
)
_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{6}$")

EXCHANGE_TO_SUFFIX: Final[dict[Exchange, str]] = _EXCHANGE_TO_SUFFIX
"""Public mapping from venue identifier to canonical symbol suffix."""

__all__ = [
    "CASH_CODE",
    "CASH_SYMBOL",
    "EXCHANGE_TO_SUFFIX",
    "ParsedSymbol",
    "exchange_of",
    "is_cash",
    "make_symbol",
    "parse_symbol",
    "validate_symbol",
]


def parse_symbol(value: str) -> ParsedSymbol:
    """Parse and validate a canonical symbol string."""
    if not isinstance(value, str):
        raise ValidationError("symbol must be a string", symbol=repr(value))
    candidate = value.strip().upper()
    match = _SYMBOL_RE.match(candidate)
    if match is None:
        raise ValidationError(
            "symbol must look like '<CODE>.<SUFFIX>' (e.g. 600519.SH)", symbol=value
        )
    code = match.group("code")
    suffix = match.group("suffix")
    exchange = _SUFFIX_TO_EXCHANGE.get(suffix)
    if exchange is None:
        raise UnsupportedEntityError(
            f"unsupported symbol suffix {suffix!r}; Phase 0 supports SH, SZ, CNY",
            symbol=value,
        )
    if exchange is Exchange.CASH:
        if code != CASH_CODE:
            raise ValidationError("the only CASH symbol is CASH.CNY", symbol=value)
    elif not _CODE_RE.match(code):
        raise ValidationError("equity/ETF symbols require a 6-digit numeric code", symbol=value)
    return ParsedSymbol(code=code, suffix=suffix, exchange=exchange, canonical=f"{code}.{suffix}")


def validate_symbol(value: str) -> str:
    """Return the canonical form of ``value`` or raise."""
    return parse_symbol(value).canonical


def make_symbol(code: str, exchange: Exchange) -> str:
    """Build a canonical symbol from a code and venue."""
    if exchange is Exchange.CASH:
        return CASH_SYMBOL
    suffix = _EXCHANGE_TO_SUFFIX.get(exchange)
    if suffix is None:  # pragma: no cover - defensive
        raise UnsupportedEntityError(f"unsupported exchange {exchange!r}", exchange=str(exchange))
    return validate_symbol(f"{code}.{suffix}")


def exchange_of(value: str) -> Exchange:
    """Venue of a canonical symbol."""
    return parse_symbol(value).exchange


def is_cash(value: str) -> bool:
    """True for the ``CASH.CNY`` cash line."""
    return value.strip().upper() == CASH_SYMBOL


@dataclass(frozen=True, slots=True)
class ParsedSymbol:
    """Decomposed canonical symbol."""

    code: str
    suffix: str
    exchange: Exchange
    canonical: str

    @property
    def is_cash(self) -> bool:
        return self.exchange is Exchange.CASH

    def __str__(self) -> str:
        return self.canonical


class _Suffix(StrEnum):  # pragma: no cover - documentation helper only
    SH = "SH"
    SZ = "SZ"
    CNY = "CNY"


def board_for(code: str, exchange: Exchange) -> Board:
    """Heuristically classify the board of a code.

    This is *seed/classification* logic used to build fixture universes. The
    authoritative board for a tradable instrument is the persisted
    :class:`~janus.domain.instruments.models.Instrument` attribute -- never
    recomputed from the code inside the rules engine.
    """
    if exchange is Exchange.CASH:
        return Board.CASH
    if exchange is Exchange.SSE:
        if code.startswith(("688", "689")):
            return Board.SSE_STAR
        return Board.SSE_MAIN
    if code.startswith(("300", "301", "302")):
        return Board.SZSE_CHINEXT
    return Board.SZSE_MAIN


# Product blocks that Phase 0 explicitly excludes. Matching here is conservative:
# a code in a known excluded block keeps its true product family so that the
# universe policy rejects it with a precise reason instead of guessing.
_SSE_ETF_PREFIXES: Final[tuple[str, ...]] = (
    "510",
    "511",
    "512",
    "513",
    "515",
    "516",
    "517",
    "518",
    "520",
    "560",
    "561",
    "562",
    "563",
    "588",
)


def instrument_type_for(code: str, exchange: Exchange) -> InstrumentType:
    """Heuristically classify instrument type for seed data only.

    The classification of *excluded* product families is deliberate: if a code
    sits in a product block Phase 0 does not support (bond/gold/cross-border/
    money-market ETFs, STAR board ETFs, ChiNext stocks) it is labelled with that
    family so the universe policy rejects it rather than guessing.
    """
    if exchange is Exchange.CASH:
        return InstrumentType.CASH
    if exchange is Exchange.SSE:
        if code.startswith(_SSE_ETF_PREFIXES):
            if code.startswith("513") or code.startswith("520"):
                return InstrumentType.CROSS_BORDER_ETF
            if code.startswith("511"):
                return InstrumentType.BOND_ETF
            if code.startswith("518"):
                return InstrumentType.GOLD_ETF
            return InstrumentType.DOMESTIC_EQUITY_ETF
        return InstrumentType.A_SHARE
    if code.startswith("159"):
        return InstrumentType.DOMESTIC_EQUITY_ETF
    if code.startswith("15"):
        return InstrumentType.BOND_ETF
    return InstrumentType.A_SHARE
