"""Market-data adapters, provider capability declarations and the deterministic
fixture provider.

Every adapter declares what it can supply (:meth:`capabilities`) and callers ask
for a capability through :func:`require_capability`; an unsupported dataset fails
closed instead of returning an empty successful result (PR-001 §5).
"""

from __future__ import annotations

from janus.infrastructure.market_data.akshare_adapter import AKShareProvider
from janus.infrastructure.market_data.capability_gate import (
    PHASE0_DECLARATION_AT,
    require_capability,
)
from janus.infrastructure.market_data.fixture_provider import (
    FixtureProvider,
    load_instruments,
    load_strategies,
)
from janus.infrastructure.market_data.tushare_adapter import TushareProvider

__all__ = [
    "PHASE0_DECLARATION_AT",
    "AKShareProvider",
    "FixtureProvider",
    "TushareProvider",
    "load_instruments",
    "load_strategies",
    "require_capability",
]
