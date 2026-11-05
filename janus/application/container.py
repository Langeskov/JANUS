"""Application facade and transfer objects.

The CLI and the HTTP API talk to JANUS exclusively through the
:class:`JanusContainer` protocol so that no transport layer ever touches a
repository, a SQLAlchemy session, or a provider SDK.

The composition root that *builds* a container lives in
``janus.infrastructure.app_container.build_container`` -- transport code may call
that factory, but must only depend on the protocol and DTOs declared here.

Conventions
-----------
* All money/weight values are **decimal strings** in DTOs (never JSON floats).
* Datetimes are ISO-8601 strings in the market/UTC timezone they were recorded
  with; ``AccountSummary.as_of`` is UTC.
* A rejection is a normal, structured outcome -- not an exception.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from janus.application.ports.clock import Clock
from janus.domain.strategy.proposal import AllocationProposal

__all__ = [
    "AccountSummary",
    "AuditEntry",
    "ComponentHealth",
    "CycleResult",
    "HealthReport",
    "JanusContainer",
    "OrderSummary",
    "PositionSummary",
    "ProposalOutcome",
    "RebuildResult",
]


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """Health of one component of the running system."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Overall system health. Must never include credentials."""

    ok: bool
    checked_at: datetime
    components: tuple[ComponentHealth, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at.isoformat(),
            "detail": self.detail,
            "components": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.components
            ],
        }


@dataclass(frozen=True, slots=True)
class PositionSummary:
    """A valued position for display/transport."""

    symbol: str
    quantity: int
    available_quantity: int
    average_cost: str
    market_value: str
    weight: str
    price_is_stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "available_quantity": self.available_quantity,
            "average_cost": self.average_cost,
            "market_value": self.market_value,
            "weight": self.weight,
            "price_is_stale": self.price_is_stale,
        }


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """Current account state plus valuation."""

    account_id: str
    mode: str
    state: str
    initial_capital: str
    cash: str
    nav: str
    cash_weight: str
    valuation_date: str
    as_of: str
    positions: tuple[PositionSummary, ...] = ()
    pause_reason: str | None = None
    data_snapshot_id: str | None = None
    rule_set_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "mode": self.mode,
            "state": self.state,
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "nav": self.nav,
            "cash_weight": self.cash_weight,
            "valuation_date": self.valuation_date,
            "as_of": self.as_of,
            "positions": [p.as_dict() for p in self.positions],
            "pause_reason": self.pause_reason,
            "data_snapshot_id": self.data_snapshot_id,
            "rule_set_id": self.rule_set_id,
        }


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """Result of submitting an allocation proposal."""

    accepted: bool
    violations: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()
    decision_id: str | None = None
    proposal_id: str | None = None
    target_weights: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "violations": list(self.violations),
            "messages": list(self.messages),
            "decision_id": self.decision_id,
            "proposal_id": self.proposal_id,
            "target_weights": dict(self.target_weights),
        }


@dataclass(frozen=True, slots=True)
class OrderSummary:
    """A paper order for display/transport."""

    order_id: str
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    status: str
    target_trade_date: str
    created_at: str
    limit_price: str | None = None
    rejection_reason: str | None = None
    rule_set_id: str = ""
    rule_set_version: str = ""
    fill_model_version: str = ""
    decision_id: str | None = None
    data_snapshot_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "status": self.status,
            "target_trade_date": self.target_trade_date,
            "created_at": self.created_at,
            "limit_price": self.limit_price,
            "rejection_reason": self.rejection_reason,
            "rule_set_id": self.rule_set_id,
            "rule_set_version": self.rule_set_version,
            "fill_model_version": self.fill_model_version,
            "decision_id": self.decision_id,
            "data_snapshot_id": self.data_snapshot_id,
        }


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Outcome of one daily paper-trading cycle."""

    trade_date: str
    orders_proposed: int
    orders_rejected: int
    orders_filled: int
    fills: int
    cash_after: str
    nav_after: str
    account_state: str
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "orders_proposed": self.orders_proposed,
            "orders_rejected": self.orders_rejected,
            "orders_filled": self.orders_filled,
            "fills": self.fills,
            "cash_after": self.cash_after,
            "nav_after": self.nav_after,
            "account_state": self.account_state,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class RebuildResult:
    """Proof that the account rebuilds exactly from its immutable history."""

    account_id: str
    stored_cash: str
    rebuilt_cash: str
    matches: bool
    cash_entry_count: int
    position_event_count: int
    realized_pnl: str
    stored_positions: Mapping[str, int] = field(default_factory=dict)
    rebuilt_positions: Mapping[str, int] = field(default_factory=dict)
    mismatches: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "stored_cash": self.stored_cash,
            "rebuilt_cash": self.rebuilt_cash,
            "matches": self.matches,
            "cash_entry_count": self.cash_entry_count,
            "position_event_count": self.position_event_count,
            "realized_pnl": self.realized_pnl,
            "stored_positions": dict(self.stored_positions),
            "rebuilt_positions": dict(self.rebuilt_positions),
            "mismatches": list(self.mismatches),
        }


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One row of decision lineage for the audit view."""

    decision_id: str
    decision_type: str
    created_at: str
    outcome: str
    executable: bool
    strategy_id: str | None = None
    strategy_version: str | None = None
    proposal_id: str | None = None
    data_snapshot_id: str | None = None
    rule_set_id: str | None = None
    model_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decision_type": self.decision_type,
            "created_at": self.created_at,
            "outcome": self.outcome,
            "executable": self.executable,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "proposal_id": self.proposal_id,
            "data_snapshot_id": self.data_snapshot_id,
            "rule_set_id": self.rule_set_id,
            "model_id": self.model_id,
        }


@runtime_checkable
class JanusContainer(Protocol):
    """The complete Phase 0 application surface.

    Every method is deterministic given the same database + clock + fixtures.
    """

    def health(self) -> HealthReport: ...

    def initialize_account(self) -> AccountSummary:
        """Create the configured fixed-capital paper account (idempotent)."""
        ...

    def account_summary(self, *, account_id: str | None = None) -> AccountSummary: ...

    def ingest_fixture_data(self, *, days: int = 20) -> dict[str, int]:
        """Load deterministic fixture bars/instruments; returns counts."""
        ...

    def submit_proposal(
        self,
        proposal: AllocationProposal | Mapping[str, Any],
        *,
        account_id: str | None = None,
    ) -> ProposalOutcome:
        """Validate a proposal and record its decision lineage.

        Never executes anything: an accepted proposal only becomes executable
        through :meth:`run_daily_cycle`.
        """
        ...

    def run_daily_cycle(
        self,
        *,
        trade_date: date | None = None,
        account_id: str | None = None,
    ) -> CycleResult:
        """Turn the latest accepted proposal into risk-checked paper orders."""
        ...

    def kill_switch(self, reason: str, *, account_id: str | None = None) -> AccountSummary:
        """Pause the account. Never liquidates positions."""
        ...

    def resume(self, reason: str, *, account_id: str | None = None) -> AccountSummary: ...

    def rebuild_from_ledger(self, *, account_id: str | None = None) -> RebuildResult: ...

    def list_orders(
        self, *, account_id: str | None = None, limit: int = 50
    ) -> tuple[OrderSummary, ...]: ...

    def list_audit(self, *, limit: int = 50) -> tuple[AuditEntry, ...]: ...


def build_container(
    *,
    config_dir: Path | str | None = None,
    database_url: str | None = None,
    clock: Clock | None = None,
) -> JanusContainer:
    """Composition-root shim.

    Re-exported for convenience; the concrete factory is
    ``janus.infrastructure.app_container.build_container``.
    """
    from janus.infrastructure.app_container import build_container as _build

    return _build(config_dir=config_dir, database_url=database_url, clock=clock)
