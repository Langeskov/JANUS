"""Explicit paper-order state machine.

Phase 0 spec s6.4 requires the minimum lifecycle::

    PROPOSED -> RISK_REJECTED | RISK_APPROVED -> SUBMITTED_PAPER
             -> PARTIALLY_FILLED | FILLED | EXPIRED | CANCELLED

Only ``RISK_APPROVED`` orders may reach the paper gateway, and invalid
transitions must fail loudly.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from janus.domain.common.enums import OrderStatus
from janus.domain.common.errors import InvalidTransitionError
from janus.domain.common.time import ensure_aware
from janus.domain.orders.models import ExecutionFill, PaperOrder

__all__ = [
    "ALLOWED_TRANSITIONS",
    "apply_fill",
    "assert_transition",
    "can_transition",
    "transition",
]


ALLOWED_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PROPOSED: frozenset(
        {OrderStatus.RISK_REJECTED, OrderStatus.RISK_APPROVED, OrderStatus.CANCELLED}
    ),
    OrderStatus.RISK_REJECTED: frozenset(),
    OrderStatus.RISK_APPROVED: frozenset(
        {OrderStatus.SUBMITTED_PAPER, OrderStatus.EXPIRED, OrderStatus.CANCELLED}
    ),
    OrderStatus.SUBMITTED_PAPER: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}
"""Complete transition table. Any transition not listed here is invalid."""

GATEWAY_ADMITTED: frozenset[OrderStatus] = frozenset({OrderStatus.RISK_APPROVED})
"""Statuses the paper execution gateway is allowed to consume."""


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    """True when ``current -> target`` is an allowed lifecycle edge."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: OrderStatus, target: OrderStatus) -> None:
    """Raise :class:`InvalidTransitionError` for a forbidden edge."""
    if not can_transition(current, target):
        raise InvalidTransitionError(
            f"illegal order transition {current.value} -> {target.value}",
            from_status=current.value,
            to_status=target.value,
        )


def _apply(order: PaperOrder, patch: dict[str, object]) -> PaperOrder:
    """Build a revised order *with full re-validation*.

    ``pydantic.model_copy`` skips validators, which would let an invariant
    violation slip through a lifecycle transition. Re-validating is cheap here
    and keeps the state machine honest.
    """
    data: dict[str, object] = dict(order.model_dump())
    data.update(patch)
    return PaperOrder.model_validate(data)


def transition(
    order: PaperOrder,
    target: OrderStatus,
    *,
    at: datetime,
    **updates: object,
) -> PaperOrder:
    """Return a new order in ``target`` status, validating the edge.

    ``updates`` may only carry fields that are part of the lifecycle
    (approval timestamp, rejection reason, ...). Domain fields such as symbol,
    side or quantity are immutable once created.
    """
    ensure_aware(at, "at")
    assert_transition(order.status, target)

    immutable = {
        "order_id",
        "intent_id",
        "account_id",
        "symbol",
        "side",
        "quantity",
        "order_type",
        "limit_price",
        "validity",
        "created_at",
        "earliest_execution_date",
        "rule_set_id",
        "rule_set_version",
        "fee_schedule_id",
        "fee_schedule_version",
        "fill_model_version",
        "data_snapshot_id",
        "decision_id",
        "strategy_id",
        "strategy_version",
    }
    forbidden = immutable.intersection(updates)
    if forbidden:
        raise InvalidTransitionError(
            "order identity fields are immutable", fields=sorted(forbidden)
        )

    patch: dict[str, object] = dict(updates)
    patch["status"] = target
    patch["updated_at"] = at

    if target is OrderStatus.RISK_APPROVED:
        patch.setdefault("approved_at", at)
    if target is OrderStatus.SUBMITTED_PAPER:
        patch.setdefault("submitted_at", at)
    if target.is_terminal:
        patch.setdefault("terminal_at", at)
    if target is OrderStatus.RISK_REJECTED:
        patch.setdefault("rejection_reason", "risk controller rejected the order")

    return _apply(order, patch)


def apply_fill(
    order: PaperOrder,
    fill: ExecutionFill,
    *,
    at: datetime,
) -> PaperOrder:
    """Accumulate a fill onto an order, enforcing over-fill protection.

    Fills are only legal from ``SUBMITTED_PAPER`` or ``PARTIALLY_FILLED``.
    """
    ensure_aware(at, "at")
    if fill.order_id != order.order_id:
        raise InvalidTransitionError(
            "fill does not belong to this order", fill_id=fill.fill_id, order_id=order.order_id
        )
    if order.status not in {OrderStatus.SUBMITTED_PAPER, OrderStatus.PARTIALLY_FILLED}:
        raise InvalidTransitionError(
            "fills are only accepted on submitted or partially filled orders",
            status=order.status.value,
            fill_id=fill.fill_id,
        )
    if fill.quantity <= 0:  # pragma: no cover - model validated
        raise InvalidTransitionError("fill quantity must be positive", fill_id=fill.fill_id)

    new_filled = order.filled_quantity + fill.quantity
    if new_filled > order.quantity:
        raise InvalidTransitionError(
            "fill would over-fill the order",
            order_id=order.order_id,
            filled=order.filled_quantity,
            fill_quantity=fill.quantity,
            quantity=order.quantity,
        )
    target = OrderStatus.FILLED if new_filled == order.quantity else OrderStatus.PARTIALLY_FILLED
    assert_transition(order.status, target)
    return _apply(
        order,
        {
            "filled_quantity": new_filled,
            "status": target,
            "updated_at": at,
            "terminal_at": at if target is OrderStatus.FILLED else None,
        },
    )
