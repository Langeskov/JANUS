"""Tushare adapter shell (Phase 1 work).

Phase 0 spec s11 P0-F: define the interface and configuration contract; actual
live integration belongs to Phase 1, where data permissions/points cost is an
explicit decision.

The adapter deliberately does not import the ``tushare`` package. Constructing it
before Phase 1 is a configuration error, not a silent no-op, so a misconfigured
deployment cannot believe it is receiving real data.
"""

from __future__ import annotations

from datetime import date

from janus.application.ports.market_data import ProviderHealth
from janus.domain.common.errors import UnsupportedEntityError
from janus.domain.common.time import utc_now
from janus.domain.market_data.models import MarketBar

__all__ = ["TushareProvider"]

_TOKEN_ENV = "TUSHARE_TOKEN"


class TushareProvider:
    """Configured-but-disabled Tushare adapter shell."""

    __slots__ = ("_token_env",)

    def __init__(self, *, token_env: str = _TOKEN_ENV, enabled: bool = False) -> None:
        if not enabled:
            raise UnsupportedEntityError(
                "the Tushare data adapter is disabled in Phase 0; "
                "live provider integration belongs to Phase 1",
                provider="tushare",
                token_env=token_env,
            )
        self._token_env = token_env

    @property
    def provider_id(self) -> str:
        return "tushare"

    def get_daily_bar(self, symbol: str, trade_date: date) -> MarketBar | None:
        raise UnsupportedEntityError("Tushare integration is not implemented in Phase 0")

    def get_daily_bars(self, symbol: str, start: date, end: date) -> tuple[MarketBar, ...]:
        raise UnsupportedEntityError("Tushare integration is not implemented in Phase 0")

    def list_symbols(self) -> tuple[str, ...]:
        raise UnsupportedEntityError("Tushare integration is not implemented in Phase 0")

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            ok=False,
            detail="disabled in Phase 0",
            checked_at=utc_now(),
        )

    def close(self) -> None:
        return None
