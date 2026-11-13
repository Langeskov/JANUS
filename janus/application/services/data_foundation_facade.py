"""Data-foundation application facade.

Bundles the data-foundation services so callers (tests, future CLI/API
commands, Phase 1 ingestion jobs) construct them once from a unit-of-work factory
instead of re-wiring five services by hand.

This is a thin composition helper: it contains no business rules.
"""

from __future__ import annotations

from janus.application.ports.clock import Clock
from janus.application.ports.repositories import UnitOfWorkFactory
from janus.application.services.data_ingestion_service import (
    DataFoundationIngestionService,
)
from janus.application.services.data_source_service import DataSourceService
from janus.application.services.point_in_time_service import PointInTimeQueryService
from janus.application.services.provenance_service import ProvenanceResolver
from janus.application.services.provider_capability_service import (
    ProviderCapabilityService,
)
from janus.domain.data_foundation.availability import AvailabilityPolicy

__all__ = ["DataFoundation"]


class DataFoundation:
    """All data-foundation use cases, sharing one transactional boundary."""

    __slots__ = ("capabilities", "ingestion", "provenance", "queries", "sources")

    def __init__(
        self,
        *,
        sources: DataSourceService,
        capabilities: ProviderCapabilityService,
        ingestion: DataFoundationIngestionService,
        queries: PointInTimeQueryService,
        provenance: ProvenanceResolver,
    ) -> None:
        self.sources = sources
        self.capabilities = capabilities
        self.ingestion = ingestion
        self.queries = queries
        self.provenance = provenance

    @classmethod
    def from_uow_factory(
        cls,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        policy: AvailabilityPolicy | None = None,
    ) -> DataFoundation:
        """Wire every data-foundation service against one unit of work."""
        return cls(
            sources=DataSourceService(uow_factory=uow_factory, clock=clock),
            capabilities=ProviderCapabilityService(uow_factory=uow_factory, clock=clock),
            ingestion=DataFoundationIngestionService(
                uow_factory=uow_factory, clock=clock, policy=policy
            ),
            queries=PointInTimeQueryService(uow_factory=uow_factory, clock=clock),
            provenance=ProvenanceResolver(uow_factory=uow_factory),
        )
