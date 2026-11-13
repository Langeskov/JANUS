"""Fail-closed provider capability gate.

PR-001 §5: a provider must be able to state, auditably, *what it can supply*.
The dangerous failure mode is an adapter that answers an unsupported request with
an empty successful result: an empty result is indistinguishable from "the
market genuinely had no data". Adapters therefore declare their capabilities
once and call :func:`require_capability` before serving a dataset; a missing
declaration raises :class:`~janus.domain.common.errors.UnsupportedEntityError`.

Kept deliberately trivial: this is a one-line delegation to
:meth:`ProviderCapabilities.require`, not a framework.
"""

from __future__ import annotations

from datetime import UTC, datetime

from janus.domain.data_foundation.capabilities import (
    DataCapability,
    ProviderCapabilities,
)

__all__ = ["PHASE0_DECLARATION_AT", "require_capability"]

PHASE0_DECLARATION_AT: datetime = datetime(2026, 11, 5, tzinfo=UTC)
"""Fixed declaration instant used by the Phase 0 capability declarations.

Deliberately a module constant rather than ``datetime.now()``: capability
declarations carry a deterministic ``declaration_id`` (a content hash), so
re-running an audit or a test must produce identical identifiers. This is an
administrative declaration timestamp -- it is **not** evidence about when any
market data was observed (see ADR-0009). Fixture users may inject their own
``declared_at`` when they need a different declaration time.
"""


def require_capability(capabilities: ProviderCapabilities, capability: DataCapability) -> None:
    """Refuse to serve ``capability`` unless the provider declared it.

    Delegates to :meth:`ProviderCapabilities.require`, which raises
    :class:`~janus.domain.common.errors.UnsupportedEntityError` for an undeclared
    capability. Call this before producing data, never after.
    """
    capabilities.require(capability)
