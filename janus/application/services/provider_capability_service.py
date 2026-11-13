"""Provider capability service.

Capability declarations are append-only and auditable. The service is the place
the rest of the system asks "may this provider supply this dataset?": an unknown
provider or an undeclared capability is an explicit
:class:`~janus.domain.common.errors.UnsupportedEntityError`, never an empty
successful result.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping

from janus.application.ports.clock import Clock
from janus.application.ports.repositories import UnitOfWorkFactory
from janus.domain.common.errors import UnsupportedEntityError
from janus.domain.common.time import ensure_aware
from janus.domain.data_foundation.capabilities import (
    DataCapability,
    ProviderCapabilities,
    ProviderCapabilityChange,
    capability_change,
)

__all__ = ["ProviderCapabilityService"]


class ProviderCapabilityService:
    """Declare, audit and gate on provider data capabilities."""

    __slots__ = ("_clock", "_uow_factory")

    def __init__(self, *, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def declare(
        self,
        *,
        provider_id: str,
        supported: Iterable[DataCapability],
        reason: str = "",
        notes: str = "",
        dataset_versions: Mapping[str, str] | None = None,
    ) -> ProviderCapabilities:
        """Append a capability declaration (idempotent for an identical one)."""
        declaration = ProviderCapabilities(
            provider_id=provider_id,
            supported=frozenset(supported),
            declared_at=ensure_aware(self._clock.now(), "declared_at"),
            declaration_reason=reason,
            notes=notes,
            dataset_versions=dict(dataset_versions or {}),
        )
        with self._uow_factory() as uow:
            previous = uow.provider_capabilities.latest(provider_id)
            created = uow.provider_capabilities.append(declaration)
            uow.commit()
        if not created and previous is not None:
            return previous
        return declaration

    def latest(self, provider_id: str) -> ProviderCapabilities | None:
        with self._uow_factory() as uow:
            return uow.provider_capabilities.latest(provider_id)

    def require(self, provider_id: str, capability: DataCapability) -> ProviderCapabilities:
        """Require a declared capability; raises when it cannot be proven."""
        declaration = self.latest(provider_id)
        if declaration is None:
            raise UnsupportedEntityError(
                "provider has no capability declaration; refusing to assume support",
                provider_id=provider_id,
                capability=capability.value,
            )
        declaration.require(capability)
        return declaration

    def history(self, provider_id: str, *, limit: int = 20) -> tuple[ProviderCapabilities, ...]:
        with self._uow_factory() as uow:
            return tuple(uow.provider_capabilities.list_declarations(provider_id, limit=limit))

    def changes(self, provider_id: str, *, limit: int = 20) -> tuple[ProviderCapabilityChange, ...]:
        """Deterministic diffs between consecutive declarations, newest first."""
        declarations = self.history(provider_id, limit=limit + 1)
        if len(declarations) < 2:
            return ()
        changes: list[ProviderCapabilityChange] = []
        # ``history`` returns newest first: diff newer against its predecessor.
        for newer, older in itertools.pairwise(declarations):
            changes.append(capability_change(older, newer))
        return tuple(changes)
