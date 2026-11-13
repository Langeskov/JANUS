"""Point-in-time eligibility and deterministic revision selection.

This module holds the *pure* semantics that decide which revisions a strategy
could legally have seen at a historical cutoff. Storage only supplies
candidates; nothing here touches a database.

Eligibility of a revision at ``as_of``:

1. its provenance must be strategy-available at ``as_of``
   (``strategy_usable_at is not None`` **and** ``<= as_of``);
2. under :attr:`ObservationMode.SYSTEM_OBSERVED_ONLY` the system must also have
   observed it by ``as_of`` (historical bulk imports then become invisible to
   historical queries — the stricter, no-look-ahead mode);
3. it must not be invalidated as of ``as_of``;
4. it must match the caller's filters (source, subject, key prefix, business
   time, period).

Selection among several eligible revisions of the same logical key is
deterministic: order by ``(strategy_usable_at, revision_sequence, revision_id)``
and take the greatest.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from janus.domain.common.errors import ValidationError
from janus.domain.common.time import ensure_aware
from janus.domain.data_foundation.provenance import DataProvenance
from janus.domain.data_foundation.revisions import DataRevision, RevisionInvalidation

__all__ = [
    "LoadedRevision",
    "ObservationMode",
    "RevisionQueryFilters",
    "invalidated_revision_ids_at",
    "revision_ordering_key",
    "select_latest_per_logical_key",
]


class ObservationMode(StrEnum):
    """Which notion of "known" a point-in-time query applies."""

    AVAILABILITY_ONLY = "AVAILABILITY_ONLY"
    """Usable when the knowledge policy says the information was public.

    This is the standard backtest convention: a historical fact that was public
    in the past may be used even if JANUS imported it later.
    """

    SYSTEM_OBSERVED_ONLY = "SYSTEM_OBSERVED_ONLY"
    """Also require that JANUS itself had observed the fact by ``as_of``.

    Historical bulk imports are therefore invisible to historical queries. Use
    this to test that backfilling does not manufacture historical observations.
    """


class RevisionQueryFilters(BaseModel):
    """Optional narrowing for an as-of query. All fields are ANDed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str | None = Field(default=None, max_length=100)
    subject_symbol: str | None = Field(default=None, max_length=100)
    logical_key_prefix: str | None = Field(default=None, max_length=300)

    period_end_from: date | None = None
    """Only facts whose business period ends on/after this date."""

    period_end_to: date | None = None
    """Only facts whose business period ends on/before this date."""

    business_at: datetime | None = None
    """Require the revision's validity window to cover this business instant."""

    @field_validator("business_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_aware(value, "business_at")

    def matches(self, revision: DataRevision) -> bool:
        if self.source_id is not None and revision.source_id != self.source_id:
            return False
        if self.subject_symbol is not None and revision.subject_symbol != self.subject_symbol:
            return False
        if self.logical_key_prefix is not None and not revision.logical_key.startswith(
            self.logical_key_prefix
        ):
            return False
        if self.period_end_from is not None and (
            revision.period_end is None or revision.period_end < self.period_end_from
        ):
            return False
        if self.period_end_to is not None and (
            revision.period_end is not None and revision.period_end > self.period_end_to
        ):
            return False
        return self.business_at is None or revision.covers_business_time(self.business_at)


class LoadedRevision(BaseModel):
    """A revision together with the provenance that authorises it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: DataRevision
    provenance: DataProvenance

    def is_eligible(
        self,
        *,
        as_of: datetime,
        observation_mode: ObservationMode = ObservationMode.AVAILABILITY_ONLY,
        invalidated: bool = False,
    ) -> bool:
        """True when a strategy query at ``as_of`` may select this revision."""
        if invalidated:
            return False
        if not self.provenance.is_available_at(as_of):
            return False
        if observation_mode is ObservationMode.SYSTEM_OBSERVED_ONLY:
            return self.provenance.is_system_observed_at(as_of)
        return True

    @property
    def strategy_usable_at(self) -> datetime | None:
        return self.provenance.strategy_usable_at


def invalidated_revision_ids_at(
    invalidations: Iterable[RevisionInvalidation], *, as_of: datetime
) -> frozenset[str]:
    """Revision ids invalidated at ``as_of`` (an invalidation is never retroactive
    before its own ``effective_at``)."""
    moment = ensure_aware(as_of, "as_of")
    return frozenset(
        invalidation.revision_id
        for invalidation in invalidations
        if invalidation.applies_at(moment)
    )


def revision_ordering_key(candidate: LoadedRevision) -> tuple[datetime, int, str]:
    """Total, deterministic ordering for selection.

    ``strategy_usable_at`` first (use what was known), then the revision's own
    ``(sequence, id)`` so equal-availability ties are broken the same way in
    every process and every database.
    """
    usable_at = candidate.provenance.strategy_usable_at
    if usable_at is None:  # pragma: no cover - eligibility filters these out
        raise ValidationError(
            "cannot order a revision with unknown strategy availability",
            revision_id=candidate.revision.revision_id,
        )
    sequence, revision_id = candidate.revision.ordering_key()
    return (usable_at, sequence, revision_id)


def select_latest_per_logical_key(
    candidates: Sequence[LoadedRevision],
    *,
    as_of: datetime,
    observation_mode: ObservationMode = ObservationMode.AVAILABILITY_ONLY,
    filters: RevisionQueryFilters | None = None,
    invalidated_revision_ids: frozenset[str] = frozenset(),
) -> tuple[LoadedRevision, ...]:
    """Select the latest eligible revision for each logical key, deterministically.

    Returns candidates ordered by ``logical_key`` so the result is stable and
    therefore safe to persist, compare and test.
    """
    moment = ensure_aware(as_of, "as_of")
    filters = filters or RevisionQueryFilters()
    best: dict[str, LoadedRevision] = {}
    for candidate in candidates:
        revision = candidate.revision
        if not filters.matches(revision):
            continue
        if not candidate.is_eligible(
            as_of=moment,
            observation_mode=observation_mode,
            invalidated=revision.revision_id in invalidated_revision_ids,
        ):
            continue
        current = best.get(revision.logical_key)
        if current is None or revision_ordering_key(candidate) > revision_ordering_key(current):
            best[revision.logical_key] = candidate
    return tuple(best[key] for key in sorted(best))


def candidate_dataset_matches(
    candidates: Sequence[LoadedRevision], dataset: str
) -> tuple[LoadedRevision, ...]:
    """Convenience filter kept explicit so callers cannot forget the dataset."""
    return tuple(candidate for candidate in candidates if candidate.revision.dataset == dataset)


def group_by_logical_key(
    candidates: Sequence[LoadedRevision],
) -> Mapping[str, tuple[LoadedRevision, ...]]:
    """All revisions of each logical key, ordered oldest-first (audit view)."""
    grouped: dict[str, list[LoadedRevision]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.revision.logical_key, []).append(candidate)
    return {
        key: tuple(sorted(values, key=revision_ordering_key))
        for key, values in sorted(grouped.items())
    }
