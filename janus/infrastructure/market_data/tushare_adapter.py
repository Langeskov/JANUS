"""Tushare adapter shell (Phase 1 work).

Phase 0 spec s11 P0-F: define the interface and configuration contract; actual
live integration belongs to Phase 1, where data permissions/points cost is an
explicit decision.

The adapter deliberately does not import the ``tushare`` package. Constructing it
before Phase 1 is a configuration error, not a silent no-op, so a misconfigured
deployment cannot believe it is receiving real data.

Its capability declaration is intentionally empty: the shell supplies nothing, so
claiming any dataset would be a lie. ``capabilities()`` is a classmethod because
a disabled adapter cannot be constructed, while the (empty) declaration still has
to be auditable and registerable (PR-001 §5).
"""

from __future__ import annotations

from datetime import date

from janus.application.ports.market_data import ProviderHealth
from janus.domain.common.errors import UnsupportedEntityError
from janus.domain.common.time import utc_now
from janus.domain.data_foundation.capabilities import ProviderCapabilities
from janus.domain.market_data.models import MarketBar
from janus.infrastructure.market_data.capability_gate import PHASE0_DECLARATION_AT

__all__ = ["TushareProvider"]

PROVIDER_ID = "tushare"

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
        return PROVIDER_ID

    @classmethod
    def capabilities(cls) -> ProviderCapabilities:
        """Declare an empty capability set: this shell supplies no data.

        Never over-claim. In Phase 1, once the Tushare adapter actually fetches
        daily data, this declaration gains ``DAILY_BARS``; minute data, news and
        announcements require separate Tushare permissions and remain undeclared
        until those are explicitly enabled and tested.
        """
        return ProviderCapabilities(
            provider_id=PROVIDER_ID,
            supported=frozenset(),
            declared_at=PHASE0_DECLARATION_AT,
            declaration_reason="disabled Phase 0 shell; no data is supplied",
            notes=(
                "Phase 1 only. Supplying daily bars requires Tushare points; "
                "minute data, news sentiment and announcements need separate "
                "permissions. The adapter is disabled in Phase 0 and declares no "
                "capabilities rather than pretending to have data."
            ),
        )

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
