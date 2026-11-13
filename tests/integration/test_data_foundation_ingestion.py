"""End-to-end data-foundation ingestion and point-in-time queries.

Drives the real SQLite repositories through the application facade:

    register source -> declare capability -> ingest raw payload
        -> provenance + immutable revision -> ingestion report
        -> point-in-time query

All tests are offline and deterministic (fixed clock, fixture payloads).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pytest
from janus.application.dto.data_foundation import IngestBatch, IngestFact
from janus.application.ports.clock import FixedClock
from janus.application.services.data_foundation_facade import DataFoundation
from janus.domain.common.time import MARKET_TZ
from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPolicy,
    AvailabilityPrecision,
    DateOnlyAvailabilityMode,
)
from janus.domain.data_foundation.capabilities import (
    DataCapability,
    DataSourceKind,
    ProviderCapabilities,
)
from janus.domain.data_foundation.quality import QualityIssueCode, QualityState
from janus.domain.data_foundation.query import ObservationMode
from janus.domain.data_foundation.revisions import DataValue
from janus.infrastructure.database.engine import Database
from janus.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.phase0

DATASET = "financial_facts"
SOURCE = "fixture"
LOGICAL_KEY = "fin:600519.SH:revenue:2026Q3"


def _dt(day: int, hour: int = 15, minute: int = 30) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=MARKET_TZ)


@dataclass
class _Rig:
    database: Database
    uow_factory: SqlAlchemyUnitOfWorkFactory
    clock: FixedClock
    foundation: DataFoundation


@pytest.fixture
def rig() -> Iterator[_Rig]:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_all()
    clock = FixedClock(_dt(5, 16, 0))
    uow_factory = SqlAlchemyUnitOfWorkFactory(database)
    foundation = DataFoundation.from_uow_factory(uow_factory=uow_factory, clock=clock)
    foundation.sources.register(
        source_id=SOURCE,
        kind=DataSourceKind.LOCAL_FIXTURE,
        display_name="Deterministic fixtures",
    )
    foundation.capabilities.declare(
        provider_id="fixture",
        supported=[DataCapability.DAILY_BARS, DataCapability.FINANCIAL_REPORTS],
        reason="offline deterministic fixtures",
    )
    yield _Rig(database=database, uow_factory=uow_factory, clock=clock, foundation=foundation)
    database.drop_all()


def _fact(
    *,
    record_id: str = "rec-1",
    logical_key: str = LOGICAL_KEY,
    value: str = "10000000000",
    published_at: datetime | None = None,
    public_available_at: datetime | None = None,
    source_revision: str = "r1",
    precision: AvailabilityPrecision = AvailabilityPrecision.INSTANT,
    basis: AvailabilityBasis = AvailabilityBasis.SOURCE_TIMESTAMP,
    period_end: date | None = date(2026, 9, 30),
) -> IngestFact:
    available = public_available_at or published_at or _dt(5)
    return IngestFact(
        source_record_id=record_id,
        logical_key=logical_key,
        value=DataValue.decimal_value(Decimal(value), unit="CNY"),
        event_at=datetime(2026, 9, 30, 15, 30, tzinfo=MARKET_TZ),
        published_at=published_at or available,
        availability_basis=basis,
        availability_precision=precision,
        public_available_at=available,
        source_revision=source_revision,
        subject_symbol="600519.SH",
        period_end=period_end,
    )


def _batch(
    facts: tuple[IngestFact, ...],
    *,
    payload: dict[str, object] | None = None,
    retrieved_at: datetime | None = None,
    provider_id: str | None = "fixture",
    required_capability: DataCapability | None = DataCapability.FINANCIAL_REPORTS,
) -> IngestBatch:
    body = payload if payload is not None else {"facts": [f.source_record_id for f in facts]}
    return IngestBatch(
        source_id=SOURCE,
        dataset=DATASET,
        payload_text=json.dumps(body, sort_keys=True),
        retrieved_at=retrieved_at or _dt(5),
        facts=facts,
        provider_id=provider_id,
        required_capability=required_capability,
        request_scope={"dataset": DATASET, "symbol": "600519.SH"},
    )


def _revisions(rig: _Rig, logical_key: str = LOGICAL_KEY) -> list[object]:
    with rig.uow_factory() as uow:
        return list(uow.data_revisions.list_for_logical_key(logical_key))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_ingest_archives_payload_and_creates_revision(rig: _Rig) -> None:
    outcome = rig.foundation.ingestion.ingest(_batch((_fact(),)))

    assert outcome.report.status is QualityState.ACCEPTED
    assert outcome.report.accepted_rows == 1
    assert outcome.report.rejected_rows == 0
    assert len(outcome.revisions) == 1
    assert outcome.payload is not None
    assert outcome.payload.content_hash == outcome.report.payload_hash

    with rig.uow_factory() as uow:
        stored = uow.raw_payloads.get_by_content_hash(outcome.payload.content_hash)
        assert stored is not None
        assert stored.payload_text.startswith("{")
        revision = uow.data_revisions.get(outcome.revisions[0].revision_id)
        assert revision is not None
        assert revision.value.as_decimal() == Decimal("10000000000")

    # Provenance resolves to its archived evidence.
    resolution = rig.foundation.provenance.resolve_for_revision(outcome.revisions[0])
    assert resolution.source_registered is True
    assert resolution.payload.content_hash == revision.payload_hash

    # And it is visible exactly from its strategy-usable instant.
    visible = rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(5, 16, 0))
    assert visible is not None
    assert visible.revision_id == revision.revision_id


def test_repeated_identical_ingestion_does_not_duplicate_facts(rig: _Rig) -> None:
    batch = _batch((_fact(),))
    first = rig.foundation.ingestion.ingest(batch)
    rig.clock.advance(minutes=1)
    second = rig.foundation.ingestion.ingest(batch)

    assert first.report.duplicate_rows == 0
    assert second.report.duplicate_rows == 1
    assert second.report.accepted_rows == 0
    assert second.report.status is QualityState.WARNING
    assert len(_revisions(rig)) == 1

    with rig.uow_factory() as uow:
        payloads = uow.raw_payloads.list_by_dataset(DATASET)
        runs = uow.ingestion_runs.list_recent(dataset=DATASET)
    assert len(payloads) == 1, "identical content must be archived once"
    assert len(runs) == 2, "both ingestion attempts must remain auditable"


def test_restatement_creates_a_new_revision_and_preserves_history(rig: _Rig) -> None:
    rig.foundation.ingestion.ingest(_batch((_fact(value="10000000000"),)))

    rig.clock.advance(days=5)  # 2026-11-10
    restatement = _fact(
        record_id="rec-1-r2",
        value="9800000000",
        published_at=_dt(10),
        public_available_at=_dt(10),
        source_revision="r2",
    )
    rig.foundation.ingestion.ingest(
        _batch((restatement,), payload={"facts": ["r2"]}, retrieved_at=_dt(10))
    )

    history = _revisions(rig)
    assert len(history) == 2, "the superseded revision must never be deleted"

    as_of_seventh = rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(7))
    as_of_eleventh = rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(11))
    assert as_of_seventh is not None
    assert as_of_eleventh is not None
    assert as_of_seventh.revision_sequence == 1
    assert as_of_seventh.value.as_decimal() == Decimal("10000000000")
    assert as_of_eleventh.revision_sequence == 2
    assert as_of_eleventh.value.as_decimal() == Decimal("9800000000")


# ---------------------------------------------------------------------------
# Fail-closed ingestion
# ---------------------------------------------------------------------------
def test_unknown_source_is_rejected(rig: _Rig) -> None:
    batch = _batch((_fact(),))
    unknown = IngestBatch(
        source_id="never-registered",
        dataset=DATASET,
        payload_text=batch.payload_text,
        retrieved_at=batch.retrieved_at,
        facts=batch.facts,
        provider_id=None,
        required_capability=None,
    )
    outcome = rig.foundation.ingestion.ingest(unknown)

    assert outcome.report.status is QualityState.REJECTED
    assert QualityIssueCode.UNKNOWN_SOURCE_IDENTITY in outcome.report.issue_codes()
    assert outcome.report.rejected_rows == 1
    assert _revisions(rig) == []
    with rig.uow_factory() as uow:
        assert uow.raw_payloads.list_by_dataset(DATASET) == []


def test_unsupported_capability_is_rejected(rig: _Rig) -> None:
    rig.foundation.capabilities.declare(
        provider_id="fixture", supported=[DataCapability.DAILY_BARS]
    )
    batch = _batch((_fact(),), required_capability=DataCapability.ANNOUNCEMENTS)
    outcome = rig.foundation.ingestion.ingest(batch)

    assert outcome.report.status is QualityState.REJECTED
    assert QualityIssueCode.UNSUPPORTED_CAPABILITY in outcome.report.issue_codes()
    assert _revisions(rig) == []


def test_provider_without_any_declaration_is_rejected(rig: _Rig) -> None:
    batch = _batch((_fact(),), provider_id="unregistered-provider")
    outcome = rig.foundation.ingestion.ingest(batch)
    assert outcome.report.status is QualityState.REJECTED
    assert QualityIssueCode.UNSUPPORTED_CAPABILITY in outcome.report.issue_codes()


def test_credential_material_is_refused_and_not_archived(rig: _Rig) -> None:
    batch = IngestBatch(
        source_id=SOURCE,
        dataset=DATASET,
        payload_text='{"api_key": "sk-live-abcdefgh12345678"}',
        retrieved_at=_dt(5),
        facts=(_fact(),),
        provider_id="fixture",
        required_capability=DataCapability.FINANCIAL_REPORTS,
    )
    outcome = rig.foundation.ingestion.ingest(batch)

    assert outcome.report.status is QualityState.REJECTED
    assert QualityIssueCode.CREDENTIAL_MATERIAL_DETECTED in outcome.report.issue_codes()
    assert outcome.payload is None
    with rig.uow_factory() as uow:
        assert uow.raw_payloads.list_by_dataset(DATASET) == []
    assert _revisions(rig) == []


def test_conflicting_source_revision_is_rejected(rig: _Rig) -> None:
    rig.foundation.ingestion.ingest(_batch((_fact(value="10000000000"),)))

    rig.clock.advance(days=1)
    conflicting = _fact(record_id="rec-conflict", value="42")
    outcome = rig.foundation.ingestion.ingest(
        _batch((conflicting,), payload={"facts": ["conflict"]}, retrieved_at=_dt(6))
    )

    assert outcome.report.rejected_rows == 1
    assert QualityIssueCode.CONFLICTING_REVISIONS in outcome.report.issue_codes()
    assert len(_revisions(rig)) == 1


def test_malformed_json_payload_is_rejected(rig: _Rig) -> None:
    batch = IngestBatch(
        source_id=SOURCE,
        dataset=DATASET,
        payload_text="{not valid json",
        retrieved_at=_dt(5),
        facts=(_fact(),),
        provider_id="fixture",
        required_capability=DataCapability.FINANCIAL_REPORTS,
    )
    outcome = rig.foundation.ingestion.ingest(batch)
    assert outcome.report.status is QualityState.REJECTED
    assert QualityIssueCode.MALFORMED_PAYLOAD in outcome.report.issue_codes()


# ---------------------------------------------------------------------------
# Availability edge cases end-to-end
# ---------------------------------------------------------------------------
def test_unknown_availability_is_incomplete_and_invisible() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_all()
    clock = FixedClock(_dt(5, 16, 0))
    uow_factory = SqlAlchemyUnitOfWorkFactory(database)
    foundation = DataFoundation.from_uow_factory(
        uow_factory=uow_factory,
        clock=clock,
        policy=AvailabilityPolicy(date_only_mode=DateOnlyAvailabilityMode.STRICT_UNKNOWN),
    )
    foundation.sources.register(
        source_id=SOURCE, kind=DataSourceKind.LOCAL_FIXTURE, display_name="fixtures"
    )

    date_only = _fact(
        published_at=_dt(5, 0, 0),
        public_available_at=_dt(5, 0, 0),
        precision=AvailabilityPrecision.DATE,
        basis=AvailabilityBasis.SOURCE_PUBLICATION_DATE,
    )
    batch = _batch((date_only,), provider_id=None, required_capability=None)
    outcome = foundation.ingestion.ingest(batch)

    assert outcome.report.status is QualityState.INCOMPLETE
    assert outcome.report.incomplete_rows == 1
    assert QualityIssueCode.UNKNOWN_AVAILABILITY in outcome.report.issue_codes()
    assert len(outcome.revisions) == 1, "the fact is stored, just not strategy-visible"
    assert foundation.queries.get_as_of(LOGICAL_KEY, _dt(30)) is None
    database.drop_all()


def test_bulk_import_respects_observation_mode(rig: _Rig) -> None:
    rig.clock.advance(days=7)  # import happens on 2026-11-12
    fact = _fact(published_at=_dt(5), public_available_at=_dt(5))
    rig.foundation.ingestion.ingest(
        _batch((fact,), retrieved_at=_dt(12), payload={"facts": ["bulk"]})
    )

    availability_view = rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(7))
    observed_view = rig.foundation.queries.get_as_of(
        LOGICAL_KEY, _dt(7), observation_mode=ObservationMode.SYSTEM_OBSERVED_ONLY
    )
    assert availability_view is not None
    assert observed_view is None, "a historical import must not fake a past observation"


def test_date_only_publication_is_not_visible_on_its_own_day(rig: _Rig) -> None:
    fact = _fact(
        published_at=_dt(5, 0, 0),
        public_available_at=_dt(5, 0, 0),
        precision=AvailabilityPrecision.DATE,
        basis=AvailabilityBasis.SOURCE_PUBLICATION_DATE,
    )
    rig.foundation.ingestion.ingest(_batch((fact,), payload={"facts": ["date-only"]}))
    assert rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(5, 23, 59)) is None
    assert rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(6, 0, 0)) is not None


def test_out_of_order_ingestion_still_answers_historically(rig: _Rig) -> None:
    rig.clock.advance(days=5)
    later = _fact(
        record_id="rec-r2",
        value="9800000000",
        published_at=_dt(10),
        public_available_at=_dt(10),
        source_revision="r2",
    )
    rig.foundation.ingestion.ingest(
        _batch((later,), payload={"facts": ["r2"]}, retrieved_at=_dt(10))
    )

    rig.clock.advance(days=2)
    earlier = _fact(
        record_id="rec-r1",
        value="10000000000",
        published_at=_dt(5),
        public_available_at=_dt(5),
        source_revision="r1",
    )
    outcome = rig.foundation.ingestion.ingest(
        _batch((earlier,), payload={"facts": ["r1"]}, retrieved_at=_dt(12))
    )

    assert QualityIssueCode.OUT_OF_ORDER_INGESTION in outcome.report.issue_codes()
    seventh = rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(7))
    eleventh = rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(11))
    # Sequence numbers follow *ingestion* order (r2 arrived first), so assert on
    # content/availability rather than on sequence.
    assert seventh is not None
    assert seventh.value.as_decimal() == Decimal("10000000000")
    assert eleventh is not None
    assert eleventh.value.as_decimal() == Decimal("9800000000")


# ---------------------------------------------------------------------------
# Invalidation and reporting
# ---------------------------------------------------------------------------
def test_invalidation_removes_a_revision_from_later_queries(rig: _Rig) -> None:
    first = rig.foundation.ingestion.ingest(_batch((_fact(value="10000000000"),)))
    rig.clock.advance(days=5)
    second = rig.foundation.ingestion.ingest(
        _batch(
            (
                _fact(
                    record_id="rec-r2",
                    value="9800000000",
                    source_revision="r2",
                    published_at=_dt(10),
                    public_available_at=_dt(10),
                ),
            ),
            payload={"facts": ["r2"]},
            retrieved_at=_dt(10),
        )
    )
    assert first.revisions
    assert second.revisions

    rig.foundation.queries.invalidate(
        revision_id=second.revisions[0].revision_id,
        reason="provider retracted the restatement",
        effective_at=_dt(15),
    )

    assert rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(11)) is not None
    fallback = rig.foundation.queries.get_as_of(LOGICAL_KEY, _dt(16))
    assert fallback is not None
    assert fallback.revision_id == first.revisions[0].revision_id


def test_quality_report_is_persisted_with_structured_issues(rig: _Rig) -> None:
    good = _fact(record_id="rec-good")
    missing_key = IngestFact(
        source_record_id="rec-bad",
        logical_key="",
        value=DataValue.text_value("x"),
        event_at=_dt(5),
    )
    outcome = rig.foundation.ingestion.ingest(
        _batch((good, missing_key), payload={"facts": ["good", "bad"]})
    )

    assert outcome.report.rejected_rows == 1
    assert outcome.report.accepted_rows == 1
    assert outcome.report.status is QualityState.WARNING
    assert QualityIssueCode.MISSING_REQUIRED_FIELD in outcome.report.issue_codes()

    with rig.uow_factory() as uow:
        stored = uow.ingestion_runs.get(outcome.report.ingestion_id)
        recent = uow.ingestion_runs.list_recent(dataset=DATASET)
    assert stored is not None
    assert stored.received_rows == 2
    assert (
        stored.accepted_rows + stored.incomplete_rows + stored.rejected_rows + stored.duplicate_rows
        == stored.received_rows
    )
    assert len(recent) == 1


def test_capability_changes_are_auditable(rig: _Rig) -> None:
    provider = "audit-provider"
    rig.foundation.capabilities.declare(
        provider_id=provider,
        supported=[DataCapability.DAILY_BARS],
        reason="initial declaration",
    )
    rig.clock.advance(days=1)
    rig.foundation.capabilities.declare(
        provider_id=provider,
        supported=[DataCapability.DAILY_BARS, DataCapability.CORPORATE_ACTIONS],
        reason="corporate actions enabled",
    )

    latest = rig.foundation.capabilities.latest(provider)
    assert latest is not None
    assert latest.supports(DataCapability.CORPORATE_ACTIONS)

    changes = rig.foundation.capabilities.changes(provider)
    assert changes
    assert changes[0].added == (DataCapability.CORPORATE_ACTIONS,)

    history = rig.foundation.capabilities.history(provider)
    assert len(history) == 2, "declarations are append-only"


def test_provider_capability_gate_raises_for_undeclared(rig: _Rig) -> None:
    from janus.domain.common.errors import UnsupportedEntityError

    with pytest.raises(UnsupportedEntityError):
        rig.foundation.capabilities.require("nobody", DataCapability.DAILY_BARS)


def test_capability_declaration_is_idempotent(rig: _Rig) -> None:
    provider = "idempotent-provider"
    first = rig.foundation.capabilities.declare(
        provider_id=provider, supported=[DataCapability.DAILY_BARS]
    )
    second = rig.foundation.capabilities.declare(
        provider_id=provider, supported=[DataCapability.DAILY_BARS]
    )
    assert first.declaration_id == second.declaration_id
    assert len(rig.foundation.capabilities.history(provider)) == 1


def test_ingestion_requires_registered_source_for_reports(rig: _Rig) -> None:
    with rig.uow_factory() as uow:
        assert uow.data_sources.exists(SOURCE) is True
        assert uow.data_sources.exists("missing") is False


def test_provider_capabilities_model_is_shared_across_providers() -> None:
    """Two providers declaring the same capability are directly comparable."""
    first = ProviderCapabilities(
        provider_id="provider-a",
        supported=frozenset({DataCapability.DAILY_BARS}),
        declared_at=_dt(5),
    )
    second = ProviderCapabilities(
        provider_id="provider-b",
        supported=frozenset({DataCapability.DAILY_BARS}),
        declared_at=_dt(5),
    )
    assert first.supported == second.supported
    assert first.declaration_id != second.declaration_id
