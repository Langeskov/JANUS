"""Paper execution gateway.

The gateway is the *only* component allowed to move an order beyond
``RISK_APPROVED``. It refuses raw model text, unknown objects, and any order that
did not pass the deterministic risk controller (Phase 0 spec s6.4, s9.10,
s11 P0-E).
"""

from __future__ import annotations

from datetime import datetime

from janus.domain.common.enums import OrderStatus
from janus.domain.common.errors import InvalidTransitionError, SecurityViolationError
from janus.domain.common.time import ensure_aware
from janus.domain.orders.models import ExecutionFill, PaperOrder
from janus.domain.orders.state_machine import (
    GATEWAY_ADMITTED,
    apply_fill,
    assert_transition,
)
from janus.infrastructure.paper_execution.fill_model import FillProposal

__all__ = ["PaperExecutionGateway"]


class PaperExecutionGateway:
    """Deterministic in-process paper gateway (no broker, no network)."""

    __slots__ = ()

    def _require_admissible(self, order: object) -> PaperOrder:
        if not isinstance(order, PaperOrder):
            raise SecurityViolationError(
                "the paper gateway accepts only PaperOrder objects, never raw text",
                received=type(order).__name__,
            )
        if order.status not in GATEWAY_ADMITTED:
            raise InvalidTransitionError(
                "only RISK_APPROVED orders may reach the paper gateway",
                order_id=order.order_id,
                status=order.status.value,
            )
        return order

    def submit(self, order: PaperOrder, *, at: datetime) -> PaperOrder:
        """Move an approved order to ``SUBMITTED_PAPER``."""
        ensure_aware(at, "at")
        admissible = self._require_admissible(order)
        assert_transition(admissible.status, OrderStatus.SUBMITTED_PAPER)
        return order.model_copy(update={"status": OrderStatus.SUBMITTED_PAPER, "updated_at": at})

    def apply_fill(self, order: PaperOrder, proposal: FillProposal, *, at: datetime) -> PaperOrder:
        """Accumulate a fill proposal onto the order."""
        ensure_aware(at, "at")
        if not proposal.filled:
            raise InvalidTransitionError(
                "the fill proposal produced no execution",
                order_id=order.order_id,
                reason=str(proposal.skip_reason),
            )
        fill = proposal.require_fill()
        return apply_fill(order, fill, at=at)

    def cancel(self, order: PaperOrder, *, at: datetime, reason: str) -> PaperOrder:
        ensure_aware(at, "at")
        assert_transition(order.status, OrderStatus.CANCELLED)
        return order.model_copy(
            update={
                "status": OrderStatus.CANCELLED,
                "updated_at": at,
                "terminal_at": at,
                "cancel_reason": reason,
            }
        )

    def expire(
        self, order: PaperOrder, *, at: datetime, reason: str = "validity expired"
    ) -> PaperOrder:
        ensure_aware(at, "at")
        assert_transition(order.status, OrderStatus.EXPIRED)
        return order.model_copy(
            update={
                "status": OrderStatus.EXPIRED,
                "updated_at": at,
                "terminal_at": at,
                "cancel_reason": reason,
            }
        )

    def record_fill_reference(self, fill: ExecutionFill) -> ExecutionFill:
        """Sanity-check a fill before it reaches the ledger."""
        if fill.fill_model_version == "":
            raise InvalidTransitionError("a fill must record its fill-model version")
        if fill.rule_set_id == "" or fill.rule_set_version == "":
            raise InvalidTransitionError("a fill must record its rule-set version")
        return fill
