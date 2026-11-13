"""Deterministic ingestion pipeline for the data foundation.

Order of operations for one :class:`IngestBatch` (all inside one transaction):

1. **source identity** — an unregistered source is a hard rejection;
2. **capability gate** — a provider that does not declare the required dataset is
   rejected (never "empty success");
3. **raw payload archive** — screened for credentials, hashed, archived once;
4. **per-fact provenance + revision** — availability derived from the knowledge
   policy, revisions appended immutably, duplicates collapsed;
5. **ingestion report** — counters and structured issues reconciled and stored.

The service never raises for *data* problems: it returns an
:class:`IngestionOutcome` whose report explains exactly what happened. That is
what makes repeated/partial/provider-degraded runs auditable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from janus.application.dto.data_foundation import (
    IngestBatch,
    IngestFact,
    IngestionOutcome,
)
from janus.application.ports.clock import Clock
from janus.application.ports.repositories import UnitOfWork, UnitOfWorkFactory
from janus.domain.common.errors import ValidationError
from janus.domain.common.ids import new_id
from janus.domain.common.time import ensure_aware
from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPolicy,
    AvailabilityPrecision,
)
from janus.domain.data_foundation.identifiers import content_hash
from janus.domain.data_foundation.provenance import (
    DataProvenance,
    ProvenanceTimestamps,
)
from janus.domain.data_foundation.quality import (
    IngestionReport,
    QualityIssue,
    QualityIssueCode,
)
from janus.domain.data_foundation.raw import RawPayload, find_credential_material
from janus.domain.data_foundation.revisions import DataRevision

__all__ = ["DataFoundationIngestionService"]


@dataclass(slots=True)
class _Counters:
    accepted: int = 0
    incomplete: int = 0
    rejected: int = 0
    duplicate: int = 0

    @property
    def total(self) -> int:
        return self.accepted + self.incomplete + self.rejected + self.duplicate


@dataclass(slots=True)
class _Issues:
    items: list[QualityIssue] = field(default_factory=list)

    def add(
        self,
        code: QualityIssueCode,
        message: str,
        *,
        logical_key: str | None = None,
        field_name: str | None = None,
    ) -> None:
        self.items.append(
            QualityIssue(
                code=code,
                message=message,
                logical_key=logical_key,
                field=field_name,
            )
        )


class DataFoundationIngestionService:
    """Ingests provider-neutral batches into the immutable data foundation."""

    __slots__ = ("_clock", "_policy", "_uow_factory")

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        policy: AvailabilityPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._policy = policy or AvailabilityPolicy()

    @property
    def policy(self) -> AvailabilityPolicy:
        return self._policy

    # -- public API -------------------------------------------------------
    def ingest(self, batch: IngestBatch) -> IngestionOutcome:
        """Ingest one batch atomically and return its reconciled report."""
        started_at = ensure_aware(self._clock.now(), "started_at")
        retrieved_at = ensure_aware(batch.retrieved_at, "retrieved_at")
        if retrieved_at > started_at:
            raise ValidationError(
                "batch.retrieved_at must not be in the future",
                retrieved_at=retrieved_at.isoformat(),
                started_at=started_at.isoformat(),
            )
        facts = self._ordered_facts(batch)
        with self._uow_factory() as uow:
            outcome = self._ingest(uow, batch, facts, started_at=started_at)
            uow.commit()
        return outcome

    # -- pipeline ---------------------------------------------------------
    def _ingest(
        self,
        uow: UnitOfWork,
        batch: IngestBatch,
        facts: tuple[IngestFact, ...],
        *,
        started_at: datetime,
    ) -> IngestionOutcome:
        counters = _Counters()
        issues = _Issues()

        if not batch.dataset.strip():
            counters.rejected = len(facts)
            issues.add(
                QualityIssueCode.MISSING_REQUIRED_FIELD,
                "batch dataset is empty",
                field_name="dataset",
            )
            return self._finish(
                uow, batch, started_at, counters, issues, payload_hash=None, received=len(facts)
            )

        # 1. Source identity -- fail closed, never invent a source.
        if not uow.data_sources.exists(batch.source_id):
            counters.rejected = len(facts)
            issues.add(
                QualityIssueCode.UNKNOWN_SOURCE_IDENTITY,
                f"data source {batch.source_id!r} is not registered",
                field_name="source_id",
            )
            return self._finish(
                uow, batch, started_at, counters, issues, payload_hash=None, received=len(facts)
            )

        # 2. Capability gate -- unsupported capability is a rejection.
        if batch.provider_id is not None and batch.required_capability is not None:
            declared = uow.provider_capabilities.latest(batch.provider_id)
            if declared is None or not declared.supports(batch.required_capability):
                counters.rejected = len(facts)
                issues.add(
                    QualityIssueCode.UNSUPPORTED_CAPABILITY,
                    f"provider {batch.provider_id!r} does not declare "
                    f"{batch.required_capability.value!r}",
                    field_name="required_capability",
                )
                return self._finish(
                    uow,
                    batch,
                    started_at,
                    counters,
                    issues,
                    payload_hash=None,
                    received=len(facts),
                )

        # 3. Raw payload: screen, hash, archive (idempotent).
        archived = self._archive_payload(uow, batch, issues)
        if archived is None:
            counters.rejected = len(facts)
            return self._finish(
                uow, batch, started_at, counters, issues, payload_hash=None, received=len(facts)
            )
        payload, first_observed_at = archived

        # 4. Per-fact provenance + revision.
        loaded_cache: dict[str, list[tuple[DataRevision, datetime | None]]] = {}
        seen_in_batch: dict[tuple[str, str], str] = {}
        sequence_counters: dict[str, int] = {}
        revisions: list[DataRevision] = []
        provenances: list[DataProvenance] = []

        for fact in facts:
            fact_issues, fatal = self._validate_fact(fact)
            for issue in fact_issues:
                issues.items.append(issue)
            if fatal:
                counters.rejected += 1
                continue

            source_revision = batch.fact_source_revision(fact)
            identity = (fact.logical_key, source_revision)
            canonical = fact.value.canonical()

            prior_in_batch = seen_in_batch.get(identity)
            if prior_in_batch is not None:
                if prior_in_batch == canonical:
                    counters.duplicate += 1
                    issues.add(
                        QualityIssueCode.DUPLICATE_RECORD,
                        "the same logical fact appears twice in one batch",
                        logical_key=fact.logical_key,
                    )
                else:
                    counters.rejected += 1
                    issues.add(
                        QualityIssueCode.CONFLICTING_REVISIONS,
                        "the same source revision claims two different values in one batch",
                        logical_key=fact.logical_key,
                    )
                continue
            seen_in_batch[identity] = canonical

            timestamps = self._timestamps(fact, batch, first_observed_at)
            if timestamps is None:
                counters.rejected += 1
                issues.add(
                    QualityIssueCode.INVALID_TIMESTAMP_PRECISION,
                    "fact timestamps are inconsistent or naive",
                    logical_key=fact.logical_key,
                )
                continue

            try:
                provenance = DataProvenance.build(
                    source_id=batch.source_id,
                    dataset=batch.dataset,
                    source_record_id=fact.source_record_id,
                    timestamps=timestamps,
                    availability_basis=fact.availability_basis,
                    availability_precision=fact.availability_precision,
                    public_available_at=fact.public_available_at,
                    payload_hash=payload.content_hash,
                    source_revision=source_revision,
                    policy=self._policy,
                )
            except ValidationError as exc:
                counters.rejected += 1
                issues.add(
                    QualityIssueCode.INVALID_TIMESTAMP_PRECISION,
                    f"provenance could not be established: {exc.message}",
                    logical_key=fact.logical_key,
                )
                continue

            cache = loaded_cache.setdefault(
                fact.logical_key,
                [
                    (item.revision, item.provenance.strategy_usable_at)
                    for item in uow.data_revisions.list_for_logical_key_loaded(fact.logical_key)
                ],
            )

            existing_same_revision = [
                revision
                for revision, _usable_at in cache
                if revision.source_revision == source_revision
            ]
            # An identical fact (same provider revision *and* same content) is a
            # duplicate delivery, not a new revision.
            if any(
                revision.payload_hash == provenance.payload_hash
                and revision.value.canonical() == canonical
                for revision in existing_same_revision
            ):
                counters.duplicate += 1
                issues.add(
                    QualityIssueCode.DUPLICATE_RECORD,
                    "this logical fact revision is already stored",
                    logical_key=fact.logical_key,
                )
                continue
            # A provider revision marker that now claims different content is a
            # contradiction, not a correction: corrections must carry a new
            # source_revision.
            if existing_same_revision:
                counters.rejected += 1
                issues.add(
                    QualityIssueCode.CONFLICTING_REVISIONS,
                    "an existing revision of this logical fact has the same provider "
                    "revision marker but different content",
                    logical_key=fact.logical_key,
                )
                continue

            if fact.logical_key not in sequence_counters:
                sequence_counters[fact.logical_key] = uow.data_revisions.next_revision_sequence(
                    fact.logical_key
                )
            sequence = sequence_counters[fact.logical_key]

            try:
                revision = DataRevision.build(
                    logical_key=fact.logical_key,
                    dataset=batch.dataset,
                    source_id=batch.source_id,
                    revision_sequence=sequence,
                    value=fact.value,
                    provenance_id=provenance.provenance_id,
                    payload_hash=payload.content_hash,
                    source_revision=source_revision,
                    ingested_at=timestamps.ingested_at,
                    subject_symbol=fact.subject_symbol,
                    period_start=fact.period_start,
                    period_end=fact.period_end,
                    effective_from=fact.effective_from,
                    effective_to=fact.effective_to,
                )
            except ValidationError as exc:
                counters.rejected += 1
                issues.add(
                    QualityIssueCode.MISSING_REQUIRED_FIELD,
                    f"revision could not be established: {exc.message}",
                    logical_key=fact.logical_key,
                )
                continue

            uow.data_provenance.append(provenance)
            created = uow.data_revisions.append(revision)
            if not created:
                counters.duplicate += 1
                issues.add(
                    QualityIssueCode.DUPLICATE_RECORD,
                    "this revision identity is already stored",
                    logical_key=fact.logical_key,
                )
                continue
            sequence_counters[fact.logical_key] = sequence + 1
            revisions.append(revision)
            provenances.append(provenance)

            usable_at = provenance.strategy_usable_at
            if usable_at is None:
                counters.incomplete += 1
                issues.add(
                    QualityIssueCode.UNKNOWN_AVAILABILITY,
                    "availability is unknown; the fact is stored but not strategy-visible",
                    logical_key=fact.logical_key,
                )
            else:
                counters.accepted += 1
                prior_usables = [item for _revision, item in cache if item is not None]
                if prior_usables and usable_at < max(prior_usables):
                    issues.add(
                        QualityIssueCode.OUT_OF_ORDER_INGESTION,
                        "a later-loaded revision became available before an earlier one",
                        logical_key=fact.logical_key,
                    )
            cache.append((revision, usable_at))

        outcome = self._finish(
            uow,
            batch,
            started_at,
            counters,
            issues,
            payload_hash=payload.content_hash,
            received=len(facts),
        )
        return IngestionOutcome(
            report=outcome.report,
            payload=outcome.payload,
            revisions=tuple(revisions),
            provenance=tuple(provenances),
        )

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _ordered_facts(batch: IngestBatch) -> tuple[IngestFact, ...]:
        """Deterministic processing order, so identical batches give identical reports."""
        return tuple(
            sorted(
                batch.facts,
                key=lambda fact: (
                    fact.source_record_id,
                    fact.logical_key,
                    fact.value.canonical(),
                ),
            )
        )

    def _archive_payload(
        self, uow: UnitOfWork, batch: IngestBatch, issues: _Issues
    ) -> tuple[RawPayload, datetime] | None:
        """Archive the raw payload once, or explain why it is refused."""
        text = batch.payload_text
        if not text.strip():
            issues.add(QualityIssueCode.EMPTY_PAYLOAD, "payload is empty", field_name="payload")
            return None

        findings = find_credential_material(text)
        if findings:
            issues.add(
                QualityIssueCode.CREDENTIAL_MATERIAL_DETECTED,
                "payload contains credential material and was not archived: "
                + ", ".join(finding.value for finding in findings),
                field_name="payload",
            )
            return None

        if batch.media_type.endswith("json"):
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                issues.add(
                    QualityIssueCode.MALFORMED_PAYLOAD,
                    f"payload is not valid JSON: {exc.msg}",
                    field_name="payload",
                )
                return None

        digest = content_hash(text)
        existing = uow.raw_payloads.get_by_content_hash(digest)
        first_observed_at = (
            existing.retrieved_at
            if existing is not None and existing.retrieved_at <= batch.retrieved_at
            else batch.retrieved_at
        )
        ingested_at = ensure_aware(self._clock.now(), "ingested_at")
        payload = RawPayload.archive(
            source_id=batch.source_id,
            dataset=batch.dataset,
            payload_text=text,
            retrieved_at=batch.retrieved_at,
            ingested_at=ingested_at,
            media_type=batch.media_type,
            request_scope=dict(batch.request_scope),
        )
        uow.raw_payloads.append(payload)
        return payload, first_observed_at

    def _timestamps(
        self, fact: IngestFact, batch: IngestBatch, first_observed_at: datetime
    ) -> ProvenanceTimestamps | None:
        try:
            return ProvenanceTimestamps(
                event_at=fact.event_at,
                published_at=fact.published_at,
                retrieved_at=batch.retrieved_at,
                ingested_at=ensure_aware(self._clock.now(), "ingested_at"),
                system_first_observed_at=first_observed_at,
            )
        except ValidationError:
            return None

    @staticmethod
    def _validate_fact(fact: IngestFact) -> tuple[list[QualityIssue], bool]:
        issues: list[QualityIssue] = []
        fatal = False
        if not fact.logical_key.strip():
            issues.append(
                QualityIssue(
                    code=QualityIssueCode.MISSING_REQUIRED_FIELD,
                    message="logical_key is empty",
                    field="logical_key",
                )
            )
            fatal = True
        if not fact.source_record_id.strip():
            issues.append(
                QualityIssue(
                    code=QualityIssueCode.MISSING_REQUIRED_FIELD,
                    message="source_record_id is empty",
                    field="source_record_id",
                )
            )
            fatal = True
        for field_name, value in (
            ("event_at", fact.event_at),
            ("published_at", fact.published_at),
            ("public_available_at", fact.public_available_at),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                issues.append(
                    QualityIssue(
                        code=QualityIssueCode.INVALID_TIMESTAMP_PRECISION,
                        message=f"{field_name} must be timezone-aware",
                        logical_key=fact.logical_key,
                        field=field_name,
                    )
                )
                fatal = True
        if (
            fact.availability_precision is AvailabilityPrecision.DATE
            and fact.availability_basis
            in {
                AvailabilityBasis.SOURCE_PUBLICATION_DATE,
                AvailabilityBasis.OFFICIAL_RELEASE_SCHEDULE,
            }
            and fact.published_at is None
            and fact.public_available_at is None
        ):
            issues.append(
                QualityIssue(
                    code=QualityIssueCode.UNAVAILABLE_PUBLICATION_TIME,
                    message="date-precision availability was declared but no date was supplied",
                    logical_key=fact.logical_key,
                    field="published_at",
                )
            )
        return issues, fatal

    def _finish(
        self,
        uow: UnitOfWork,
        batch: IngestBatch,
        started_at: datetime,
        counters: _Counters,
        issues: _Issues,
        *,
        payload_hash: str | None,
        received: int,
    ) -> IngestionOutcome:
        """Persist the reconciled report; callers must have counted every row."""
        if counters.total != received:
            raise ValidationError(
                "ingestion counters do not account for every received row",
                received=received,
                accounted=counters.total,
            )
        finished_at = ensure_aware(self._clock.now(), "finished_at")
        report = IngestionReport.build(
            ingestion_id=new_id("ing"),
            dataset=batch.dataset,
            source_id=batch.source_id,
            started_at=started_at,
            finished_at=finished_at,
            received_rows=received,
            accepted_rows=counters.accepted,
            incomplete_rows=counters.incomplete,
            rejected_rows=counters.rejected,
            duplicate_rows=counters.duplicate,
            issues=tuple(issues.items),
            payload_hash=payload_hash,
        )
        uow.ingestion_runs.append(report)
        payload = None
        if payload_hash is not None:
            payload = uow.raw_payloads.get_by_content_hash(payload_hash)
        return IngestionOutcome(
            report=report,
            payload=payload,
            revisions=(),
            provenance=(),
        )
