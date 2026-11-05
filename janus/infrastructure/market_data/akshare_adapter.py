"""AKShare adapter shell (optional cross-check source; Phase 1 work).

AKShare aggregates public sources. Its own documentation flags research/
academic use and commercial-usage risk, so Phase 0 must never treat it as a
guaranteed commercial feed (Phase 0 spec s7.1). The adapter is a configured but
disabled shell and does not import the ``akshare`` package.
"""

from __future__ import annotations

from datetime import date

from janus.application.ports.market_data import ProviderHealth
from janus.domain.common.errors import UnsupportedEntityError
from janus.domain.common.time import utc_now
from janus.domain.market_data.models import MarketBar

__all__ = ["AKShareProvider"]


class AKShareProvider:
    """Configured-but-disabled AKShare adapter shell."""

    __slots__ = ()

    def __init__(self, *, mode: str = "research_only", enabled: bool = False) -> None:
        if not enabled:
            raise UnsupportedEntityError(
                "the AKShare data adapter is disabled in Phase 0; it is a "
                "research-only cross-check source, not a commercial feed",
                provider="akshare",
                mode=mode,
            )

    @property
    def provider_id(self) -> str:
        return "akshare"

    def get_daily_bar(self, symbol: str, trade_date: date) -> MarketBar | None:
        raise UnsupportedEntityError("AKShare integration is not implemented in Phase 0")

    def get_daily_bars(self, symbol: str, start: date, end: date) -> tuple[MarketBar, ...]:
        raise UnsupportedEntityError("AKShare integration is not implemented in Phase 0")

    def list_symbols(self) -> tuple[str, ...]:
        raise UnsupportedEntityError("AKShare integration is not implemented in Phase 0")

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            ok=False,
            detail="disabled in Phase 0 (research-only cross-check)",
            checked_at=utc_now(),
        )

    def close(self) -> None:
        return None
