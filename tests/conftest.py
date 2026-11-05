"""Shared deterministic fixtures for JANUS tests.

Every test in Phase 0 runs fully offline against fixture data and a frozen clock.
No test may reach the network, a real brokerage, or the wall clock.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from janus.application.container import JanusContainer
from janus.application.ports.clock import FixedClock
from janus.domain.common.time import MARKET_TZ
from janus.domain.portfolio.models import TargetWeight
from janus.domain.strategy.models import RegimeClass
from janus.domain.strategy.proposal import AllocationProposal
from janus.infrastructure.app_container import build_container

SIGNAL_DAY = date(2026, 11, 5)
"""A calm trading day inside the registered 2026 Q4 calendar window."""

NEXT_DAY = date(2026, 11, 6)
"""The next trading day after :data:`SIGNAL_DAY`."""

SIGNAL_INSTANT = datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ)
"""15:30 Asia/Shanghai: after the close, once day-T data is known."""


def make_proposal(
    weights: dict[str, str],
    *,
    as_of: datetime = SIGNAL_INSTANT,
    strategy_id: str = "broad_etf_core",
    strategy_version: str = "0.1.0",
    horizon_days: int = 20,
    data_snapshot_id: str | None = None,
    thesis: str = "Phase 0 acceptance test allocation.",
    regime: RegimeClass = RegimeClass.RISK_ON,
) -> AllocationProposal:
    """Build a valid :class:`AllocationProposal` from ``{symbol: weight}``."""
    return AllocationProposal(
        as_of=as_of,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        regime=regime,
        target_weights=tuple(
            TargetWeight(symbol=symbol, weight=Decimal(weight))
            for symbol, weight in weights.items()
        ),
        horizon_days=horizon_days,
        thesis=thesis,
        invalidation_conditions=("Index trend breaks down",),
        evidence_refs=(),
        uncertainties=("Deterministic fixture data only",),
        data_snapshot_id=data_snapshot_id,
    )


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test independent of the developer/CI shell environment.

    Phase 0 must run entirely offline from fixtures: no ambient database URL and
    certainly no real credentials may influence a test.
    """
    for variable in (
        "JANUS_DB_URL",
        "TUSHARE_TOKEN",
        "DEEPSEEK_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
    ):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def clock() -> FixedClock:
    """Frozen clock at 2026-11-05T15:30 Asia/Shanghai."""
    return FixedClock(SIGNAL_INSTANT)


@pytest.fixture
def container(clock: FixedClock) -> JanusContainer:
    """A fully wired Phase 0 container on a throw-away in-memory SQLite database."""
    app = build_container(
        database_url="sqlite+pysqlite:///:memory:",
        apply_migrations=False,
        clock=clock,
    )
    app.initialize_account()
    app.ingest_fixture_data(days=6)
    return app
