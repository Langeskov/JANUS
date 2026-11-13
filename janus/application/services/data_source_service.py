"""Data-source registry service.

Every provenance record must point at a *registered* source. Registering is
idempotent and never rewrites an existing registration: the original identity and
registration time are part of the audit trail.
"""

from __future__ import annotations

from janus.application.ports.clock import Clock
from janus.application.ports.repositories import UnitOfWorkFactory
from janus.domain.common.errors import NotFoundError
from janus.domain.common.time import ensure_aware
from janus.domain.data_foundation.capabilities import DataSource, DataSourceKind

__all__ = ["DataSourceService"]


class DataSourceService:
    """Registers and resolves data origins."""

    __slots__ = ("_clock", "_uow_factory")

    def __init__(self, *, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def register(
        self,
        *,
        source_id: str,
        kind: DataSourceKind,
        display_name: str,
        notes: str = "",
    ) -> DataSource:
        """Register a source once; an existing registration is returned unchanged."""
        with self._uow_factory() as uow:
            existing = uow.data_sources.get(source_id)
            if existing is not None:
                return existing
            source = DataSource(
                source_id=source_id,
                kind=kind,
                display_name=display_name,
                registered_at=ensure_aware(self._clock.now(), "registered_at"),
                notes=notes,
            )
            uow.data_sources.register(source)
            uow.commit()
        return source

    def require(self, source_id: str) -> DataSource:
        with self._uow_factory() as uow:
            source = uow.data_sources.get(source_id)
        if source is None:
            raise NotFoundError("data source is not registered", source_id=source_id)
        return source

    def list_all(self) -> tuple[DataSource, ...]:
        with self._uow_factory() as uow:
            return tuple(uow.data_sources.list_all())
