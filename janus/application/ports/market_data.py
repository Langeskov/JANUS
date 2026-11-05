"""Market-data and trading-calendar provider ports.

Phase 0 ships a deterministic :class:`~janus.Application` fixture provider and
adapter shells only (spec s7.1, s11 P0-F). Live provider integration is Phase 1.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from janus.domain.common.time import ensure_aware
from janus.domain.market_data.models import MarketBar
from janus.domain.market_rules.calendar import TradingCalendar

__all__ = [
    "MarketDataProvider",
    "ProviderHealth",
    "TradingCalendarProvider",
]


class ProviderHealth(BaseModel):
    """Health probe result. Never contains credentials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    ok: bool
    detail: str = ""
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "checked_at")


@runtime_checkable
class MarketDataProvider(Protocol):
    """Provider-neutral daily market-data port.

    Adapters normalise provider field names and symbol formats to the canonical
    JANUS schema before returning anything (spec s7.1).
    """

    @property
    def provider_id(self) -> str: ...

    def get_daily_bar(self, symbol: str, trade_date: date) -> MarketBar | None:
        """One bar, or None when the provider has no row."""
        ...

    def get_daily_bars(self, symbol: str, start: date, end: date) -> tuple[MarketBar, ...]:
        """Bars in the inclusive range, ordered by trade date."""
        ...

    def list_symbols(self) -> tuple[str, ...]: ...

    def health(self) -> ProviderHealth: ...

    def close(self) -> None: ...


@runtime_checkable
class TradingCalendarProvider(Protocol):
    """Supplies the effective-dated trading calendar."""

    def get_calendar(self) -> TradingCalendar: ...
