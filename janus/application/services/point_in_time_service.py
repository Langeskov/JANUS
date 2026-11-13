"""Point-in-time query service.

Answers one question, deterministically: *what may a strategy see at this
cutoff?* The service applies the domain eligibility rules from
:mod:`janus.domain.data_foundation.query` to candidates supplied by storage, so
the temporal semantics live in one place and cannot drift per storage engine.

Guarantees:

* naive ``as_of`` values are rejected (``ValidationError``);
* unknown availability is never visible;
* under :attr:`ObservationMode.SYSTEM_OBSERVED_ONLY` a historical bulk import is
  not visible in the past (no manufactured historical observations);
* invalidation is respected as of the cutoff, never retroactively;
* selection is deterministic (``strategy_usable_at``, then sequence, then id).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from janus.application.ports.clock import Clock, SystemClock
from janus.application.ports.data_foundation import DataRevisionQuery
from janus.application.ports.repositories import UnitOfWork, UnitOfWorkFactory
from janus.domain.common.errors import NotFoundError
from janus.domain.common.time import ensure_aware
from janus.domain.data_foundation.provenance import DataProvenance
from janus.domain.data_foundation.query import (
    LoadedRevision,
    ObservationMode,
    RevisionQueryFilters,
    invalidated_revision_ids_at,
    select_latest_per_logical_key,
)
from janus.domain.data_foundation.revisions import DataRevision, RevisionInvalidation

__all__ = ["PointInTimeQueryService"]


class PointInTimeQueryService:
    """Deterministic as-of queries over immutable normalized facts."""

    __slots__ = ("_clock", "_uow_factory")

    def __init__(self, *, uow_factory: UnitOfWorkFactory, clock: Clock | None = None) -> None:
        self._uow_factory = uow_factory
        self._clock: Clock = clock or SystemClock()

    # -- single logical fact ---------------------------------------------
    def get_as_of(
        self,
        logical_key: str,
        as_of: datetime,
        *,
        observation_mode: ObservationMode = ObservationMode.AVAILABILITY_ONLY,
        filters: RevisionQueryFilters | None = None,
    ) -> DataRevision | None:
        """Latest eligible revision of ``logical_key`` at ``as_of``, or ``None``."""
        selected = self.get_as_of_loaded(
            logical_key,
            as_of,
            observation_mode=observation_mode,
            filters=filters,
        )
        return None if selected is None else selected.revision

    def get_as_of_loaded(
        self,
        logical_key: str,
        as_of: datetime,
        *,
        observation_mode: ObservationMode = ObservationMode.AVAILABILITY_ONLY,
        filters: RevisionQueryFilters | None = None,
    ) -> LoadedRevision | None:
        """As :meth:`get_as_of`, but keeping the authorising provenance."""
        moment = ensure_aware(as_of, "as_of")
        with self._uow_factory() as uow:
            candidates = uow.data_revisions.list_for_logical_key_loaded(logical_key)
            if not candidates:
                return None
            invalidated = self._invalidated_ids(uow, candidates, as_of=moment)
            selected = select_latest_per_logical_key(
                candidates,
                as_of=moment,
                observation_mode=observation_mode,
                filters=filters,
                invalidated_revision_ids=invalidated,
            )
            return selected[0] if selected else None

    # -- whole dataset ----------------------------------------------------
    def query_as_of(
        self,
        dataset: str,
        as_of: datetime,
        *,
        observation_mode: ObservationMode = ObservationMode.AVAILABILITY_ONLY,
        filters: RevisionQueryFilters | None = None,
    ) -> tuple[DataRevision, ...]:
        """Latest eligible revision per logical key in ``dataset`` at ``as_of``."""
        return tuple(
            selected.revision
            for selected in self.query_as_of_loaded(
                dataset,
                as_of,
                observation_mode=observation_mode,
                filters=filters,
            )
        )

    def query_as_of_loaded(
        self,
        dataset: str,
        as_of: datetime,
        *,
        observation_mode: ObservationMode = ObservationMode.AVAILABILITY_ONLY,
        filters: RevisionQueryFilters | None = None,
    ) -> tuple[LoadedRevision, ...]:
        """As :meth:`query_as_of`, but keeping each revision's provenance."""
        if not dataset.strip():
            raise NotFoundError("dataset must not be empty")
        moment = ensure_aware(as_of, "as_of")
        filters = filters or RevisionQueryFilters()
        with self._uow_factory() as uow:
            candidates = uow.data_revisions.list_candidates(
                DataRevisionQuery(
                    dataset=dataset,
                    as_of=moment,
                    source_id=filters.source_id,
                    logical_key_prefix=filters.logical_key_prefix,
                    subject_symbol=filters.subject_symbol,
                    period_end_from=filters.period_end_from,
                    period_end_to=filters.period_end_to,
                )
            )
            if not candidates:
                return ()
            invalidated = self._invalidated_ids(uow, candidates, as_of=moment)
            return select_latest_per_logical_key(
                candidates,
                as_of=moment,
                observation_mode=observation_mode,
                filters=filters,
                invalidated_revision_ids=invalidated,
            )

    # -- audit surfaces ---------------------------------------------------
    def history(self, logical_key: str) -> tuple[LoadedRevision, ...]:
        """Every revision of a logical key, oldest first (no eligibility filter)."""
        with self._uow_factory() as uow:
            return tuple(uow.data_revisions.list_for_logical_key_loaded(logical_key))

    def is_visible(
        self,
        candidate: LoadedRevision,
        as_of: datetime,
        *,
        observation_mode: ObservationMode = ObservationMode.AVAILABILITY_ONLY,
    ) -> bool:
        """Pure eligibility check for one candidate (used by audit tooling/tests)."""
        moment = ensure_aware(as_of, "as_of")
        with self._uow_factory() as uow:
            invalidated = self._invalidated_ids(uow, (candidate,), as_of=moment)
        return candidate.is_eligible(
            as_of=moment,
            observation_mode=observation_mode,
            invalidated=candidate.revision.revision_id in invalidated,
        )

    def invalidate(
        self,
        *,
        revision_id: str,
        reason: str,
        effective_at: datetime,
        decision_id: str | None = None,
        policy_version: str = "p1a-availability-v1",
    ) -> RevisionInvalidation:
        """Append an invalidation (never deletes or mutates the revision)."""
        with self._uow_factory() as uow:
            revision = uow.data_revisions.get(revision_id)
            if revision is None:
                raise NotFoundError("revision does not exist", revision_id=revision_id)
            invalidation = RevisionInvalidation.record(
                revision_id=revision_id,
                reason=reason,
                effective_at=effective_at,
                recorded_at=self._clock.now(),
                policy_version=policy_version,
                decision_id=decision_id,
            )
            uow.revision_invalidations.append(invalidation)
            uow.commit()
        return invalidation

    # -- internals --------------------------------------------------------
    @staticmethod
    def _invalidated_ids(
        uow: UnitOfWork,
        candidates: Sequence[LoadedRevision],
        *,
        as_of: datetime,
    ) -> frozenset[str]:
        revision_ids = [candidate.revision.revision_id for candidate in candidates]
        invalidations = uow.revision_invalidations.list_for_revisions(revision_ids)
        return invalidated_revision_ids_at(invalidations, as_of=as_of)

    def provenance_of(self, revision_id: str) -> DataProvenance:
        """Convenience accessor used by CLI/API audit views."""
        with self._uow_factory() as uow:
            loaded = uow.data_revisions.get_with_provenance(revision_id)
        if loaded is None:
            raise NotFoundError("revision does not exist", revision_id=revision_id)
        return loaded.provenance
