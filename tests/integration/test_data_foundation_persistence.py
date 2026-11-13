"""Persistence round-trip and behaviour tests for the PR-001 data foundation.

Covers the ten new/changed persistence contracts:

* lossless round-trip of every data-foundation model (enums, aware datetimes,
  exact decimal text, frozensets, JSON request scope, nested provenance
  timestamps, child ingestion issues);
* append-only idempotency for every repository (identical replay -> ``False``,
  altered content -> :class:`IdempotencyConflictError`);
* revision history across a new unit of work and a full database restart;
* point-in-time candidate filtering/ordering in ``list_candidates``;
* ``risk_checks.verdict`` round-trip;
* no update/delete method on any data-foundation repository.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from janus.application.ports.data_foundation import DataRevisionQuery
from janus.domain.common.enums import RiskOutcome
from janus.domain.common.errors import IdempotencyConflictError
from janus.domain.common.time import MARKET_TZ
from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPolicy,
    AvailabilityPrecision,
)
from janus.domain.data_foundation.capabilities import (
    DataCapability,
    DataSource,
    DataSourceKind,
    ProviderCapabilities,
)
from janus.domain.data_foundation.provenance import (
    DataProvenance,
    ProvenanceTimestamps,
)
from janus.domain.data_foundation.quality import (
    IngestionReport,
    QualityIssue,
    QualityIssueCode,
)
from janus.domain.data_foundation.query import LoadedRevision
from janus.domain.data_foundation.raw import RawPayload
from janus.domain.data_foundation.revisions import (
    DataRevision,
    DataValue,
    RevisionInvalidation,
)
from janus.domain.risk.models import RiskCheck, RiskRuleId, RiskVerdict
from janus.infrastructure.database import Database, SqlAlchemyUnitOfWorkFactory
from janus.infrastructure.database.repositories import (
    SqlAlchemyDataProvenanceRepository,
    SqlAlchemyDataRevisionRepository,
    SqlAlchemyDataSourceRepository,
    SqlAlchemyIngestionRunRepository,
    SqlAlchemyProviderCapabilityRepository,
    SqlAlchemyRawPayloadRepository,
    SqlAlchemyRevisionInvalidationRepository,
)

SOURCE_ID = "fixture_provider"
SECOND_SOURCE_ID = "secondary_provider"
DATASET = "daily_bars"
OTHER_DATASET = "financial_facts"

T_RETRIEVED = datetime(2026, 11, 13, 9, 0, tzinfo=MARKET_TZ)
T_INGESTED = datetime(2026, 11, 13, 9, 5, tzinfo=MARKET_TZ)
EVENT_AT = datetime(2026, 11, 12, 15, 0, tzinfo=MARKET_TZ)
PUBLISHED_AT = datetime(2026, 11, 12, 16, 0, tzinfo=MARKET_TZ)

LOGICAL_KEY = "bar.close:600519.SH"
SECOND_LOGICAL_KEY = "bar.open:600519.SH"
THIRD_LOGICAL_KEY = "bar.close:510300.SH"

AS_OF_BEFORE_SECOND = datetime(2026, 11, 12, 17, 0, tzinfo=MARKET_TZ)
AS_OF_ALL = datetime(2026, 11, 16, 0, 0, tzinfo=MARKET_TZ)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_source(
    source_id: str = SOURCE_ID,
    *,
    display_name: str = "Fixture Provider",
) -> DataSource:
    return DataSource(
        source_id=source_id,
        kind=DataSourceKind.LOCAL_FIXTURE,
        display_name=display_name,
        registered_at=T_RETRIEVED,
        notes="deterministic fixture source",
    )


def make_payload(
    payload_text: str = '{"bars": [{"symbol": "600519.SH", "close": "101.00"}]}',
    *,
    source_id: str = SOURCE_ID,
    dataset: str = DATASET,
) -> RawPayload:
    return RawPayload.archive(
        source_id=source_id,
        dataset=dataset,
        payload_text=payload_text,
        retrieved_at=T_RETRIEVED,
        ingested_at=T_INGESTED,
        request_scope={"symbol": "600519.SH", "frequency": "daily"},
    )


def make_provenance(
    payload: RawPayload,
    *,
    source_record_id: str = "record-1",
    source_revision: str = "r1",
    public_at: datetime | None = PUBLISHED_AT,
    precision: AvailabilityPrecision = AvailabilityPrecision.INSTANT,
    basis: AvailabilityBasis = AvailabilityBasis.SOURCE_TIMESTAMP,
    published_at: datetime | None = PUBLISHED_AT,
    ingested_at: datetime | None = None,
) -> DataProvenance:
    timestamps = ProvenanceTimestamps(
        event_at=EVENT_AT,
        published_at=published_at,
        retrieved_at=payload.retrieved_at,
        ingested_at=payload.ingested_at if ingested_at is None else ingested_at,
        system_first_observed_at=payload.retrieved_at,
    )
    return DataProvenance.build(
        source_id=payload.source_id,
        dataset=payload.dataset,
        source_record_id=source_record_id,
        timestamps=timestamps,
        availability_basis=basis,
        availability_precision=precision,
        public_available_at=public_at,
        payload_hash=payload.content_hash,
        source_revision=source_revision,
        policy=AvailabilityPolicy(),
    )


def make_revision(
    provenance: DataProvenance,
    *,
    logical_key: str,
    revision_sequence: int = 1,
    value: DataValue | None = None,
    source_revision: str = "r1",
    subject_symbol: str | None = "600519.SH",
    period_start: date | None = None,
    period_end: date | None = None,
    supersedes_revision_id: str | None = None,
) -> DataRevision:
    return DataRevision.build(
        logical_key=logical_key,
        dataset=provenance.dataset,
        source_id=provenance.source_id,
        revision_sequence=revision_sequence,
        value=value or DataValue.decimal_value(Decimal("123.45"), unit="CNY", precision_digits=2),
        provenance_id=provenance.provenance_id,
        payload_hash=provenance.payload_hash,
        source_revision=source_revision,
        ingested_at=provenance.timestamps.ingested_at,
        subject_symbol=subject_symbol,
        period_start=period_start,
        period_end=period_end,
        supersedes_revision_id=supersedes_revision_id,
    )


def make_invalidation(revision: DataRevision) -> RevisionInvalidation:
    return RevisionInvalidation.record(
        revision_id=revision.revision_id,
        reason="source restatement superseded this revision",
        effective_at=datetime(2026, 11, 14, 9, 0, tzinfo=MARKET_TZ),
        recorded_at=T_INGESTED,
        policy_version="p1a-availability-v1",
    )


def make_report(
    ingestion_id: str = "ing-0001",
    *,
    source_id: str = SOURCE_ID,
    dataset: str = DATASET,
) -> IngestionReport:
    return IngestionReport.build(
        ingestion_id=ingestion_id,
        dataset=dataset,
        source_id=source_id,
        started_at=T_RETRIEVED,
        finished_at=T_INGESTED,
        received_rows=2,
        accepted_rows=1,
        incomplete_rows=0,
        rejected_rows=1,
        duplicate_rows=0,
        issues=(
            QualityIssue(
                code=QualityIssueCode.MISSING_REQUIRED_FIELD,
                message="close is missing for one row",
                logical_key=LOGICAL_KEY,
                field="close",
            ),
        ),
    )


def make_capabilities(
    provider_id: str = SOURCE_ID,
    *,
    notes: str = "",
    declared_at: datetime = T_INGESTED,
) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_id=provider_id,
        supported=frozenset({DataCapability.DAILY_BARS, DataCapability.CORPORATE_ACTIONS}),
        declared_at=declared_at,
        declaration_reason="fixture bootstrap",
        notes=notes,
        dataset_versions={DATASET: "2026-11-01", "corporate_actions": "2026-11-01"},
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_every_data_foundation_model_round_trips(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    source = make_source()
    payload = make_payload()
    provenance = make_provenance(payload)
    revision = make_revision(
        provenance,
        logical_key=LOGICAL_KEY,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 9, 30),
    )
    invalidation = make_invalidation(revision)
    report = make_report()
    capabilities = make_capabilities()

    with uow_factory() as uow:
        assert uow.data_sources.register(source) is True
        assert uow.raw_payloads.append(payload) is True
        assert uow.data_provenance.append(provenance) is True
        assert uow.data_revisions.append(revision) is True
        assert uow.revision_invalidations.append(invalidation) is True
        assert uow.ingestion_runs.append(report) is True
        assert uow.provider_capabilities.append(capabilities) is True
        uow.commit()

    with uow_factory() as uow:
        assert uow.data_sources.get(SOURCE_ID) == source
        assert uow.raw_payloads.get(payload.payload_id) == payload
        assert uow.raw_payloads.get_by_content_hash(payload.content_hash) == payload
        assert uow.data_provenance.get(provenance.provenance_id) == provenance
        assert uow.data_revisions.get(revision.revision_id) == revision
        loaded = uow.data_revisions.get_with_provenance(revision.revision_id)
        assert loaded == LoadedRevision(revision=revision, provenance=provenance)
        assert uow.revision_invalidations.list_for_revision(revision.revision_id) == [invalidation]
        assert uow.ingestion_runs.get(report.ingestion_id) == report
        assert uow.provider_capabilities.latest(SOURCE_ID) == capabilities

        reloaded = uow.data_revisions.get(revision.revision_id)
        assert reloaded is not None
        assert reloaded.value.as_decimal() == Decimal("123.45")
        assert reloaded.period_end == date(2026, 9, 30)
        reloaded_provenance = uow.data_provenance.get(provenance.provenance_id)
        assert reloaded_provenance is not None
        assert reloaded_provenance.payload_hash == payload.content_hash
        assert reloaded_provenance.availability_basis is (AvailabilityBasis.SOURCE_TIMESTAMP)
        assert reloaded_provenance.strategy_usable_at == PUBLISHED_AT
        assert reloaded_provenance.timestamps.system_first_observed_at == T_RETRIEVED
        assert capabilities.supported == frozenset(
            {DataCapability.DAILY_BARS, DataCapability.CORPORATE_ACTIONS}
        )


def test_unknown_availability_is_persisted_as_unknown(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    with uow_factory() as uow:
        uow.data_sources.register(make_source())
        payload = make_payload('{"pending": true}')
        uow.raw_payloads.append(payload)
        unknown = make_provenance(
            payload,
            source_record_id="record-unknown",
            precision=AvailabilityPrecision.UNKNOWN,
            basis=AvailabilityBasis.UNKNOWN,
            public_at=None,
            published_at=None,
        )
        assert unknown.strategy_usable_at is None
        assert uow.data_provenance.append(unknown) is True
        uow.commit()

    with uow_factory() as uow:
        reloaded = uow.data_provenance.get(unknown.provenance_id)
        assert reloaded is not None
        assert reloaded.strategy_usable_at is None
        assert reloaded.public_available_at is None
        assert reloaded.availability_precision is AvailabilityPrecision.UNKNOWN
        assert reloaded.strategy_usable_at_derivation is AvailabilityBasis.UNKNOWN
        assert reloaded.timestamps.published_at is None


def test_risk_check_verdict_round_trips(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    from tests.integration import scenario

    decision = scenario.make_risk_decision()
    decision = decision.model_copy(
        update={
            "checks": (
                RiskCheck.ok(RiskRuleId.AVAILABLE_CASH),
                RiskCheck.not_applicable(
                    RiskRuleId.SECTOR_EXPOSURE, "no sector attribution in Phase 0"
                ),
                RiskCheck.indeterminate(RiskRuleId.MARKET_DATA_FRESH, "bar timestamp unavailable"),
            ),
            "outcome": RiskOutcome.REJECT,
            "summary": "indeterminate freshness blocks approval",
        }
    )
    with uow_factory() as uow:
        uow.risk_decisions.append(decision)
        uow.commit()
    with uow_factory() as uow:
        reloaded = uow.risk_decisions.get(decision.risk_decision_id)
        assert reloaded == decision
        assert reloaded is not None
        assert [check.verdict for check in reloaded.checks] == [
            RiskVerdict.PASS,
            RiskVerdict.NOT_APPLICABLE,
            RiskVerdict.INDETERMINATE,
        ]
        assert reloaded.failed_rule_ids == (RiskRuleId.MARKET_DATA_FRESH,)


# ---------------------------------------------------------------------------
# Idempotency and conflicts
# ---------------------------------------------------------------------------


def test_append_is_idempotent_for_every_repository(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    source = make_source()
    payload = make_payload()
    provenance = make_provenance(payload)
    revision = make_revision(provenance, logical_key=LOGICAL_KEY)
    invalidation = make_invalidation(revision)
    report = make_report()
    capabilities = make_capabilities()

    with uow_factory() as uow:
        assert uow.data_sources.register(source) is True
        assert uow.raw_payloads.append(payload) is True
        assert uow.data_provenance.append(provenance) is True
        assert uow.data_revisions.append(revision) is True
        assert uow.revision_invalidations.append(invalidation) is True
        assert uow.ingestion_runs.append(report) is True
        assert uow.provider_capabilities.append(capabilities) is True

        assert uow.data_sources.register(source) is False
        assert uow.raw_payloads.append(payload) is False
        # Content-addressed archive: a re-retrieval with different timestamp
        # metadata is still the same bytes and must not raise.
        assert (
            uow.raw_payloads.append(
                payload.model_copy(update={"retrieved_at": T_RETRIEVED + timedelta(minutes=30)})
            )
            is False
        )
        assert uow.data_provenance.append(provenance) is False
        assert uow.data_revisions.append(revision) is False
        assert uow.revision_invalidations.append(invalidation) is False
        assert uow.ingestion_runs.append(report) is False
        assert uow.provider_capabilities.append(capabilities) is False
        uow.commit()

    with uow_factory() as uow:
        assert len(uow.data_sources.list_all()) == 1
        assert len(uow.raw_payloads.list_by_dataset(DATASET)) == 1
        assert len(uow.data_provenance.list_for_source(SOURCE_ID)) == 1
        assert len(uow.data_revisions.list_for_logical_key(LOGICAL_KEY)) == 1
        assert len(uow.revision_invalidations.list_for_revision(revision.revision_id)) == 1
        assert len(uow.ingestion_runs.list_recent()) == 1
        assert len(uow.provider_capabilities.list_declarations(SOURCE_ID)) == 1
        assert uow.data_revisions.next_revision_sequence(LOGICAL_KEY) == 2
        assert uow.data_revisions.next_revision_sequence("brand.new:key") == 1


def test_conflicting_replay_raises_idempotency_conflict(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    source = make_source()
    payload = make_payload()
    provenance = make_provenance(payload)
    revision = make_revision(provenance, logical_key=LOGICAL_KEY)
    invalidation = make_invalidation(revision)
    report = make_report()
    capabilities = make_capabilities()

    with uow_factory() as uow:
        uow.data_sources.register(source)
        uow.raw_payloads.append(payload)
        uow.data_provenance.append(provenance)
        uow.data_revisions.append(revision)
        uow.revision_invalidations.append(invalidation)
        uow.ingestion_runs.append(report)
        uow.provider_capabilities.append(capabilities)

        with pytest.raises(IdempotencyConflictError):
            uow.data_sources.register(source.model_copy(update={"display_name": "Different name"}))
        with pytest.raises(IdempotencyConflictError):
            # Same payload id reused for different bytes is a hard conflict.
            uow.raw_payloads.append(
                make_payload('{"different content": true}').model_copy(
                    update={"payload_id": payload.payload_id}
                )
            )
        with pytest.raises(IdempotencyConflictError):
            uow.data_provenance.append(provenance.model_copy(update={"source_revision": "r9"}))
        # Revision identity excludes provenance/ingestion-attempt metadata, so the
        # "same fact, later attempt" payload is an idempotent replay (False), not
        # a conflict; a genuine identity-slot clash is covered separately.
        assert (
            uow.data_revisions.append(
                revision.model_copy(
                    update={"ingested_at": revision.ingested_at + timedelta(minutes=1)}
                )
            )
            is False
        )
        with pytest.raises(IdempotencyConflictError):
            uow.revision_invalidations.append(
                invalidation.model_copy(update={"reason": "different reason"})
            )
        with pytest.raises(IdempotencyConflictError):
            uow.ingestion_runs.append(
                report.model_copy(update={"failure_detail": "provider exploded"})
            )
        with pytest.raises(IdempotencyConflictError):
            # ``declaration_id`` ignores dataset_versions, so the same id with
            # altered dataset versions is a real conflict.
            uow.provider_capabilities.append(
                capabilities.model_copy(update={"dataset_versions": {DATASET: "2099-01-01"}})
            )
        # Notes are free-form and do not participate in declaration identity.
        assert (
            uow.provider_capabilities.append(
                capabilities.model_copy(update={"notes": "different notes"})
            )
            is False
        )
        uow.commit()


def test_revision_sequence_slot_conflict_raises(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    with uow_factory() as uow:
        uow.data_sources.register(make_source())
        first_payload = make_payload('{"v": 1}')
        second_payload = make_payload('{"v": 2}')
        uow.raw_payloads.append(first_payload)
        uow.raw_payloads.append(second_payload)
        first_provenance = make_provenance(first_payload, source_record_id="rec-1")
        second_provenance = make_provenance(
            second_payload, source_record_id="rec-2", source_revision="r2"
        )
        uow.data_provenance.append(first_provenance)
        uow.data_provenance.append(second_provenance)
        first_revision = make_revision(
            first_provenance, logical_key=LOGICAL_KEY, revision_sequence=1
        )
        uow.data_revisions.append(first_revision)

        clashing = make_revision(
            second_provenance,
            logical_key=LOGICAL_KEY,
            revision_sequence=1,
            source_revision="r2",
        )
        assert clashing.revision_id != first_revision.revision_id
        with pytest.raises(IdempotencyConflictError):
            uow.data_revisions.append(clashing)


# ---------------------------------------------------------------------------
# Identity layering: attempts vs normalized facts
# ---------------------------------------------------------------------------


def test_reingestion_attempt_records_new_provenance_but_not_a_new_revision(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """PR-001 §4.2 layering: attempts are auditable, facts are not duplicated."""
    payload = make_payload()
    first_provenance = make_provenance(payload, source_record_id="rec-1")
    second_provenance = make_provenance(
        payload,
        source_record_id="rec-1",
        ingested_at=T_INGESTED + timedelta(minutes=5),
    )
    first_revision = make_revision(first_provenance, logical_key=LOGICAL_KEY, revision_sequence=1)
    second_revision = make_revision(second_provenance, logical_key=LOGICAL_KEY, revision_sequence=2)

    assert second_provenance.provenance_id != first_provenance.provenance_id
    assert second_revision.revision_id == first_revision.revision_id

    with uow_factory() as uow:
        uow.data_sources.register(make_source())
        assert uow.raw_payloads.append(payload) is True
        assert uow.data_provenance.append(first_provenance) is True
        assert uow.data_revisions.append(first_revision) is True
        assert uow.data_provenance.append(second_provenance) is True
        assert uow.data_revisions.append(second_revision) is False
        uow.commit()

    with uow_factory() as uow:
        assert len(uow.data_provenance.list_for_source(SOURCE_ID)) == 2
        assert uow.data_revisions.list_for_logical_key(LOGICAL_KEY) == [first_revision]
        loaded = uow.data_revisions.get_with_provenance(first_revision.revision_id)
        assert loaded is not None
        assert loaded.provenance.provenance_id == first_provenance.provenance_id
        assert uow.data_revisions.next_revision_sequence(LOGICAL_KEY) == 2


def test_capability_dedupe_compares_content_and_instants(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    capabilities = make_capabilities(declared_at=datetime(2026, 11, 13, 9, 5, tzinfo=MARKET_TZ))
    with uow_factory() as uow:
        assert uow.provider_capabilities.append(capabilities) is True
        uow.commit()

    # Same declaration content, same instant, different timezone representation.
    utc_twin = capabilities.model_copy(
        update={"declared_at": capabilities.declared_at.astimezone(UTC)}
    )
    assert utc_twin.declaration_id == capabilities.declaration_id
    with uow_factory() as uow:
        assert uow.provider_capabilities.append(utc_twin) is False
        uow.commit()
    with uow_factory() as uow:
        assert len(uow.provider_capabilities.list_declarations(SOURCE_ID)) == 1


def test_ingestion_report_for_unregistered_source_is_persistable(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """The audit log must be able to name a source that was never registered."""
    report = make_report("ing-unknown-source", source_id="never_registered_source")
    with uow_factory() as uow:
        assert uow.ingestion_runs.append(report) is True
        uow.commit()
    with uow_factory() as uow:
        assert uow.ingestion_runs.get(report.ingestion_id) == report


# ---------------------------------------------------------------------------
# History durability
# ---------------------------------------------------------------------------


def test_revision_history_survives_new_uow_and_database_restart(
    database_url: str,
) -> None:
    first_payload = make_payload('{"close": "100.00"}')
    first_provenance = make_provenance(first_payload, source_record_id="rec-1")
    first_revision = make_revision(
        first_provenance,
        logical_key=LOGICAL_KEY,
        revision_sequence=1,
        value=DataValue.decimal_value(Decimal("100.00"), unit="CNY"),
        period_end=date(2026, 9, 30),
    )

    database = Database(database_url)
    try:
        database.create_all()
        factory = SqlAlchemyUnitOfWorkFactory(database)
        with factory() as uow:
            uow.data_sources.register(make_source())
            uow.raw_payloads.append(first_payload)
            uow.data_provenance.append(first_provenance)
            uow.data_revisions.append(first_revision)
            uow.commit()
    finally:
        database.dispose()

    # A completely new engine/session (simulated process restart).
    database = Database(database_url)
    try:
        factory = SqlAlchemyUnitOfWorkFactory(database)
        with factory() as uow:
            assert uow.data_revisions.next_revision_sequence(LOGICAL_KEY) == 2
            second_payload = make_payload('{"close": "101.00"}')
            second_provenance = make_provenance(
                second_payload, source_record_id="rec-2", source_revision="r2"
            )
            second_revision = make_revision(
                second_provenance,
                logical_key=LOGICAL_KEY,
                revision_sequence=uow.data_revisions.next_revision_sequence(LOGICAL_KEY),
                value=DataValue.decimal_value(Decimal("101.00"), unit="CNY"),
                source_revision="r2",
                period_end=date(2026, 9, 30),
                supersedes_revision_id=first_revision.revision_id,
            )
            uow.raw_payloads.append(second_payload)
            uow.data_provenance.append(second_provenance)
            uow.data_revisions.append(second_revision)
            uow.commit()
    finally:
        database.dispose()

    database = Database(database_url)
    try:
        factory = SqlAlchemyUnitOfWorkFactory(database)
        with factory() as uow:
            history = uow.data_revisions.list_for_logical_key(LOGICAL_KEY)
            assert history == [first_revision, second_revision]
            loaded = uow.data_revisions.list_for_logical_key_loaded(LOGICAL_KEY)
            assert [candidate.revision for candidate in loaded] == [
                first_revision,
                second_revision,
            ]
            assert loaded[1].provenance.payload_hash == second_payload.content_hash
            assert loaded[1].revision.supersedes_revision_id == first_revision.revision_id
            assert uow.data_revisions.next_revision_sequence(LOGICAL_KEY) == 3
    finally:
        database.dispose()


# ---------------------------------------------------------------------------
# Candidate query semantics
# ---------------------------------------------------------------------------


def test_list_candidates_as_of_filtering_and_ordering(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    source = make_source()
    second_source = make_source(SECOND_SOURCE_ID, display_name="Secondary Provider")

    payload_a = make_payload('{"candidate": "a"}')
    payload_b = make_payload('{"candidate": "b"}')
    payload_c = make_payload('{"candidate": "c"}')
    payload_facts = make_payload('{"candidate": "facts"}', dataset=OTHER_DATASET)
    payload_other_source = make_payload('{"candidate": "other-source"}', source_id=SECOND_SOURCE_ID)
    payload_unknown = make_payload('{"candidate": "unknown"}')

    provenance_a = make_provenance(payload_a, source_record_id="cand-a", public_at=PUBLISHED_AT)
    provenance_b = make_provenance(
        payload_b,
        source_record_id="cand-b",
        source_revision="r2",
        public_at=datetime(2026, 11, 13, 9, 0, tzinfo=MARKET_TZ),
    )
    provenance_c = make_provenance(
        payload_c,
        source_record_id="cand-c",
        source_revision="r3",
        public_at=datetime(2026, 11, 14, 9, 0, tzinfo=MARKET_TZ),
    )
    provenance_facts = make_provenance(
        payload_facts, source_record_id="cand-facts", source_revision="r4"
    )
    provenance_other_source = make_provenance(
        payload_other_source,
        source_record_id="cand-other",
        source_revision="r5",
        public_at=datetime(2026, 11, 15, 9, 0, tzinfo=MARKET_TZ),
    )
    provenance_unknown = make_provenance(
        payload_unknown,
        source_record_id="cand-unknown",
        source_revision="r6",
        precision=AvailabilityPrecision.UNKNOWN,
        basis=AvailabilityBasis.UNKNOWN,
        public_at=None,
        published_at=None,
    )

    revision_a = make_revision(
        provenance_a,
        logical_key=LOGICAL_KEY,
        subject_symbol="600519.SH",
        period_end=date(2026, 9, 30),
    )
    revision_b = make_revision(
        provenance_b,
        logical_key=SECOND_LOGICAL_KEY,
        source_revision="r2",
        subject_symbol="600519.SH",
        period_end=date(2026, 9, 30),
    )
    revision_k1_second = make_revision(
        provenance_b,
        logical_key=LOGICAL_KEY,
        revision_sequence=2,
        source_revision="r2",
        value=DataValue.decimal_value(Decimal("100.50"), unit="CNY"),
        subject_symbol="600519.SH",
        period_end=date(2026, 9, 30),
        supersedes_revision_id=revision_a.revision_id,
    )
    revision_c = make_revision(
        provenance_c,
        logical_key=THIRD_LOGICAL_KEY,
        source_revision="r3",
        subject_symbol="510300.SH",
        period_end=date(2026, 11, 12),
    )
    revision_other_source = make_revision(
        provenance_other_source,
        logical_key="bar.close:000001.SZ",
        source_revision="r5",
        subject_symbol="000001.SZ",
        period_end=date(2026, 9, 30),
    )
    revision_facts = make_revision(
        provenance_facts,
        logical_key="financial.revenue:600519.SH:2026Q3",
        subject_symbol="600519.SH",
        period_end=date(2026, 9, 30),
    )
    revision_unknown = make_revision(
        provenance_unknown,
        logical_key="bar.close:mystery",
        source_revision="r6",
        subject_symbol=None,
        period_end=None,
    )

    with uow_factory() as uow:
        uow.data_sources.register(source)
        uow.data_sources.register(second_source)
        for payload in (
            payload_b,
            payload_a,
            payload_c,
            payload_facts,
            payload_other_source,
            payload_unknown,
        ):
            uow.raw_payloads.append(payload)
        for provenance in (
            provenance_b,
            provenance_a,
            provenance_c,
            provenance_facts,
            provenance_other_source,
            provenance_unknown,
        ):
            uow.data_provenance.append(provenance)
        for revision in (
            revision_a,
            revision_b,
            revision_k1_second,
            revision_c,
            revision_other_source,
            revision_facts,
            revision_unknown,
        ):
            uow.data_revisions.append(revision)
        uow.commit()

    with uow_factory() as uow:
        all_candidates = uow.data_revisions.list_candidates(
            DataRevisionQuery(dataset=DATASET, as_of=AS_OF_ALL)
        )
        assert [candidate.revision.revision_id for candidate in all_candidates] == [
            revision_a.revision_id,
            revision_b.revision_id,
            revision_k1_second.revision_id,
            revision_c.revision_id,
            revision_other_source.revision_id,
        ]
        assert [candidate.strategy_usable_at for candidate in all_candidates] == sorted(
            candidate.strategy_usable_at for candidate in all_candidates
        )

        before_second = uow.data_revisions.list_candidates(
            DataRevisionQuery(dataset=DATASET, as_of=AS_OF_BEFORE_SECOND)
        )
        assert [candidate.revision.revision_id for candidate in before_second] == [
            revision_a.revision_id
        ]

        by_logical_key = uow.data_revisions.list_candidates(
            DataRevisionQuery(dataset=DATASET, as_of=AS_OF_ALL, logical_key=LOGICAL_KEY)
        )
        assert [candidate.revision.revision_id for candidate in by_logical_key] == [
            revision_a.revision_id,
            revision_k1_second.revision_id,
        ]

        by_prefix = uow.data_revisions.list_candidates(
            DataRevisionQuery(dataset=DATASET, as_of=AS_OF_ALL, logical_key_prefix="bar.c")
        )
        assert [candidate.revision.revision_id for candidate in by_prefix] == [
            revision_a.revision_id,
            revision_k1_second.revision_id,
            revision_c.revision_id,
            revision_other_source.revision_id,
        ]

        by_subject = uow.data_revisions.list_candidates(
            DataRevisionQuery(dataset=DATASET, as_of=AS_OF_ALL, subject_symbol="510300.SH")
        )
        assert [candidate.revision.revision_id for candidate in by_subject] == [
            revision_c.revision_id
        ]

        by_source = uow.data_revisions.list_candidates(
            DataRevisionQuery(dataset=DATASET, as_of=AS_OF_ALL, source_id=SECOND_SOURCE_ID)
        )
        assert [candidate.revision.revision_id for candidate in by_source] == [
            revision_other_source.revision_id
        ]

        period_from = uow.data_revisions.list_candidates(
            DataRevisionQuery(
                dataset=DATASET,
                as_of=AS_OF_ALL,
                period_end_from=date(2026, 10, 1),
            )
        )
        assert [candidate.revision.revision_id for candidate in period_from] == [
            revision_c.revision_id
        ]

        period_to = uow.data_revisions.list_candidates(
            DataRevisionQuery(
                dataset=DATASET,
                as_of=AS_OF_ALL,
                period_end_to=date(2026, 9, 30),
            )
        )
        assert [candidate.revision.revision_id for candidate in period_to] == [
            revision_a.revision_id,
            revision_b.revision_id,
            revision_k1_second.revision_id,
            revision_other_source.revision_id,
        ]

        limited = uow.data_revisions.list_candidates(
            DataRevisionQuery(dataset=DATASET, as_of=AS_OF_ALL, limit=2)
        )
        assert [candidate.revision.revision_id for candidate in limited] == [
            revision_a.revision_id,
            revision_b.revision_id,
        ]

        other_dataset = uow.data_revisions.list_candidates(
            DataRevisionQuery(dataset=OTHER_DATASET, as_of=AS_OF_ALL)
        )
        assert [candidate.revision.revision_id for candidate in other_dataset] == [
            revision_facts.revision_id
        ]

        empty = uow.data_revisions.list_candidates(
            DataRevisionQuery(dataset="no_such_dataset", as_of=AS_OF_ALL)
        )
        assert empty == []

        audit = uow.data_revisions.list_all_for_dataset(DATASET)
        assert [candidate.revision.logical_key for candidate in audit] == sorted(
            candidate.revision.logical_key for candidate in audit
        )


# ---------------------------------------------------------------------------
# Append-only shape and unit-of-work wiring
# ---------------------------------------------------------------------------


def test_data_foundation_repositories_are_append_only() -> None:
    forbidden = (
        "update",
        "delete",
        "remove",
        "save",
        "replace",
        "purge",
        "mutate",
        "edit",
    )
    repository_classes = (
        SqlAlchemyDataSourceRepository,
        SqlAlchemyRawPayloadRepository,
        SqlAlchemyDataProvenanceRepository,
        SqlAlchemyDataRevisionRepository,
        SqlAlchemyRevisionInvalidationRepository,
        SqlAlchemyIngestionRunRepository,
        SqlAlchemyProviderCapabilityRepository,
    )
    for repository_class in repository_classes:
        for method_name in forbidden:
            assert not hasattr(repository_class, method_name), (
                f"{repository_class.__name__} must not expose {method_name!r}: "
                "the data foundation is append-only"
            )


def test_unit_of_work_binds_all_data_foundation_repositories(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    with uow_factory() as uow:
        assert isinstance(uow.data_sources, SqlAlchemyDataSourceRepository)
        assert isinstance(uow.raw_payloads, SqlAlchemyRawPayloadRepository)
        assert isinstance(uow.data_provenance, SqlAlchemyDataProvenanceRepository)
        assert isinstance(uow.data_revisions, SqlAlchemyDataRevisionRepository)
        assert isinstance(uow.revision_invalidations, SqlAlchemyRevisionInvalidationRepository)
        assert isinstance(uow.ingestion_runs, SqlAlchemyIngestionRunRepository)
        assert isinstance(uow.provider_capabilities, SqlAlchemyProviderCapabilityRepository)


def test_list_recent_ingestion_runs_filters_and_orders(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    first = make_report("ing-0001")
    second = make_report("ing-0002", dataset=OTHER_DATASET)
    with uow_factory() as uow:
        uow.data_sources.register(make_source())
        uow.ingestion_runs.append(first)
        uow.ingestion_runs.append(second)
        uow.commit()

    with uow_factory() as uow:
        everything = uow.ingestion_runs.list_recent()
        assert [report.ingestion_id for report in everything] == [
            "ing-0002",
            "ing-0001",
        ]
        filtered = uow.ingestion_runs.list_recent(dataset=OTHER_DATASET)
        assert [report.ingestion_id for report in filtered] == ["ing-0002"]
        by_source = uow.ingestion_runs.list_recent(source_id=SOURCE_ID)
        assert [report.ingestion_id for report in by_source] == [
            "ing-0002",
            "ing-0001",
        ]


def test_invalidations_can_be_listed_for_many_revisions(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    with uow_factory() as uow:
        uow.data_sources.register(make_source())
        payload = make_payload()
        uow.raw_payloads.append(payload)
        provenance = make_provenance(payload)
        uow.data_provenance.append(provenance)
        first = make_revision(provenance, logical_key=LOGICAL_KEY, revision_sequence=1)
        second_payload = make_payload('{"other": true}')
        uow.raw_payloads.append(second_payload)
        second_provenance = make_provenance(
            second_payload, source_record_id="record-2", source_revision="r2"
        )
        uow.data_provenance.append(second_provenance)
        second = make_revision(
            second_provenance,
            logical_key=SECOND_LOGICAL_KEY,
            revision_sequence=1,
            source_revision="r2",
        )
        uow.data_revisions.append(first)
        uow.data_revisions.append(second)
        first_invalidation = make_invalidation(first)
        second_invalidation = make_invalidation(second)
        uow.revision_invalidations.append(first_invalidation)
        uow.revision_invalidations.append(second_invalidation)
        uow.commit()

    with uow_factory() as uow:
        assert uow.revision_invalidations.list_for_revisions([]) == []
        listed = uow.revision_invalidations.list_for_revisions(
            [second.revision_id, first.revision_id]
        )
        expected = sorted(
            [first_invalidation, second_invalidation],
            key=lambda invalidation: invalidation.revision_id,
        )
        assert listed == expected
