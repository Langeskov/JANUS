"""Provider-independent data-quality reporting.

Ingestion never reports "success with zero rows" when something went wrong: an
ingestion run always produces a deterministic :class:`IngestionReport` whose
status is *derived* from its counters and issue codes, and whose counters must
reconcile exactly.

Status derivation (documented once, enforced by a model validator):

* ``REJECTED``  — provider failure or a critical issue (credentials, unknown
  source, malformed/hashed-bad payload, unsupported capability/dataset);
* ``WARNING``   — at least one row was rejected, or a non-critical issue was
  raised (duplicates, conflicting revisions, unknown availability, ...);
* ``INCOMPLETE``— everything was stored but at least one fact has unknown
  strategy availability;
* ``ACCEPTED``  — every row was accepted with known availability and no issues.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from janus.domain.common.errors import ValidationError
from janus.domain.common.time import ensure_aware

__all__ = [
    "CRITICAL_ISSUE_CODES",
    "IngestionReport",
    "IssueSeverity",
    "QualityIssue",
    "QualityIssueCode",
    "QualityState",
    "derive_quality_state",
    "severity_of",
]


class QualityState(StrEnum):
    ACCEPTED = "ACCEPTED"
    WARNING = "WARNING"
    REJECTED = "REJECTED"
    INCOMPLETE = "INCOMPLETE"


class IssueSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class QualityIssueCode(StrEnum):
    """Structured issue codes. Free-form text never has to be parsed."""

    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_FIELD_TYPE = "INVALID_FIELD_TYPE"
    INVALID_TIMESTAMP_PRECISION = "INVALID_TIMESTAMP_PRECISION"
    DUPLICATE_RECORD = "DUPLICATE_RECORD"
    INVALID_PAYLOAD_HASH = "INVALID_PAYLOAD_HASH"
    UNKNOWN_SOURCE_IDENTITY = "UNKNOWN_SOURCE_IDENTITY"
    UNAVAILABLE_PUBLICATION_TIME = "UNAVAILABLE_PUBLICATION_TIME"
    UNKNOWN_AVAILABILITY = "UNKNOWN_AVAILABILITY"
    CONFLICTING_REVISIONS = "CONFLICTING_REVISIONS"
    INCONSISTENT_PROVENANCE_REFERENCE = "INCONSISTENT_PROVENANCE_REFERENCE"
    CREDENTIAL_MATERIAL_DETECTED = "CREDENTIAL_MATERIAL_DETECTED"
    EMPTY_PAYLOAD = "EMPTY_PAYLOAD"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    UNSUPPORTED_DATASET = "UNSUPPORTED_DATASET"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    OUT_OF_ORDER_INGESTION = "OUT_OF_ORDER_INGESTION"


_SEVERITY_BY_CODE: dict[QualityIssueCode, IssueSeverity] = {
    QualityIssueCode.MISSING_REQUIRED_FIELD: IssueSeverity.ERROR,
    QualityIssueCode.INVALID_FIELD_TYPE: IssueSeverity.ERROR,
    QualityIssueCode.INVALID_TIMESTAMP_PRECISION: IssueSeverity.WARNING,
    QualityIssueCode.DUPLICATE_RECORD: IssueSeverity.INFO,
    QualityIssueCode.INVALID_PAYLOAD_HASH: IssueSeverity.CRITICAL,
    QualityIssueCode.UNKNOWN_SOURCE_IDENTITY: IssueSeverity.CRITICAL,
    QualityIssueCode.UNAVAILABLE_PUBLICATION_TIME: IssueSeverity.WARNING,
    QualityIssueCode.UNKNOWN_AVAILABILITY: IssueSeverity.WARNING,
    QualityIssueCode.CONFLICTING_REVISIONS: IssueSeverity.ERROR,
    QualityIssueCode.INCONSISTENT_PROVENANCE_REFERENCE: IssueSeverity.ERROR,
    QualityIssueCode.CREDENTIAL_MATERIAL_DETECTED: IssueSeverity.CRITICAL,
    QualityIssueCode.EMPTY_PAYLOAD: IssueSeverity.ERROR,
    QualityIssueCode.MALFORMED_PAYLOAD: IssueSeverity.CRITICAL,
    QualityIssueCode.UNSUPPORTED_DATASET: IssueSeverity.CRITICAL,
    QualityIssueCode.UNSUPPORTED_CAPABILITY: IssueSeverity.CRITICAL,
    QualityIssueCode.PROVIDER_FAILURE: IssueSeverity.CRITICAL,
    QualityIssueCode.OUT_OF_ORDER_INGESTION: IssueSeverity.WARNING,
}

CRITICAL_ISSUE_CODES: frozenset[QualityIssueCode] = frozenset(
    code for code, severity in _SEVERITY_BY_CODE.items() if severity is IssueSeverity.CRITICAL
)


def severity_of(code: QualityIssueCode) -> IssueSeverity:
    """Intrinsic severity of an issue code (total function, never guessed)."""
    try:
        return _SEVERITY_BY_CODE[code]
    except KeyError as exc:  # pragma: no cover - guarded by the enum
        raise ValidationError(
            "unclassified data-quality issue code; refusing to guess severity",
            code=code.value,
        ) from exc


class QualityIssue(BaseModel):
    """One structured ingestion finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: QualityIssueCode
    message: str = ""
    logical_key: str | None = Field(default=None, max_length=300)
    field: str | None = Field(default=None, max_length=100)

    @property
    def severity(self) -> IssueSeverity:
        return severity_of(self.code)


def derive_quality_state(
    *,
    received_rows: int,
    accepted_rows: int,
    incomplete_rows: int,
    rejected_rows: int,
    duplicate_rows: int,
    issues: tuple[QualityIssue, ...],
    provider_failure: bool = False,
) -> QualityState:
    """Deterministic mapping from counters/issues to a single report status."""
    if provider_failure or any(issue.severity is IssueSeverity.CRITICAL for issue in issues):
        return QualityState.REJECTED
    if rejected_rows > 0:
        return QualityState.WARNING
    if incomplete_rows > 0:
        return QualityState.INCOMPLETE
    if duplicate_rows > 0 or any(
        issue.severity in {IssueSeverity.INFO, IssueSeverity.WARNING} for issue in issues
    ):
        return QualityState.WARNING
    if received_rows > 0 and accepted_rows == received_rows:
        return QualityState.ACCEPTED
    if received_rows == 0:
        return QualityState.WARNING
    raise ValidationError(  # pragma: no cover - counters are validated first
        "ingestion counters do not describe a derivable quality state"
    )


class IngestionReport(BaseModel):
    """Deterministic per-run ingestion report (audited, never free-form)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ingestion_id: str
    dataset: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=100)
    started_at: datetime
    finished_at: datetime

    received_rows: int = Field(ge=0)
    accepted_rows: int = Field(ge=0)
    incomplete_rows: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)

    status: QualityState
    provider_failure: bool = False
    failure_detail: str = ""
    issues: tuple[QualityIssue, ...] = ()

    payload_hash: str | None = None
    """Hash of the archived payload this run consumed, when one was archived."""

    @field_validator("started_at", "finished_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "timestamp")

    @model_validator(mode="after")
    def _reconcile(self) -> IngestionReport:
        if self.finished_at < self.started_at:
            raise ValidationError("finished_at must not precede started_at")
        total = self.accepted_rows + self.incomplete_rows + self.rejected_rows + self.duplicate_rows
        if total != self.received_rows:
            raise ValidationError(
                "ingestion counters must reconcile: received == "
                "accepted + incomplete + rejected + duplicate",
                received=self.received_rows,
                counted=total,
            )
        derived = derive_quality_state(
            received_rows=self.received_rows,
            accepted_rows=self.accepted_rows,
            incomplete_rows=self.incomplete_rows,
            rejected_rows=self.rejected_rows,
            duplicate_rows=self.duplicate_rows,
            issues=self.issues,
            provider_failure=self.provider_failure,
        )
        if derived is not self.status:
            raise ValidationError(
                "reported status contradicts the derived quality state",
                reported=self.status.value,
                derived=derived.value,
            )
        return self

    @classmethod
    def build(
        cls,
        *,
        ingestion_id: str,
        dataset: str,
        source_id: str,
        started_at: datetime,
        finished_at: datetime,
        received_rows: int,
        accepted_rows: int,
        incomplete_rows: int,
        rejected_rows: int,
        duplicate_rows: int,
        issues: tuple[QualityIssue, ...] = (),
        provider_failure: bool = False,
        failure_detail: str = "",
        payload_hash: str | None = None,
    ) -> IngestionReport:
        """Derive the status and build a reconciled report."""
        status = derive_quality_state(
            received_rows=received_rows,
            accepted_rows=accepted_rows,
            incomplete_rows=incomplete_rows,
            rejected_rows=rejected_rows,
            duplicate_rows=duplicate_rows,
            issues=issues,
            provider_failure=provider_failure,
        )
        return cls(
            ingestion_id=ingestion_id,
            dataset=dataset,
            source_id=source_id,
            started_at=ensure_aware(started_at, "started_at"),
            finished_at=ensure_aware(finished_at, "finished_at"),
            received_rows=received_rows,
            accepted_rows=accepted_rows,
            incomplete_rows=incomplete_rows,
            rejected_rows=rejected_rows,
            duplicate_rows=duplicate_rows,
            status=status,
            provider_failure=provider_failure,
            failure_detail=failure_detail,
            issues=issues,
            payload_hash=payload_hash,
        )

    @property
    def has_rejections(self) -> bool:
        return self.rejected_rows > 0

    def issue_codes(self) -> tuple[QualityIssueCode, ...]:
        """Distinct issue codes in deterministic (first-seen) order."""
        return tuple(dict.fromkeys(issue.code for issue in self.issues))
