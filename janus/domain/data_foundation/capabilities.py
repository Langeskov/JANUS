"""Data-source registry and provider capability declarations.

Phase 1 must be able to say, auditably, *what a provider can and cannot supply*.
A provider that cannot supply a dataset must produce an explicit
"unsupported capability" outcome — never an empty successful result, because an
empty success is indistinguishable from "the market had no data".

Capability declarations are append-only: every declaration is a new immutable
record, so the history of what a provider claimed (and when) is auditable.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from janus.domain.common.errors import UnsupportedEntityError, ValidationError
from janus.domain.common.time import ensure_aware, to_utc
from janus.domain.data_foundation.identifiers import deterministic_id

__all__ = [
    "DataCapability",
    "DataSource",
    "DataSourceKind",
    "ProviderCapabilities",
    "ProviderCapabilityChange",
    "capability_change",
]


class DataSourceKind(StrEnum):
    """What kind of system a data source is."""

    MARKET_DATA_PROVIDER = "MARKET_DATA_PROVIDER"
    EXCHANGE_DISCLOSURE = "EXCHANGE_DISCLOSURE"
    LOCAL_FIXTURE = "LOCAL_FIXTURE"
    MANUAL_ENTRY = "MANUAL_ENTRY"
    DERIVED = "DERIVED"


class DataCapability(StrEnum):
    """Datasets a source may declare support for."""

    DAILY_BARS = "DAILY_BARS"
    HISTORICAL_MINUTES = "HISTORICAL_MINUTES"
    REALTIME_QUOTES = "REALTIME_QUOTES"
    FINANCIAL_REPORTS = "FINANCIAL_REPORTS"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    ANNOUNCEMENTS = "ANNOUNCEMENTS"


class DataSource(BaseModel):
    """A registered origin of data (provider, exchange, fixture, derived set)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1, max_length=100)
    kind: DataSourceKind
    display_name: str = Field(min_length=1, max_length=200)
    registered_at: datetime
    notes: str = ""

    @field_validator("registered_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "registered_at")

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        lowered = value.strip()
        if lowered != lowered.lower():
            raise ValidationError("source_id must be lower-case", source_id=value)
        return lowered


class ProviderCapabilities(BaseModel):
    """One immutable capability declaration for a provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=1, max_length=100)
    supported: frozenset[DataCapability]
    declared_at: datetime
    declaration_reason: str = ""
    notes: str = ""
    dataset_versions: dict[str, str] = Field(default_factory=dict)

    @field_validator("declared_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "declared_at")

    @field_validator("dataset_versions")
    @classmethod
    def _versions(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not key.strip() or not item.strip():
                raise ValidationError("dataset_versions entries must be non-empty")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def _not_empty(self) -> ProviderCapabilities:
        # A declaration with no capabilities at all is still meaningful ("this
        # provider is registered but supplies nothing yet"), so it is allowed.
        if self.notes and len(self.notes) > 2000:
            raise ValidationError("capability notes are too long")
        return self

    # -- queries ---------------------------------------------------------
    def supports(self, capability: DataCapability) -> bool:
        return capability in self.supported

    def require(self, capability: DataCapability) -> None:
        """Raise :class:`UnsupportedEntityError` when the capability is missing.

        This is the fail-closed gate that prevents "unsupported" from being
        reported as an empty successful result.
        """
        if not self.supports(capability):
            raise UnsupportedEntityError(
                "provider does not declare the requested data capability",
                provider_id=self.provider_id,
                capability=capability.value,
                supported=sorted(item.value for item in self.supported),
            )

    def unsupported(self) -> tuple[DataCapability, ...]:
        """Capabilities in the vocabulary that this provider does not declare."""
        return tuple(
            sorted(
                (item for item in DataCapability if item not in self.supported),
                key=lambda item: item.value,
            )
        )

    @property
    def declaration_id(self) -> str:
        """Deterministic identity of this declaration (used for dedupe).

        Timestamps are normalised to UTC before hashing: two representations of
        the same instant (``+08:00`` vs ``Z``) must produce the same id, otherwise
        a round-tripped declaration would look like a new one.
        """
        return deterministic_id(
            "cap",
            self.provider_id,
            ",".join(sorted(item.value for item in self.supported)),
            to_utc(self.declared_at).isoformat(),
            self.declaration_reason,
        )


class ProviderCapabilityChange(BaseModel):
    """Auditable diff between two consecutive capability declarations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    change_id: str
    provider_id: str
    previous_declared_at: datetime | None
    current_declared_at: datetime
    added: tuple[DataCapability, ...] = ()
    removed: tuple[DataCapability, ...] = ()

    @field_validator("previous_declared_at", "current_declared_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_aware(value, "declared_at")

    @property
    def is_change(self) -> bool:
        return bool(self.added or self.removed)


def capability_change(
    previous: ProviderCapabilities | None, current: ProviderCapabilities
) -> ProviderCapabilityChange:
    """Compute the deterministic diff between two declarations."""
    before = frozenset() if previous is None else previous.supported
    added = tuple(
        sorted((item for item in current.supported - before), key=lambda item: item.value)
    )
    removed = tuple(
        sorted((item for item in before - current.supported), key=lambda item: item.value)
    )
    change_id = deterministic_id(
        "capchg",
        current.provider_id,
        ",".join(item.value for item in added),
        ",".join(item.value for item in removed),
        to_utc(current.declared_at).isoformat(),
    )
    return ProviderCapabilityChange(
        change_id=change_id,
        provider_id=current.provider_id,
        previous_declared_at=None if previous is None else previous.declared_at,
        current_declared_at=current.declared_at,
        added=added,
        removed=removed,
    )
