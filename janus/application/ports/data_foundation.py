"""Provider-neutral application ports for the data foundation.

Ordering contract (PR-001 §5): the application layer knows *what* must be
queryable; it does not know that SQLite stores it. Every method below is
expressed in domain value objects only.

Immutability contract: there is deliberately **no** update or delete method for
raw payloads, provenance or revisions. Corrections are new revisions;
invalidations are new invalidation rows. A repository that cannot express a
mutation cannot be used to destroy history.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from janus.domain.data_foundation.capabilities import (
    DataSource,
    ProviderCapabilities,
)
from janus.domain.data_foundation.provenance import DataProvenance
from janus.domain.data_foundation.quality import IngestionReport
from janus.domain.data_foundation.query import LoadedRevision
from janus.domain.data_foundation.raw import RawPayload
from janus.domain.data_foundation.revisions import DataRevision, RevisionInvalidation

__all__ = [
    "DataProvenanceRepository",
    "DataRevisionQuery",
    "DataRevisionRepository",
    "DataSourceRepository",
    "IngestionRunRepository",
    "ProviderCapabilityRepository",
    "RawPayloadRepository",
    "RevisionInvalidationRepository",
]


@runtime_checkable
class DataSourceRepository(Protocol):
    """Registry of known data origins."""

    def register(self, source: DataSource) -> bool:
        """Register ``source``; returns ``False`` if it was already registered."""
        ...

    def get(self, source_id: str) -> DataSource | None: ...

    def exists(self, source_id: str) -> bool: ...

    def list_all(self) -> list[DataSource]: ...


@runtime_checkable
class RawPayloadRepository(Protocol):
    """Append-only raw payload archive with content-hash deduplication."""

    def append(self, payload: RawPayload) -> bool:
        """Archive ``payload``; ``False`` when identical content already exists."""
        ...

    def get(self, payload_id: str) -> RawPayload | None: ...

    def get_by_content_hash(self, content_hash: str) -> RawPayload | None: ...

    def exists_hash(self, content_hash: str) -> bool: ...

    def list_by_dataset(self, dataset: str, *, limit: int = 100) -> list[RawPayload]: ...


@runtime_checkable
class DataProvenanceRepository(Protocol):
    """Append-only provenance store."""

    def append(self, provenance: DataProvenance) -> bool:
        """Persist ``provenance``; ``False`` when the identical record exists."""
        ...

    def get(self, provenance_id: str) -> DataProvenance | None: ...

    def list_for_source(
        self, source_id: str, *, dataset: str | None = None, limit: int = 100
    ) -> list[DataProvenance]: ...


@dataclass(frozen=True, slots=True)
class DataRevisionQuery:
    """Storage-level candidate query for as-of revision retrieval.

    The storage layer applies only these *mechanical* prefilters (dataset,
    availability cutoff, identity fields). All business semantics — observation
    mode, invalidation, business-time coverage, tie-breaking — are applied by the
    application service so the rule cannot drift between storage engines.
    """

    dataset: str
    as_of: datetime
    source_id: str | None = None
    logical_key: str | None = None
    logical_key_prefix: str | None = None
    subject_symbol: str | None = None
    period_end_from: date | None = None
    period_end_to: date | None = None
    limit: int = 10_000


@runtime_checkable
class DataRevisionRepository(Protocol):
    """Append-only normalized revision store."""

    def append(self, revision: DataRevision) -> bool:
        """Persist ``revision``; ``False`` when the same identity already exists."""
        ...

    def get(self, revision_id: str) -> DataRevision | None: ...

    def get_with_provenance(self, revision_id: str) -> LoadedRevision | None: ...

    def list_for_logical_key(self, logical_key: str) -> list[DataRevision]:
        """Every revision of ``logical_key``, ordered by ``revision_sequence``."""
        ...

    def list_for_logical_key_loaded(self, logical_key: str) -> list[LoadedRevision]:
        """Every revision of ``logical_key`` with its provenance, oldest first.

        Used by point-in-time queries that are scoped to a single logical fact.
        """
        ...

    def next_revision_sequence(self, logical_key: str) -> int:
        """Next deterministic sequence number for a new revision of ``logical_key``."""
        ...

    def list_candidates(self, query: DataRevisionQuery) -> list[LoadedRevision]:
        """Revisions of ``query.dataset`` that are strategy-available by ``as_of``.

        Implementations must return ``LoadedRevision`` pairs (revision + its
        provenance) and must not apply invalidation or business-time rules.
        """
        ...

    def list_all_for_dataset(self, dataset: str, *, limit: int = 10_000) -> list[LoadedRevision]:
        """Every revision of a dataset (audit/history view)."""
        ...


@runtime_checkable
class RevisionInvalidationRepository(Protocol):
    """Append-only invalidation records."""

    def append(self, invalidation: RevisionInvalidation) -> bool: ...

    def list_for_revision(self, revision_id: str) -> list[RevisionInvalidation]: ...

    def list_for_revisions(self, revision_ids: Sequence[str]) -> list[RevisionInvalidation]: ...


@runtime_checkable
class IngestionRunRepository(Protocol):
    """Append-only ingestion attempt/report log."""

    def append(self, report: IngestionReport) -> bool: ...

    def get(self, ingestion_id: str) -> IngestionReport | None: ...

    def list_recent(
        self,
        *,
        dataset: str | None = None,
        source_id: str | None = None,
        limit: int = 50,
    ) -> list[IngestionReport]: ...


@runtime_checkable
class ProviderCapabilityRepository(Protocol):
    """Append-only provider capability declarations."""

    def append(self, capabilities: ProviderCapabilities) -> bool:
        """Record a declaration; ``False`` when it is identical to the latest one."""
        ...

    def latest(self, provider_id: str) -> ProviderCapabilities | None: ...

    def list_declarations(self, provider_id: str, *, limit: int = 50) -> list[ProviderCapabilities]:
        """Declarations, newest first."""
        ...
