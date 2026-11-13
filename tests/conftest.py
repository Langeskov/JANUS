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
from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPolicy,
    AvailabilityPrecision,
)
from janus.domain.data_foundation.provenance import (
    DataProvenance,
    ProvenanceTimestamps,
)
from janus.domain.data_foundation.query import LoadedRevision
from janus.domain.data_foundation.revisions import DataRevision, DataValue
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


# ---------------------------------------------------------------------------
# Data-foundation factories (PR-001)
#
# Pure domain construction helpers: they build provenance/revision records with
# a controllable availability timeline so point-in-time semantics can be tested
# without a database. Import them explicitly in tests.
# ---------------------------------------------------------------------------

HASH_A = "a" * 64
"""A syntactically valid SHA-256 hex digest used where content is irrelevant."""

HASH_B = "b" * 64


def timestamps(
    *,
    event_at: datetime,
    retrieved_at: datetime | None = None,
    published_at: datetime | None = None,
    ingested_at: datetime | None = None,
    first_observed_at: datetime | None = None,
) -> ProvenanceTimestamps:
    retrieved = retrieved_at or event_at
    return ProvenanceTimestamps(
        event_at=event_at,
        published_at=published_at,
        retrieved_at=retrieved,
        ingested_at=ingested_at or retrieved,
        system_first_observed_at=first_observed_at or retrieved,
    )


def provenance(
    *,
    public_available_at: datetime,
    event_at: datetime | None = None,
    published_at: datetime | None = None,
    retrieved_at: datetime | None = None,
    first_observed_at: datetime | None = None,
    precision: AvailabilityPrecision = AvailabilityPrecision.INSTANT,
    basis: AvailabilityBasis = AvailabilityBasis.SOURCE_TIMESTAMP,
    payload_hash: str = HASH_A,
    source_revision: str = "r1",
    record_id: str = "rec-1",
    dataset: str = "financials",
    source_id: str = "fixture",
    policy: AvailabilityPolicy | None = None,
) -> DataProvenance:
    event = event_at or public_available_at
    return DataProvenance.build(
        source_id=source_id,
        dataset=dataset,
        source_record_id=record_id,
        timestamps=timestamps(
            event_at=event,
            retrieved_at=retrieved_at or event,
            published_at=published_at,
            first_observed_at=first_observed_at,
        ),
        availability_basis=basis,
        availability_precision=precision,
        public_available_at=public_available_at,
        payload_hash=payload_hash,
        source_revision=source_revision,
        policy=policy or AvailabilityPolicy(),
    )


def revision(
    *,
    provenance_record: DataProvenance,
    sequence: int = 1,
    logical_key: str = "fin:600519.SH:revenue:2026Q3",
    value: DataValue | None = None,
    subject_symbol: str | None = "600519.SH",
    period_end: date | None = None,
    dataset: str | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> DataRevision:
    return DataRevision.build(
        logical_key=logical_key,
        dataset=dataset or provenance_record.dataset,
        source_id=provenance_record.source_id,
        revision_sequence=sequence,
        value=value or DataValue.decimal_value(Decimal("10000000000"), unit="CNY"),
        provenance_id=provenance_record.provenance_id,
        payload_hash=provenance_record.payload_hash,
        source_revision=provenance_record.source_revision,
        ingested_at=provenance_record.timestamps.ingested_at,
        subject_symbol=subject_symbol,
        period_end=period_end,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def loaded(*, revision_record: DataRevision, provenance_record: DataProvenance) -> LoadedRevision:
    return LoadedRevision(revision=revision_record, provenance=provenance_record)
