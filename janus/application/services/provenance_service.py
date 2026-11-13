"""Provenance resolution.

Every strategy-visible fact must be explainable: it must resolve to a registered
data source, to an archived raw payload (by content hash), and to a provenance
record that carries the temporal evidence. Resolution **fails closed** — a
dangling or inconsistent reference is a data-quality error, never a silent pass.
"""

from __future__ import annotations

from collections.abc import Sequence

from janus.application.dto.data_foundation import ProvenanceResolution
from janus.application.ports.repositories import UnitOfWork, UnitOfWorkFactory
from janus.domain.common.errors import DataQualityError, NotFoundError, ValidationError
from janus.domain.data_foundation.provenance import DataProvenance
from janus.domain.data_foundation.raw import RawPayload
from janus.domain.data_foundation.revisions import DataRevision

__all__ = ["ProvenanceResolver"]


class ProvenanceResolver:
    """Resolves provenance to its evidence, or refuses to."""

    __slots__ = ("_uow_factory",)

    def __init__(self, *, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def resolve(self, provenance_id: str, *, uow: UnitOfWork | None = None) -> ProvenanceResolution:
        """Resolve one provenance id to its payload and source registration."""
        if not provenance_id or not provenance_id.strip():
            raise ValidationError("provenance_id must not be empty")
        if uow is not None:
            return self._resolve(provenance_id, uow)
        with self._uow_factory() as own:
            return self._resolve(provenance_id, own)

    def resolve_for_revision(
        self, revision: DataRevision, *, uow: UnitOfWork | None = None
    ) -> ProvenanceResolution:
        """Resolve the provenance of one revision."""
        resolution = self.resolve(revision.provenance_id, uow=uow)
        if resolution.provenance.payload_hash != revision.payload_hash:
            raise DataQualityError(
                "revision payload hash disagrees with its provenance",
                revision_id=revision.revision_id,
                revision_payload_hash=revision.payload_hash,
                provenance_payload_hash=resolution.provenance.payload_hash,
            )
        return resolution

    def resolve_many(
        self, revisions: Sequence[DataRevision], *, uow: UnitOfWork | None = None
    ) -> tuple[ProvenanceResolution, ...]:
        """Resolve several revisions (batch helper; same fail-closed rules)."""
        return tuple(self.resolve_for_revision(revision, uow=uow) for revision in revisions)

    # -- internals -------------------------------------------------------
    def _resolve(self, provenance_id: str, uow: UnitOfWork) -> ProvenanceResolution:
        provenance: DataProvenance | None = uow.data_provenance.get(provenance_id)
        if provenance is None:
            raise NotFoundError("provenance record does not exist", provenance_id=provenance_id)
        payload: RawPayload | None = uow.raw_payloads.get_by_content_hash(provenance.payload_hash)
        if payload is None:
            raise DataQualityError(
                "provenance references a raw payload that is not archived",
                provenance_id=provenance_id,
                payload_hash=provenance.payload_hash,
            )
        if payload.content_hash != provenance.payload_hash:
            raise DataQualityError(
                "archived payload content hash disagrees with provenance",
                provenance_id=provenance_id,
            )
        source_registered = uow.data_sources.exists(provenance.source_id)
        if not source_registered:
            raise DataQualityError(
                "provenance references an unregistered data source",
                provenance_id=provenance_id,
                source_id=provenance.source_id,
            )
        return ProvenanceResolution(
            provenance=provenance,
            payload=payload,
            source_registered=True,
        )
