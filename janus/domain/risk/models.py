"""Deterministic risk-control models and policy.

The risk controller is ordinary deterministic code, never an LLM prompt. Its
policy is read-only to all agents (Phase 0 spec s8). This module defines the
policy shape and the decision record; the controller itself lives in
``janus.application.services.risk_service``.

Phase 0 hard restrictions are enforced by *policy validation*: a configuration
that tries to enable leverage, shorting, derivatives, intraday strategies, live
trading or forced liquidation is rejected at load time.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from janus.domain.common.enums import RiskOutcome
from janus.domain.common.errors import UnsupportedEntityError, ValidationError
from janus.domain.common.ids import ensure_id
from janus.domain.common.money import as_decimal
from janus.domain.common.time import ensure_aware

__all__ = [
    "DrawdownPolicy",
    "ExecutionPolicy",
    "RiskCheck",
    "RiskControls",
    "RiskDecision",
    "RiskLimits",
    "RiskPolicy",
    "RiskRuleId",
    "RiskVerdict",
]


class RiskRuleId(StrEnum):
    """Stable identifiers for every deterministic check (audit-friendly)."""

    ENVIRONMENT_PAPER_ONLY = "ENVIRONMENT_PAPER_ONLY"
    ACCOUNT_ACTIVE = "ACCOUNT_ACTIVE"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    INSTRUMENT_KNOWN = "INSTRUMENT_KNOWN"
    INSTRUMENT_ELIGIBLE = "INSTRUMENT_ELIGIBLE"
    RULESET_KNOWN = "RULESET_KNOWN"
    DECISION_REFERENCE = "DECISION_REFERENCE"
    DATA_SNAPSHOT_REFERENCE = "DATA_SNAPSHOT_REFERENCE"
    STRATEGY_VERSION = "STRATEGY_VERSION"
    MARKET_DATA_FRESH = "MARKET_DATA_FRESH"
    DATA_QUALITY = "DATA_QUALITY"
    ORDER_TYPE_SUPPORTED = "ORDER_TYPE_SUPPORTED"
    QUANTITY_INCREMENT = "QUANTITY_INCREMENT"
    PRICE_TICK = "PRICE_TICK"
    PRICE_LIMIT_BAND = "PRICE_LIMIT_BAND"
    SESSION_VALID = "SESSION_VALID"
    SIGNAL_LOOKAHEAD = "SIGNAL_LOOKAHEAD"
    AVAILABLE_CASH = "AVAILABLE_CASH"
    AVAILABLE_QUANTITY = "AVAILABLE_QUANTITY"
    SINGLE_INSTRUMENT_WEIGHT = "SINGLE_INSTRUMENT_WEIGHT"
    MIN_CASH_WEIGHT = "MIN_CASH_WEIGHT"
    DAILY_TURNOVER = "DAILY_TURNOVER"
    MAX_ORDERS_PER_DAY = "MAX_ORDERS_PER_DAY"
    GROSS_EXPOSURE = "GROSS_EXPOSURE"
    SECTOR_EXPOSURE = "SECTOR_EXPOSURE"
    DRAWDOWN_PAUSE = "DRAWDOWN_PAUSE"
    NO_SHORT = "NO_SHORT"
    NO_LEVERAGE = "NO_LEVERAGE"
    NO_DERIVATIVES = "NO_DERIVATIVES"


class ExecutionPolicy(BaseModel):
    """Immutable Phase 0 execution environment switches."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str = "paper_only"
    allow_short: bool = False
    allow_leverage: bool = False
    allow_derivatives: bool = False
    allow_intraday_strategy: bool = False

    @field_validator("environment")
    @classmethod
    def _paper_only(cls, value: str) -> str:
        if value != "paper_only":
            raise UnsupportedEntityError(
                "Phase 0 is paper-only; live execution is refused", environment=value
            )
        return value

    @model_validator(mode="after")
    def _all_restrictions_hold(self) -> ExecutionPolicy:
        enabled = [
            name
            for name in (
                "allow_short",
                "allow_leverage",
                "allow_derivatives",
                "allow_intraday_strategy",
            )
            if getattr(self, name)
        ]
        if enabled:
            raise UnsupportedEntityError(
                "Phase 0 forbids these capabilities; refusing to load policy",
                enabled=enabled,
            )
        return self


class RiskControls(BaseModel):
    """Boolean and data-quality gates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    require_fresh_market_data: bool = True
    require_known_ruleset: bool = True
    require_decision_reference: bool = True
    require_human_for_policy_change: bool = True
    pause_on_data_integrity_failure: bool = True

    max_bar_age_days: int = Field(default=5, ge=1)
    """A daily bar older than this many calendar days is stale."""

    max_quote_age_seconds: int = Field(default=300, ge=1)

    require_sector_attribution: bool = False
    """Whether ``SECTOR_EXPOSURE`` is a mandatory constraint.

    Phase 0's instrument master carries no sector field, so by default the rule
    is explicitly ``NOT_APPLICABLE`` (documented, non-blocking). Setting this to
    ``True`` makes missing attribution ``INDETERMINATE``, which blocks approval:
    an unverified exposure limit must never look like compliance.
    """


class RiskLimits(BaseModel):
    """Exposure/turnover limits. Conservative *development* defaults only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_single_instrument_weight: Decimal = Decimal("0.10")
    max_daily_turnover: Decimal = Decimal("0.20")
    min_cash_weight: Decimal = Decimal("0.20")
    max_orders_per_day: int = 5
    max_gross_exposure: Decimal = Decimal("0.80")
    max_sector_weight: Decimal = Decimal("0.40")
    max_order_notional: Decimal | None = None
    """Optional absolute cap in CNY per order."""

    @field_validator(
        "max_single_instrument_weight",
        "max_daily_turnover",
        "min_cash_weight",
        "max_gross_exposure",
        "max_sector_weight",
    )
    @classmethod
    def _unit_interval(cls, value: Decimal) -> Decimal:
        amount = as_decimal(value)
        if amount < 0 or amount > 1:
            raise ValidationError("risk limit must be within [0, 1]", value=str(value))
        return amount

    @model_validator(mode="after")
    def _coherent(self) -> RiskLimits:
        if self.min_cash_weight + self.max_gross_exposure > Decimal("1") + Decimal("0.0001"):
            raise ValidationError(
                "min_cash_weight and max_gross_exposure are contradictory",
                min_cash_weight=str(self.min_cash_weight),
                max_gross_exposure=str(self.max_gross_exposure),
            )
        if self.max_orders_per_day < 1:
            raise ValidationError("max_orders_per_day must be >= 1")
        return self


class DrawdownPolicy(BaseModel):
    """Drawdown handling. Pauses new orders; never liquidates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pause_new_orders_at: Decimal = Decimal("0.10")
    liquidation_on_trigger: bool = False

    @field_validator("pause_new_orders_at")
    @classmethod
    def _interval(cls, value: Decimal) -> Decimal:
        amount = as_decimal(value)
        if amount <= 0 or amount > 1:
            raise ValidationError("pause_new_orders_at must be within (0, 1]")
        return amount

    @model_validator(mode="after")
    def _never_liquidate(self) -> DrawdownPolicy:
        if self.liquidation_on_trigger:
            raise UnsupportedEntityError(
                "Phase 0 never force-liquidates on a drawdown trigger; "
                "a risk breach pauses new orders only",
            )
        return self


class RiskPolicy(BaseModel):
    """Complete risk policy document (``config/risk_policy.yaml``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "0.1.0"
    execution: ExecutionPolicy = ExecutionPolicy()
    controls: RiskControls = RiskControls()
    limits: RiskLimits = RiskLimits()
    drawdown: DrawdownPolicy = DrawdownPolicy()


class RiskVerdict(StrEnum):
    """Explicit outcome of one deterministic risk check.

    ``passed`` alone cannot express "the check could not be performed", which is
    exactly the situation that must never be reported as compliance.
    """

    PASS = "PASS"
    """The check was performed and the constraint holds."""

    FAIL = "FAIL"
    """The check was performed and the constraint is violated."""

    INDETERMINATE = "INDETERMINATE"
    """Required data was unavailable/insufficient, so compliance is unknown.

    An enabled mandatory constraint in this state blocks approval; it is never
    silently treated as a pass.
    """

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """The rule is explicitly not applicable to this order/configuration.

    Unlike ``INDETERMINATE`` this is a deliberate, documented decision and does
    not block approval.
    """

    @property
    def is_pass(self) -> bool:
        return self is RiskVerdict.PASS or self is RiskVerdict.NOT_APPLICABLE

    @property
    def blocks(self) -> bool:
        return self in {RiskVerdict.FAIL, RiskVerdict.INDETERMINATE}


class RiskCheck(BaseModel):
    """One deterministic check outcome with its verdict and measured value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: RiskRuleId
    verdict: RiskVerdict
    passed: bool
    message: str = ""
    measured: str | None = None
    threshold: str | None = None

    @model_validator(mode="after")
    def _verdict_matches_passed(self) -> RiskCheck:
        expected = self.verdict.is_pass
        if self.passed is not expected:
            raise ValidationError(
                "passed must be True exactly when the verdict is PASS or NOT_APPLICABLE",
                rule_id=self.rule_id.value,
                verdict=self.verdict.value,
                passed=self.passed,
            )
        if self.verdict is RiskVerdict.FAIL and not self.message:
            raise ValidationError(
                "a FAIL verdict requires an explanatory message", rule_id=self.rule_id.value
            )
        return self

    @classmethod
    def ok(
        cls, rule_id: RiskRuleId, *, measured: object = None, threshold: object = None
    ) -> RiskCheck:
        return cls(
            rule_id=rule_id,
            verdict=RiskVerdict.PASS,
            passed=True,
            measured=None if measured is None else str(measured),
            threshold=None if threshold is None else str(threshold),
        )

    @classmethod
    def failed(
        cls,
        rule_id: RiskRuleId,
        message: str,
        *,
        measured: object = None,
        threshold: object = None,
    ) -> RiskCheck:
        return cls(
            rule_id=rule_id,
            verdict=RiskVerdict.FAIL,
            passed=False,
            message=message,
            measured=None if measured is None else str(measured),
            threshold=None if threshold is None else str(threshold),
        )

    @classmethod
    def indeterminate(
        cls,
        rule_id: RiskRuleId,
        message: str,
        *,
        measured: object = None,
        threshold: object = None,
    ) -> RiskCheck:
        """Compliance could not be established; this blocks approval."""
        return cls(
            rule_id=rule_id,
            verdict=RiskVerdict.INDETERMINATE,
            passed=False,
            message=message,
            measured=None if measured is None else str(measured),
            threshold=None if threshold is None else str(threshold),
        )

    @classmethod
    def not_applicable(
        cls,
        rule_id: RiskRuleId,
        message: str = "",
        *,
        measured: object = None,
        threshold: object = None,
    ) -> RiskCheck:
        """The rule does not apply here, by documented policy (does not block)."""
        return cls(
            rule_id=rule_id,
            verdict=RiskVerdict.NOT_APPLICABLE,
            passed=True,
            message=message,
            measured=None if measured is None else str(measured),
            threshold=None if threshold is None else str(threshold),
        )


class RiskDecision(BaseModel):
    """Immutable record of a risk evaluation.

    ``outcome`` must agree with ``checks``: PASS only when every check verdict is
    ``PASS`` or ``NOT_APPLICABLE``. A ``FAIL`` or ``INDETERMINATE`` check blocks
    approval — an unverified mandatory constraint is never reported as success.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_decision_id: str
    order_intent_id: str
    account_id: str
    outcome: RiskOutcome
    checks: tuple[RiskCheck, ...]
    policy_version: str
    decided_at: datetime
    rule_set_id: str | None = None
    rule_set_version: str | None = None
    data_snapshot_id: str | None = None
    summary: str = ""

    @model_validator(mode="after")
    def _validate(self) -> RiskDecision:
        ensure_id(self.risk_decision_id, "risk_decision_id")
        ensure_id(self.order_intent_id, "order_intent_id")
        ensure_aware(self.decided_at, "decided_at")
        if not self.checks:
            raise ValidationError("a risk decision must contain at least one check")
        all_passed = all(check.verdict.is_pass for check in self.checks)
        if self.outcome is RiskOutcome.PASS and not all_passed:
            raise ValidationError(
                "PASS outcome contradicts a blocking check verdict",
                blocking=[
                    f"{check.rule_id.value}:{check.verdict.value}"
                    for check in self.checks
                    if check.verdict.blocks
                ],
            )
        if self.outcome is RiskOutcome.REJECT and all_passed:
            raise ValidationError("REJECT outcome contradicts a fully passing check set")
        return self

    @property
    def failed_rule_ids(self) -> tuple[RiskRuleId, ...]:
        """Rules that actively failed or could not be verified (all blocking)."""
        return tuple(check.rule_id for check in self.checks if check.verdict.blocks)

    @property
    def indeterminate_rule_ids(self) -> tuple[RiskRuleId, ...]:
        """Mandatory rules whose compliance could not be established."""
        return tuple(
            check.rule_id for check in self.checks if check.verdict is RiskVerdict.INDETERMINATE
        )

    @property
    def not_applicable_rule_ids(self) -> tuple[RiskRuleId, ...]:
        return tuple(
            check.rule_id for check in self.checks if check.verdict is RiskVerdict.NOT_APPLICABLE
        )

    def failed_reasons(self) -> list[str]:
        return [
            f"{check.rule_id.value} [{check.verdict.value}]: {check.message}"
            for check in self.checks
            if check.verdict.blocks
        ]

    @property
    def approved(self) -> bool:
        return self.outcome is RiskOutcome.PASS
