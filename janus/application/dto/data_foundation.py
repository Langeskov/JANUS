"""Application DTOs for data ingestion.

Provider adapters (infrastructure) normalise their payloads into
:class:`IngestBatch`/:class:`IngestFact` objects. These DTOs are provider-neutral
and contain no provider SDK types, so the ingestion pipeline never depends on
where the bytes came from.

The DTOs are deliberately *not* domain models: they describe a request to
ingest, whereas the domain models describe the resulting immutable facts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPrecision,
)
from janus.domain.data_foundation.capabilities import DataCapability
from janus.domain.data_foundation.provenance import DataProvenance
from janus.domain.data_foundation.quality import IngestionReport
from janus.domain.data_foundation.raw import RawPayload
from janus.domain.data_foundation.revisions import DataRevision, DataValue

__all__ = [
    "IngestBatch",
    "IngestFact",
    "IngestionOutcome",
    "ProvenanceResolution",
]


@dataclass(frozen=True, slots=True)
class IngestFact:
    """One provider-neutral normalized fact proposed for ingestion.

    ``availability_*`` fields describe what the source actually supplied. The
    ingestion service — never the adapter — applies the knowledge policy to
    derive ``strategy_usable_at``.
    """

    source_record_id: str
    logical_key: str
    value: DataValue
    event_at: datetime

    published_at: datetime | None = None
    """Source-claimed publication instant, when the source supplies one."""

    availability_basis: AvailabilityBasis = AvailabilityBasis.UNKNOWN
    availability_precision: AvailabilityPrecision = AvailabilityPrecision.UNKNOWN
    public_available_at: datetime | None = None

    source_revision: str | None = None
    """Provider revision marker; defaults to the batch's default revision."""

    subject_symbol: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class IngestBatch:
    """One retrieval attempt's worth of facts plus the raw payload."""

    source_id: str
    dataset: str
    payload_text: str
    retrieved_at: datetime
    facts: tuple[IngestFact, ...] = ()

    media_type: str = "application/json"
    request_scope: Mapping[str, str] = field(default_factory=dict)
    provider_id: str | None = None
    required_capability: DataCapability | None = None
    source_revision_default: str = "r1"

    def fact_source_revision(self, fact: IngestFact) -> str:
        return fact.source_revision or self.source_revision_default


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """Everything one ingestion run produced, for callers and tests."""

    report: IngestionReport
    payload: RawPayload | None
    revisions: tuple[DataRevision, ...]
    provenance: tuple[DataProvenance, ...]

    @property
    def is_rejected(self) -> bool:
        from janus.domain.data_foundation.quality import QualityState

        return self.report.status is QualityState.REJECTED


@dataclass(frozen=True, slots=True)
class ProvenanceResolution:
    """A provenance record resolved together with its archived evidence."""

    provenance: DataProvenance
    payload: RawPayload
    source_registered: bool
