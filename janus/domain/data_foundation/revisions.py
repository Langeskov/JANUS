"""Immutable normalized data revisions.

A *logical fact* (for example "revenue of 600519.SH for 2026Q3") is identified by
a stable ``logical_key``. Each correction, restatement or provider revision of
that fact is a **new** :class:`DataRevision`; earlier revisions are never updated
or deleted.

That append-only shape is what makes point-in-time queries meaningful: a query
"as of 7 November" can return the revision that was permissible then, even when
a newer revision exists today.

Ordering inside one logical key is deterministic:
``(revision_sequence, revision_id)``. ``revision_sequence`` is allocated by the
ingestion service for the ``(logical_key, source_revision)`` pair; it is never
reused.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from janus.domain.common.errors import ValidationError
from janus.domain.common.time import ensure_aware, to_utc
from janus.domain.data_foundation.identifiers import (
    deterministic_id,
    require_content_hash,
)

__all__ = [
    "DataRevision",
    "DataValue",
    "RevisionInvalidation",
    "ValueKind",
]


class ValueKind(StrEnum):
    """Provider-independent value discriminator for a normalized fact."""

    DECIMAL = "DECIMAL"
    INTEGER = "INTEGER"
    TEXT = "TEXT"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    DATETIME = "DATETIME"


class DataValue(BaseModel):
    """One normalized fact value, preserved as exact text plus its kind.

    Financial values are never converted to ``float``; a decimal fact keeps its
    exact digits and its unit. ``text`` is the canonical lexical form and is what
    gets hashed into the revision identity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ValueKind
    text: str = Field(min_length=1, max_length=4000)
    unit: str | None = Field(default=None, max_length=40)
    precision_digits: int | None = Field(default=None, ge=0, le=30)

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValidationError("value unit must not be blank when supplied")
        return value.strip()

    @model_validator(mode="after")
    def _canonical(self) -> DataValue:
        kind = self.kind
        text = self.text
        if kind is ValueKind.DECIMAL:
            try:
                parsed = Decimal(text)
            except InvalidOperation as exc:
                raise ValidationError("DECIMAL value is not a decimal", text=text) from exc
            if not parsed.is_finite():
                raise ValidationError("DECIMAL value must be finite", text=text)
        elif kind is ValueKind.INTEGER:
            try:
                int(text)
            except ValueError as exc:
                raise ValidationError("INTEGER value is not an integer", text=text) from exc
        elif kind is ValueKind.BOOLEAN:
            if text not in {"true", "false"}:
                raise ValidationError("BOOLEAN value must be 'true' or 'false'", text=text)
        elif kind is ValueKind.DATE:
            try:
                date.fromisoformat(text)
            except ValueError as exc:
                raise ValidationError("DATE value is not an ISO date", text=text) from exc
        elif kind is ValueKind.DATETIME:
            try:
                parsed_dt = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValidationError("DATETIME value is not ISO-8601", text=text) from exc
            ensure_aware(parsed_dt, "value")
        return self

    # -- convenience constructors ---------------------------------------
    @classmethod
    def decimal_value(
        cls, value: Decimal, *, unit: str | None = None, precision_digits: int | None = None
    ) -> DataValue:
        return cls(
            kind=ValueKind.DECIMAL,
            text=format(value, "f"),
            unit=unit,
            precision_digits=precision_digits,
        )

    @classmethod
    def integer_value(cls, value: int, *, unit: str | None = None) -> DataValue:
        return cls(kind=ValueKind.INTEGER, text=str(value), unit=unit)

    @classmethod
    def text_value(cls, value: str) -> DataValue:
        return cls(kind=ValueKind.TEXT, text=value)

    @classmethod
    def boolean_value(cls, value: bool) -> DataValue:
        return cls(kind=ValueKind.BOOLEAN, text="true" if value else "false")

    @classmethod
    def date_value(cls, value: date) -> DataValue:
        return cls(kind=ValueKind.DATE, text=value.isoformat())

    @classmethod
    def datetime_value(cls, value: datetime) -> DataValue:
        return cls(kind=ValueKind.DATETIME, text=ensure_aware(value, "value").isoformat())

    # -- accessors -------------------------------------------------------
    def as_decimal(self) -> Decimal:
        if self.kind is not ValueKind.DECIMAL:
            raise ValidationError("value is not a DECIMAL", kind=self.kind.value)
        return Decimal(self.text)

    def as_int(self) -> int:
        if self.kind is not ValueKind.INTEGER:
            raise ValidationError("value is not an INTEGER", kind=self.kind.value)
        return int(self.text)

    def canonical(self) -> str:
        """Stable hashable form used for deterministic revision identity."""
        unit = "" if self.unit is None else self.unit
        return f"{self.kind.value}\x1f{self.text}\x1f{unit}"


class DataRevision(BaseModel):
    """One immutable revision of one logical fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    revision_id: str
    logical_key: str = Field(min_length=1, max_length=300)
    dataset: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=100)

    subject_symbol: str | None = Field(default=None, max_length=100)
    """Business identity (instrument symbol) where the fact is instrument-scoped."""

    revision_sequence: int = Field(ge=1)
    value: DataValue

    period_start: date | None = None
    period_end: date | None = None
    """Reporting/business period the fact describes (inclusive end)."""

    effective_from: datetime | None = None
    effective_to: datetime | None = None
    """Applicability window of the fact itself (exclusive end)."""

    provenance_id: str = Field(min_length=1, max_length=100)
    payload_hash: str
    source_revision: str = Field(min_length=1, max_length=200)
    supersedes_revision_id: str | None = Field(default=None, max_length=100)
    ingested_at: datetime

    @field_validator("payload_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return require_content_hash(value)

    @field_validator("ingested_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "ingested_at")

    @field_validator("effective_from", "effective_to")
    @classmethod
    def _aware_optional(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        return None if value is None else ensure_aware(value, info.field_name or "effective")

    @model_validator(mode="after")
    def _consistency(self) -> DataRevision:
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_end < self.period_start
        ):
            raise ValidationError("period_end must not precede period_start")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to <= self.effective_from
        ):
            raise ValidationError("effective_to must be strictly after effective_from")
        if self.supersedes_revision_id == self.revision_id:
            raise ValidationError("a revision cannot supersede itself")
        return self

    # -- construction ----------------------------------------------------
    @classmethod
    def build(
        cls,
        *,
        logical_key: str,
        dataset: str,
        source_id: str,
        revision_sequence: int,
        value: DataValue,
        provenance_id: str,
        payload_hash: str,
        source_revision: str,
        ingested_at: datetime,
        subject_symbol: str | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        effective_from: datetime | None = None,
        effective_to: datetime | None = None,
        supersedes_revision_id: str | None = None,
    ) -> DataRevision:
        """Derive the deterministic revision id and construct the record.

        The revision identity covers *what the fact is* (logical key, provider
        revision marker, payload hash, value and period) but deliberately **not**
        the provenance id: a re-ingestion attempt must be recorded for audit
        without creating a second revision of the same fact.
        """
        payload_hash = require_content_hash(payload_hash)
        period = ""
        if period_start is not None or period_end is not None:
            period = (
                f"{'' if period_start is None else period_start.isoformat()}"
                f"/{'' if period_end is None else period_end.isoformat()}"
            )
        revision_id = deterministic_id(
            "rev",
            logical_key,
            source_revision,
            payload_hash,
            value.canonical(),
            period,
        )
        return cls(
            revision_id=revision_id,
            logical_key=logical_key,
            dataset=dataset,
            source_id=source_id,
            subject_symbol=subject_symbol,
            revision_sequence=revision_sequence,
            value=value,
            period_start=period_start,
            period_end=period_end,
            effective_from=effective_from,
            effective_to=effective_to,
            provenance_id=provenance_id,
            payload_hash=payload_hash,
            source_revision=source_revision,
            supersedes_revision_id=supersedes_revision_id,
            ingested_at=ensure_aware(ingested_at, "ingested_at"),
        )

    # -- queries ---------------------------------------------------------
    def ordering_key(self) -> tuple[int, str]:
        """Deterministic ordering within one logical key (later = larger)."""
        return (self.revision_sequence, self.revision_id)

    def covers_business_time(self, business_at: datetime) -> bool:
        """True when the revision's validity window covers ``business_at``.

        A revision with no window covers all business time.
        """
        moment = ensure_aware(business_at, "business_at")
        if self.effective_from is not None and moment < self.effective_from:
            return False
        return not (self.effective_to is not None and moment >= self.effective_to)

    def covers_period_end(self, day: date) -> bool:
        """True when the fact's business period ends on/after ``day``."""
        return self.period_end is None or self.period_end >= day

    def describe(self) -> str:
        unit = "" if self.value.unit is None else f" {self.value.unit}"
        return (
            f"{self.logical_key} rev{self.revision_sequence}="
            f"{self.value.text}{unit} (source_revision={self.source_revision})"
        )


class RevisionInvalidation(BaseModel):
    """Append-only record that a revision must no longer be used from a given time.

    Invalidations are never destructive: the revision row stays, and queries
    simply stop selecting it at/after ``effective_at``. Use this when data is
    proven wrong and no replacement revision exists.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    invalidation_id: str
    revision_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    effective_at: datetime
    """Instant from which point-in-time queries must exclude the revision."""

    recorded_at: datetime
    policy_version: str = Field(min_length=1, max_length=100)
    decision_id: str | None = Field(default=None, max_length=100)

    @field_validator("effective_at", "recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "timestamp")

    @classmethod
    def record(
        cls,
        *,
        revision_id: str,
        reason: str,
        effective_at: datetime,
        recorded_at: datetime,
        policy_version: str = "p1a-availability-v1",
        decision_id: str | None = None,
    ) -> RevisionInvalidation:
        effective_at = ensure_aware(effective_at, "effective_at")
        # Normalise to UTC: a deterministic id must not depend on which
        # timezone representation the caller happened to use for the instant.
        invalidation_id = deterministic_id(
            "inv",
            revision_id,
            to_utc(effective_at).isoformat(),
            reason,
            policy_version,
        )
        return cls(
            invalidation_id=invalidation_id,
            revision_id=revision_id,
            reason=reason,
            effective_at=effective_at,
            recorded_at=ensure_aware(recorded_at, "recorded_at"),
            policy_version=policy_version,
            decision_id=decision_id,
        )

    def applies_at(self, at: datetime) -> bool:
        """True when the invalidation is in force at ``at``."""
        return ensure_aware(at, "at") >= self.effective_at
