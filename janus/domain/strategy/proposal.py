"""Structured allocation proposal -- the *only* output an agent may produce.

Phase 0 spec s3.3: the LLM emits a validated :class:`AllocationProposal`, never
an executable broker instruction. A proposal is rejected when it contains
unknown or duplicate symbols, out-of-range weights, a bad weight total, an
unsupported strategy/version, stale data references, or any attempt to steer
policy/execution.

This module is pure domain code: callers supply the facts (eligible symbols,
known snapshots, registry, clock) so nothing here needs a repository.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from janus.domain.common.errors import NotFoundError, ValidationError
from janus.domain.common.ids import new_id
from janus.domain.common.money import CASH_SYMBOL
from janus.domain.common.time import ensure_aware
from janus.domain.portfolio.models import TargetAllocation, TargetWeight
from janus.domain.strategy.models import RegimeClass, StrategyRegistry

__all__ = [
    "AllocationProposal",
    "ProposalValidationResult",
    "ProposalViolation",
    "find_prohibited_content",
    "validate_proposal",
]

MAX_THESIS_CHARS = 2000
MAX_LIST_ITEMS = 10
MAX_ITEM_CHARS = 500
MAX_EVIDENCE_REFS = 50
MAX_TARGET_WEIGHTS = 50
MAX_HORIZON_DAYS = 365

_PROPOSED_WEIGHT_TOLERANCE = Decimal("0.0001")

_PROHIBITED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("shell", re.compile(r"(?i)(rm\s+-rf|curl\s+http|wget\s+http|\bpowershell\b|\bbash\b)")),
    ("sql", re.compile(r"(?i)(drop\s+table|delete\s+from|update\s+ledger|insert\s+into)")),
    (
        "execution_tool",
        re.compile(
            r"(?i)(execute_order|place_order|submit_order_tool|"
            r"broker_api|trade_now)"
        ),
    ),
    (
        "policy_edit",
        re.compile(
            r"(?i)(set_risk_policy|change_risk_limit|edit_policy|"
            r"update_config|disable_kill_switch)"
        ),
    ),
    (
        "capital_edit",
        re.compile(
            r"(?i)(add_capital|deposit|withdraw|top_up|set_balance|"
            r"edit_ledger)"
        ),
    ),
    ("risk_self_approval", re.compile(r"(?i)(approve_risk|override_risk|bypass_risk)")),
    (
        "secret_access",
        re.compile(
            r"(?i)(api_key|secret_key|\.env\b|tushare_token|"
            r"deepseek_api_key)"
        ),
    ),
    (
        "tool_directive",
        re.compile(
            r"(?i)(\"__tool__\"|<\|tool|tool_call\s*:|\bBEGIN SYSTEM\b|"
            r"\bSYSTEM PROMPT\b)"
        ),
    ),
)
"""Defence-in-depth patterns. Prompt-injection classifiers are *not* a security
boundary; these checks only make blatant attempts produce no order."""


class ProposalViolation(StrEnum):
    """Stable rejection reasons (stored verbatim in the decision record)."""

    MALFORMED = "MALFORMED"
    EMPTY_PROPOSAL = "EMPTY_PROPOSAL"
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
    INELIGIBLE_INSTRUMENT = "INELIGIBLE_INSTRUMENT"
    DUPLICATE_SYMBOL = "DUPLICATE_SYMBOL"
    WEIGHT_OUT_OF_RANGE = "WEIGHT_OUT_OF_RANGE"
    WEIGHT_SUM_MISMATCH = "WEIGHT_SUM_MISMATCH"
    UNKNOWN_STRATEGY = "UNKNOWN_STRATEGY"
    STRATEGY_DISABLED = "STRATEGY_DISABLED"
    STALE_DATA = "STALE_DATA"
    MISSING_DATA_SNAPSHOT = "MISSING_DATA_SNAPSHOT"
    UNKNOWN_EVIDENCE_REF = "UNKNOWN_EVIDENCE_REF"
    INVALID_HORIZON = "INVALID_HORIZON"
    PROHIBITED_INSTRUCTION = "PROHIBITED_INSTRUCTION"
    FIELD_TOO_LONG = "FIELD_TOO_LONG"
    MISSING_THESIS = "MISSING_THESIS"
    FUTURE_PROPOSAL = "FUTURE_PROPOSAL"


class ProposalValidationResult(BaseModel):
    """Validation outcome, persisted with the proposal for audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    violations: tuple[ProposalViolation, ...] = ()
    messages: tuple[str, ...] = ()

    @property
    def reason_codes(self) -> list[str]:
        return [v.value for v in self.violations]

    @classmethod
    def ok(cls) -> ProposalValidationResult:
        return cls(valid=True)

    @classmethod
    def rejected(
        cls, violations: Sequence[ProposalViolation], messages: Sequence[str] = ()
    ) -> ProposalValidationResult:
        return cls(valid=False, violations=tuple(violations), messages=tuple(messages))


class AllocationProposal(BaseModel):
    """A structured, schema-validated target allocation.

    Model-shape validation happens here. Semantic validation (does the symbol
    exist, is the strategy registered, is the snapshot stale) happens in
    :func:`validate_proposal` because it needs external facts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str = Field(default_factory=lambda: new_id("proposal"))
    as_of: datetime
    strategy_id: str
    strategy_version: str
    regime: RegimeClass = RegimeClass.UNKNOWN
    target_weights: tuple[TargetWeight, ...]
    horizon_days: int
    thesis: str = ""
    invalidation_conditions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()

    data_snapshot_id: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None

    @field_validator("as_of")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "as_of")

    @field_validator("horizon_days")
    @classmethod
    def _horizon(cls, value: int) -> int:
        if value < 1 or value > MAX_HORIZON_DAYS:
            raise ValidationError(f"horizon_days must be within [1, {MAX_HORIZON_DAYS}]")
        return value

    @field_validator("thesis")
    @classmethod
    def _thesis(cls, value: str) -> str:
        if len(value) > MAX_THESIS_CHARS:
            raise ValidationError("thesis exceeds the maximum allowed length")
        return value

    @field_validator("invalidation_conditions", "uncertainties")
    @classmethod
    def _lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_LIST_ITEMS:
            raise ValidationError(f"too many list items (max {MAX_LIST_ITEMS})")
        for item in value:
            if len(item) > MAX_ITEM_CHARS:
                raise ValidationError("list item exceeds the maximum allowed length")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_EVIDENCE_REFS:
            raise ValidationError(f"too many evidence refs (max {MAX_EVIDENCE_REFS})")
        for item in value:
            if len(item) > MAX_ITEM_CHARS:
                raise ValidationError("evidence ref exceeds the maximum allowed length")
        return value

    @field_validator("target_weights")
    @classmethod
    def _count(cls, value: tuple[TargetWeight, ...]) -> tuple[TargetWeight, ...]:
        if not value:
            raise ValidationError("target_weights must not be empty")
        if len(value) > MAX_TARGET_WEIGHTS:
            raise ValidationError(f"too many target weights (max {MAX_TARGET_WEIGHTS})")
        return value

    @model_validator(mode="after")
    def _weights(self) -> AllocationProposal:
        symbols = [w.symbol for w in self.target_weights]
        if len(symbols) != len(set(symbols)):
            raise ValidationError("duplicate symbol in target_weights")
        total = sum((w.weight for w in self.target_weights), Decimal("0"))
        if abs(total - Decimal("1")) > _PROPOSED_WEIGHT_TOLERANCE:
            raise ValidationError("target_weights must sum to 1 within tolerance", total=str(total))
        return self

    # -- helpers ---------------------------------------------------------
    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(w.symbol for w in self.target_weights)

    @property
    def cash_weight(self) -> Decimal:
        return sum((w.weight for w in self.target_weights if w.symbol == CASH_SYMBOL), Decimal("0"))

    @property
    def invested_weight(self) -> Decimal:
        return Decimal("1") - self.cash_weight

    def to_target_allocation(self) -> TargetAllocation:
        return TargetAllocation(weights=self.target_weights, tolerance=_PROPOSED_WEIGHT_TOLERANCE)

    def all_text(self) -> str:
        """Concatenate every free-text field for prohibited-content scanning."""
        parts: list[str] = [
            self.thesis,
            self.strategy_id,
            self.strategy_version,
            *self.invalidation_conditions,
            *self.evidence_refs,
            *self.uncertainties,
        ]
        return "\n".join(parts)


def find_prohibited_content(text: str) -> list[str]:
    """Return the names of prohibited patterns found in ``text``."""
    hits: list[str] = []
    for name, pattern in _PROHIBITED_PATTERNS:
        if pattern.search(text):
            hits.append(name)
    return hits


def validate_proposal(
    proposal: AllocationProposal,
    *,
    known_symbols: Mapping[str, object] | Sequence[str],
    eligible_symbols: Sequence[str] | None,
    registry: StrategyRegistry,
    evaluation_time: datetime,
    max_proposal_age: timedelta = timedelta(hours=24),
    known_evidence_refs: Sequence[str] | None = None,
    required_data_snapshot: bool = True,
) -> ProposalValidationResult:
    """Semantically validate a proposal against current, trusted facts.

    Fails closed: every uncertain condition produces a rejection.
    """
    violations: list[ProposalViolation] = []
    messages: list[str] = []
    evaluation_time = ensure_aware(evaluation_time, "evaluation_time")

    known = set(known_symbols)
    eligible = set(eligible_symbols) if eligible_symbols is not None else None

    if not proposal.target_weights:
        violations.append(ProposalViolation.EMPTY_PROPOSAL)
        messages.append("proposal contains no target weights")

    for weight in proposal.target_weights:
        if weight.symbol == CASH_SYMBOL:
            continue
        if weight.symbol not in known:
            violations.append(ProposalViolation.UNKNOWN_SYMBOL)
            messages.append(f"unknown symbol {weight.symbol}")
            continue
        if eligible is not None and weight.symbol not in eligible:
            violations.append(ProposalViolation.INELIGIBLE_INSTRUMENT)
            messages.append(f"ineligible instrument {weight.symbol}")

    if not proposal.thesis.strip():
        violations.append(ProposalViolation.MISSING_THESIS)
        messages.append("thesis is required")

    # -- strategy registration ------------------------------------------
    try:
        strategy = registry.get(proposal.strategy_id, proposal.strategy_version)
    except NotFoundError:
        strategy = None
    if strategy is None:
        violations.append(ProposalViolation.UNKNOWN_STRATEGY)
        messages.append(
            f"strategy {proposal.strategy_id}@{proposal.strategy_version} is not registered"
        )
    elif not strategy.status.is_usable:
        violations.append(ProposalViolation.STRATEGY_DISABLED)
        messages.append(
            f"strategy {proposal.strategy_id}@{proposal.strategy_version} is "
            f"{strategy.status.value}"
        )
    elif proposal.horizon_days > strategy.max_horizon_days:
        violations.append(ProposalViolation.INVALID_HORIZON)
        messages.append(
            f"horizon_days {proposal.horizon_days} exceeds strategy maximum "
            f"{strategy.max_horizon_days}"
        )
    else:
        allowed_types = strategy.eligible_instrument_types
        for weight in proposal.target_weights:
            if weight.symbol == CASH_SYMBOL or weight.symbol not in known:
                continue
            instrument = (
                known_symbols.get(weight.symbol) if isinstance(known_symbols, Mapping) else None
            )
            instrument_type = getattr(instrument, "instrument_type", None)
            if instrument_type is not None and instrument_type not in allowed_types:
                violations.append(ProposalViolation.INELIGIBLE_INSTRUMENT)
                messages.append(
                    f"{weight.symbol} type {instrument_type} is not allowed for the strategy"
                )

    # -- freshness -------------------------------------------------------
    if proposal.as_of > evaluation_time + timedelta(minutes=5):
        violations.append(ProposalViolation.FUTURE_PROPOSAL)
        messages.append("proposal as_of is in the future")
    if evaluation_time - proposal.as_of > max_proposal_age:
        violations.append(ProposalViolation.STALE_DATA)
        messages.append("proposal is older than the configured freshness bound")

    if required_data_snapshot and not proposal.data_snapshot_id:
        violations.append(ProposalViolation.MISSING_DATA_SNAPSHOT)
        messages.append("proposal must reference a data snapshot")

    if known_evidence_refs is not None and proposal.evidence_refs:
        known_refs = set(known_evidence_refs)
        unknown = [ref for ref in proposal.evidence_refs if ref not in known_refs]
        if unknown:
            violations.append(ProposalViolation.UNKNOWN_EVIDENCE_REF)
            messages.append(f"unknown evidence refs: {unknown[:3]}")

    # -- injection / policy-edit attempts --------------------------------
    hits = find_prohibited_content(proposal.all_text())
    if hits:
        violations.append(ProposalViolation.PROHIBITED_INSTRUCTION)
        messages.append(f"proposal text contains prohibited directives: {hits}")

    deduped = tuple(dict.fromkeys(violations))
    if deduped:
        return ProposalValidationResult.rejected(deduped, messages)
    return ProposalValidationResult.ok()
