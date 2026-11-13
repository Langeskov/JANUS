"""Immutable data provenance.

One :class:`DataProvenance` record explains, for exactly one ingesta of one
source record, *what* was received, *where* it came from, and *when each
different kind of "when" happened*. It never contains credentials, request
headers or environment configuration.

The record is immutable: corrections arrive as new revision/provenance pairs,
never as edits.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from janus.domain.common.errors import ValidationError
from janus.domain.common.time import PointInTime, ensure_aware, to_utc
from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPolicy,
    AvailabilityPrecision,
    derive_strategy_usable_at,
)
from janus.domain.data_foundation.identifiers import (
    deterministic_id,
    require_content_hash,
)

__all__ = ["DataProvenance", "ProvenanceTimestamps"]


class ProvenanceTimestamps(BaseModel):
    """The four observation/publication instants, kept strictly distinct."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_at: datetime
    """When the described event actually occurred (business time)."""

    published_at: datetime | None = None
    """What the source claims about the public release time (may be absent)."""

    retrieved_at: datetime
    """When JANUS fetched this response from the source."""

    ingested_at: datetime
    """When JANUS persisted it. Never earlier than ``retrieved_at``."""

    system_first_observed_at: datetime
    """When JANUS first saw this information.

    For a historical bulk import this is a *modern* timestamp. It must never be
    back-dated to the historical publication time.
    """

    @field_validator("event_at", "retrieved_at", "ingested_at", "system_first_observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "timestamp")

    @field_validator("published_at")
    @classmethod
    def _aware_optional(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_aware(value, "published_at")

    @model_validator(mode="after")
    def _ordering(self) -> ProvenanceTimestamps:
        if self.retrieved_at > self.ingested_at:
            raise ValidationError(
                "retrieved_at must not follow ingested_at",
                retrieved_at=self.retrieved_at.isoformat(),
                ingested_at=self.ingested_at.isoformat(),
            )
        if self.system_first_observed_at > self.retrieved_at:
            raise ValidationError(
                "system_first_observed_at must not follow retrieved_at "
                "(observation cannot happen after the retrieval it belongs to)",
                system_first_observed_at=self.system_first_observed_at.isoformat(),
                retrieved_at=self.retrieved_at.isoformat(),
            )
        if self.published_at is not None and self.published_at < self.event_at:
            raise ValidationError(
                "published_at must not precede event_at",
                published_at=self.published_at.isoformat(),
                event_at=self.event_at.isoformat(),
            )
        return self


class DataProvenance(BaseModel):
    """Immutable provenance for one ingested source record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provenance_id: str
    source_id: str
    """Registered data-source identifier (never a provider SDK class name)."""

    dataset: str
    """Logical dataset name, e.g. ``daily_bars`` or ``financial_facts``."""

    source_record_id: str
    """The source's own identifier for this record."""

    timestamps: ProvenanceTimestamps

    public_available_at: datetime | None
    """Estimated/verified instant the information was publicly accessible.

    For date-only evidence this is the start of the publication day and
    ``availability_precision`` is ``DATE``; it is a lower bound, not a claim of
    exact availability.
    """

    strategy_usable_at: datetime | None
    """Earliest instant the knowledge policy permits a strategy query to see it.

    ``None`` means availability is unknown or insufficiently precise. Unknown
    availability is never treated as available.
    """

    availability_basis: AvailabilityBasis
    availability_precision: AvailabilityPrecision
    strategy_usable_at_derivation: AvailabilityBasis
    """Which evidence justified ``strategy_usable_at`` (``CONSERVATIVE_POLICY``
    when it came from the documented date-only policy)."""

    source_revision: str = Field(min_length=1, max_length=200)
    """The source's revision marker (restatement id, sequence, version...)."""

    knowledge_policy_version: str = Field(min_length=1, max_length=100)
    payload_hash: str
    """SHA-256 of the archived raw payload this record was normalised from."""

    @field_validator(
        "public_available_at",
        "strategy_usable_at",
    )
    @classmethod
    def _aware_optional(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_aware(value, "availability")

    @field_validator("payload_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return require_content_hash(value)

    @field_validator("source_id", "dataset", "source_record_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValidationError("provenance identity fields must not be empty")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> DataProvenance:
        precision = self.availability_precision
        basis = self.availability_basis

        if precision is AvailabilityPrecision.UNKNOWN:
            if self.public_available_at is not None:
                raise ValidationError(
                    "public_available_at must be absent when availability precision is UNKNOWN"
                )
            if basis is not AvailabilityBasis.UNKNOWN:
                raise ValidationError(
                    "availability_basis must be UNKNOWN when precision is UNKNOWN"
                )
        elif self.public_available_at is None:
            raise ValidationError(
                "public_available_at is required when availability precision is known",
                precision=precision.value,
            )

        if self.strategy_usable_at is not None:
            if precision is AvailabilityPrecision.UNKNOWN or basis is AvailabilityBasis.UNKNOWN:
                raise ValidationError(
                    "strategy_usable_at cannot be known when availability is unknown"
                )
            if self.strategy_usable_at_derivation is AvailabilityBasis.UNKNOWN:
                raise ValidationError("a known strategy_usable_at must record the derivation basis")
            if (
                self.public_available_at is not None
                and self.strategy_usable_at < self.public_available_at
            ):
                raise ValidationError(
                    "strategy_usable_at must not precede public_available_at",
                    strategy_usable_at=self.strategy_usable_at.isoformat(),
                    public_available_at=self.public_available_at.isoformat(),
                )
        elif self.strategy_usable_at_derivation is not AvailabilityBasis.UNKNOWN:
            raise ValidationError(
                "unknown strategy availability must record UNKNOWN derivation basis"
            )

        if (
            self.public_available_at is not None
            and self.timestamps.published_at is not None
            and self.public_available_at < self.timestamps.published_at
        ):
            raise ValidationError(
                "public_available_at must not precede published_at",
                public_available_at=self.public_available_at.isoformat(),
                published_at=self.timestamps.published_at.isoformat(),
            )
        return self

    # -- construction ----------------------------------------------------
    @classmethod
    def build(
        cls,
        *,
        source_id: str,
        dataset: str,
        source_record_id: str,
        timestamps: ProvenanceTimestamps,
        availability_basis: AvailabilityBasis,
        availability_precision: AvailabilityPrecision,
        public_available_at: datetime | None,
        payload_hash: str,
        source_revision: str,
        policy: AvailabilityPolicy,
    ) -> DataProvenance:
        """Derive a complete, immutable provenance record from raw inputs."""
        usable_at, derivation = derive_strategy_usable_at(
            public_available_at=public_available_at,
            published_at=timestamps.published_at,
            precision=availability_precision,
            basis=availability_basis,
            policy=policy,
        )
        payload_hash = require_content_hash(payload_hash)
        # Provenance identity is per *ingestion attempt*: it includes
        # ``ingested_at`` so re-retrieving identical bytes records a new attempt
        # (auditable) instead of colliding with the previous one. The normalized
        # revision identity deliberately does not include this, so a repeated
        # attempt cannot create a duplicate fact.
        provenance_id = deterministic_id(
            "prov",
            source_id,
            dataset,
            source_record_id,
            source_revision,
            payload_hash,
            policy.policy_version,
            availability_basis.value,
            availability_precision.value,
            to_utc(timestamps.ingested_at).isoformat(),
        )
        return cls(
            provenance_id=provenance_id,
            source_id=source_id,
            dataset=dataset,
            source_record_id=source_record_id,
            timestamps=timestamps,
            public_available_at=public_available_at,
            strategy_usable_at=usable_at,
            availability_basis=availability_basis,
            availability_precision=availability_precision,
            strategy_usable_at_derivation=derivation,
            source_revision=source_revision,
            knowledge_policy_version=policy.policy_version,
            payload_hash=payload_hash,
        )

    # -- queries ---------------------------------------------------------
    @property
    def has_known_availability(self) -> bool:
        """True when a strategy-visible eligibility instant exists."""
        return self.strategy_usable_at is not None

    def is_available_at(self, at: datetime) -> bool:
        """True when a strategy query at ``at`` may see this fact.

        Unknown availability is **never** available.
        """
        if self.strategy_usable_at is None:
            return False
        return ensure_aware(at, "at") >= self.strategy_usable_at

    def is_system_observed_at(self, at: datetime) -> bool:
        """True when JANUS had actually observed the fact by ``at``.

        Stricter than :meth:`is_available_at`: a historical bulk import was not
        observed by the system in the past, even though the underlying
        information was public.
        """
        return ensure_aware(at, "at") >= self.timestamps.system_first_observed_at

    def to_point_in_time(self) -> PointInTime:
        """Bridge to the Phase 0 :class:`PointInTime` carrier.

        Requires known strategy availability: a fact whose availability is
        unknown cannot be represented as point-in-time usable.
        """
        if self.strategy_usable_at is None:
            raise ValidationError(
                "cannot bridge to PointInTime without known strategy availability",
                provenance_id=self.provenance_id,
            )
        return PointInTime(
            event_time=self.timestamps.event_at,
            published_at=self.timestamps.published_at,
            retrieved_at=self.timestamps.retrieved_at,
            known_at=self.strategy_usable_at,
        )
