"""Data foundation domain package.

Provider-independent, immutable, point-in-time-correct data contracts.

Nothing in this package knows about Tushare, AKShare, HTTP, SQLAlchemy or any
other transport/storage detail. It defines *what a fact is*, *where it came
from*, and *when it was allowed to be known*.
"""

from __future__ import annotations

from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPolicy,
    AvailabilityPrecision,
    DateOnlyAvailabilityMode,
    derive_strategy_usable_at,
    market_day_start,
    next_market_day_start,
)
from janus.domain.data_foundation.capabilities import (
    DataCapability,
    DataSource,
    DataSourceKind,
    ProviderCapabilities,
    ProviderCapabilityChange,
    capability_change,
)
from janus.domain.data_foundation.identifiers import (
    content_hash,
    deterministic_id,
    is_valid_content_hash,
    require_content_hash,
)
from janus.domain.data_foundation.provenance import (
    DataProvenance,
    ProvenanceTimestamps,
)
from janus.domain.data_foundation.quality import (
    CRITICAL_ISSUE_CODES,
    IngestionReport,
    IssueSeverity,
    QualityIssue,
    QualityIssueCode,
    QualityState,
    derive_quality_state,
    severity_of,
)
from janus.domain.data_foundation.query import (
    LoadedRevision,
    ObservationMode,
    RevisionQueryFilters,
    invalidated_revision_ids_at,
    revision_ordering_key,
    select_latest_per_logical_key,
)
from janus.domain.data_foundation.raw import (
    RawPayload,
    RedactionFinding,
    find_credential_material,
    require_clean_payload,
)
from janus.domain.data_foundation.revisions import (
    DataRevision,
    DataValue,
    RevisionInvalidation,
    ValueKind,
)

__all__ = [
    "CRITICAL_ISSUE_CODES",
    "AvailabilityBasis",
    "AvailabilityPolicy",
    "AvailabilityPrecision",
    "DataCapability",
    "DataProvenance",
    "DataRevision",
    "DataSource",
    "DataSourceKind",
    "DataValue",
    "DateOnlyAvailabilityMode",
    "IngestionReport",
    "IssueSeverity",
    "LoadedRevision",
    "ObservationMode",
    "ProvenanceTimestamps",
    "ProviderCapabilities",
    "ProviderCapabilityChange",
    "QualityIssue",
    "QualityIssueCode",
    "QualityState",
    "RawPayload",
    "RedactionFinding",
    "RevisionInvalidation",
    "RevisionQueryFilters",
    "ValueKind",
    "capability_change",
    "content_hash",
    "derive_quality_state",
    "derive_strategy_usable_at",
    "deterministic_id",
    "find_credential_material",
    "invalidated_revision_ids_at",
    "is_valid_content_hash",
    "market_day_start",
    "next_market_day_start",
    "require_clean_payload",
    "require_content_hash",
    "revision_ordering_key",
    "select_latest_per_logical_key",
    "severity_of",
]
