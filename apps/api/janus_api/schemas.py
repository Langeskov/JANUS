"""Pydantic transport schemas for the Phase 0 HTTP API.

Conventions
-----------
* **Money and weights are decimal strings**, never JSON floats: every value
  coming from a container DTO is already a decimal string and stays a ``str``
  here. Request weights are accepted as JSON numbers or strings but are handed
  to the domain as strings (``model_dump(mode="json")``).
* Request models use ``extra="forbid"`` so unknown fields -- ``mode: "live"``,
  ``add_capital``, ``risk_policy_override``, credentials, raw strings that do
  not belong to the contract -- are rejected with a 422 before any container
  call.
* There is no field anywhere in this module for strategy source code, shell
  commands, SQL, file paths to execute, or credentials.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Self

from janus.application.container import (
    AccountSummary,
    AuditEntry,
    ComponentHealth,
    CycleResult,
    HealthReport,
    OrderSummary,
    PositionSummary,
    ProposalOutcome,
    RebuildResult,
)
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AccountOut",
    "AuditOut",
    "CycleOut",
    "CycleRunIn",
    "HealthComponentOut",
    "HealthOut",
    "KillSwitchIn",
    "OrderOut",
    "PositionOut",
    "ProposalIn",
    "ProposalOut",
    "RebuildOut",
    "ResumeIn",
    "TargetWeightIn",
]


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
class PositionOut(BaseModel):
    """A valued paper position, with money as decimal strings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: int
    available_quantity: int
    average_cost: str
    market_value: str
    weight: str
    price_is_stale: bool

    @classmethod
    def from_dto(cls, dto: PositionSummary) -> Self:
        return cls(
            symbol=dto.symbol,
            quantity=dto.quantity,
            available_quantity=dto.available_quantity,
            average_cost=dto.average_cost,
            market_value=dto.market_value,
            weight=dto.weight,
            price_is_stale=dto.price_is_stale,
        )


class AccountOut(BaseModel):
    """Current paper-account state plus valuation (all money as strings)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    mode: str
    state: str
    initial_capital: str
    cash: str
    nav: str
    cash_weight: str
    valuation_date: str
    as_of: str
    positions: list[PositionOut] = Field(default_factory=list)
    pause_reason: str | None = None
    data_snapshot_id: str | None = None
    rule_set_id: str | None = None

    @classmethod
    def from_dto(cls, dto: AccountSummary) -> Self:
        return cls(
            account_id=dto.account_id,
            mode=dto.mode,
            state=dto.state,
            initial_capital=dto.initial_capital,
            cash=dto.cash,
            nav=dto.nav,
            cash_weight=dto.cash_weight,
            valuation_date=dto.valuation_date,
            as_of=dto.as_of,
            positions=[PositionOut.from_dto(position) for position in dto.positions],
            pause_reason=dto.pause_reason,
            data_snapshot_id=dto.data_snapshot_id,
            rule_set_id=dto.rule_set_id,
        )


class HealthComponentOut(BaseModel):
    """Health of one component; never contains credentials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ok: bool
    detail: str = ""

    @classmethod
    def from_dto(cls, dto: ComponentHealth) -> Self:
        return cls(name=dto.name, ok=dto.ok, detail=dto.detail)


class HealthOut(BaseModel):
    """Overall health report; never contains credentials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checked_at: str
    detail: str = ""
    components: list[HealthComponentOut] = Field(default_factory=list)

    @classmethod
    def from_dto(cls, dto: HealthReport) -> Self:
        return cls(
            ok=dto.ok,
            checked_at=dto.checked_at.isoformat(),
            detail=dto.detail,
            components=[HealthComponentOut.from_dto(item) for item in dto.components],
        )


class ProposalOut(BaseModel):
    """Outcome of submitting an allocation proposal.

    A rejection is a normal structured outcome; the API maps it to HTTP 422 with
    the stable violation codes. Money/weights remain decimal strings.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    violations: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    decision_id: str | None = None
    proposal_id: str | None = None
    target_weights: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_dto(cls, dto: ProposalOutcome) -> Self:
        return cls(
            accepted=dto.accepted,
            violations=list(dto.violations),
            messages=list(dto.messages),
            decision_id=dto.decision_id,
            proposal_id=dto.proposal_id,
            target_weights=dict(dto.target_weights),
        )


class CycleOut(BaseModel):
    """Outcome of one daily paper-trading cycle (money as strings)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: str
    orders_proposed: int
    orders_rejected: int
    orders_filled: int
    fills: int
    cash_after: str
    nav_after: str
    account_state: str
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def from_dto(cls, dto: CycleResult) -> Self:
        return cls(
            trade_date=dto.trade_date,
            orders_proposed=dto.orders_proposed,
            orders_rejected=dto.orders_rejected,
            orders_filled=dto.orders_filled,
            fills=dto.fills,
            cash_after=dto.cash_after,
            nav_after=dto.nav_after,
            account_state=dto.account_state,
            notes=list(dto.notes),
        )


class OrderOut(BaseModel):
    """A paper order (prices as decimal strings, never floats)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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

    @classmethod
    def from_dto(cls, dto: OrderSummary) -> Self:
        return cls(
            order_id=dto.order_id,
            symbol=dto.symbol,
            side=dto.side,
            quantity=dto.quantity,
            filled_quantity=dto.filled_quantity,
            status=dto.status,
            target_trade_date=dto.target_trade_date,
            created_at=dto.created_at,
            limit_price=dto.limit_price,
            rejection_reason=dto.rejection_reason,
            rule_set_id=dto.rule_set_id,
            rule_set_version=dto.rule_set_version,
            fill_model_version=dto.fill_model_version,
            decision_id=dto.decision_id,
            data_snapshot_id=dto.data_snapshot_id,
        )


class AuditOut(BaseModel):
    """One decision-lineage row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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

    @classmethod
    def from_dto(cls, dto: AuditEntry) -> Self:
        return cls(
            decision_id=dto.decision_id,
            decision_type=dto.decision_type,
            created_at=dto.created_at,
            outcome=dto.outcome,
            executable=dto.executable,
            strategy_id=dto.strategy_id,
            strategy_version=dto.strategy_version,
            proposal_id=dto.proposal_id,
            data_snapshot_id=dto.data_snapshot_id,
            rule_set_id=dto.rule_set_id,
            model_id=dto.model_id,
        )


class RebuildOut(BaseModel):
    """Proof that the account rebuilds exactly from its immutable history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    stored_cash: str
    rebuilt_cash: str
    matches: bool
    cash_entry_count: int
    position_event_count: int
    realized_pnl: str
    stored_positions: dict[str, int] = Field(default_factory=dict)
    rebuilt_positions: dict[str, int] = Field(default_factory=dict)
    mismatches: list[str] = Field(default_factory=list)

    @classmethod
    def from_dto(cls, dto: RebuildResult) -> Self:
        return cls(
            account_id=dto.account_id,
            stored_cash=dto.stored_cash,
            rebuilt_cash=dto.rebuilt_cash,
            matches=dto.matches,
            cash_entry_count=dto.cash_entry_count,
            position_event_count=dto.position_event_count,
            realized_pnl=dto.realized_pnl,
            stored_positions=dict(dto.stored_positions),
            rebuilt_positions=dict(dto.rebuilt_positions),
            mismatches=list(dto.mismatches),
        )


# ---------------------------------------------------------------------------
# Requests (extra="forbid"; no execution, policy, capital or credential fields)
# ---------------------------------------------------------------------------
class TargetWeightIn(BaseModel):
    """One target weight in a submitted proposal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    weight: Decimal


class ProposalIn(BaseModel):
    """A structured allocation proposal.

    Deliberately contains **no** field for execution, risk-policy changes, capital
    changes, credentials, shell commands, SQL, or strategy source code: any
    request carrying such a field is rejected by ``extra="forbid"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    strategy_id: str = Field(min_length=1, max_length=128)
    strategy_version: str = Field(min_length=1, max_length=64)
    regime: str = Field(default="unknown", min_length=1, max_length=32)
    target_weights: tuple[TargetWeightIn, ...]
    horizon_days: int = Field(ge=1, le=365)
    thesis: str = Field(default="", max_length=2000)
    invalidation_conditions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    data_snapshot_id: str | None = Field(default=None, max_length=128)
    model_id: str | None = Field(default=None, max_length=128)
    prompt_version: str | None = Field(default=None, max_length=128)


class CycleRunIn(BaseModel):
    """Optional body for ``POST /cycles/run``; no mode or policy override."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date | None = None
    account_id: str | None = Field(default=None, min_length=1, max_length=64)


class PauseIn(BaseModel):
    """Administrative pause/resume request; a reason is required and audited."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    account_id: str | None = Field(default=None, min_length=1, max_length=64)


class KillSwitchIn(PauseIn):
    """Body for ``POST /admin/kill-switch``."""


class ResumeIn(PauseIn):
    """Body for ``POST /admin/resume``."""
