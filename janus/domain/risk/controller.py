"""Deterministic, independent pre-trade risk controller (Phase 0 spec s8).

The controller is **ordinary deterministic code**, never an LLM prompt. Its
policy (:class:`~janus.domain.risk.models.RiskPolicy`) is read-only to every
agent. Every rejection is expressed as a
:class:`~janus.domain.risk.models.RiskCheck` carrying a stable
:class:`~janus.domain.risk.models.RiskRuleId`, and :meth:`RiskController.evaluate`
returns a :class:`~janus.domain.risk.models.RiskDecision` instead of raising for
a business rejection. Nothing here performs I/O: the caller supplies the
account, portfolio, resolved rules, calendar, bar, eligibility, references and
aggregate counters.

Design notes
------------
* Fail closed: a missing/unusable input makes its rule fail; nothing is guessed.
* Money and weights are :class:`~decimal.Decimal` throughout (no floats).
* Every relevant rule id is emitted on every evaluation so the decision record
  is self-describing and replayable (spec s10).
* ``evaluate`` raises :class:`~janus.domain.common.errors.ValidationError` only
  for *programmer errors* (naive datetimes, negative counters), never for a
  business rejection.

Two optional keyword arguments extend the mandated signature in a
backwards-compatible way:

``sector``
    The instrument's sector/industry key, used by ``SECTOR_EXPOSURE``. Phase 0's
    instrument master carries no sector field, so callers that do have sector
    attribution pass it here; when omitted the check uses ``intent.symbol`` as
    the key of ``sector_weights`` and, if that is absent too, emits a passing
    check documenting that sector attribution was unavailable.

``estimated_fees``
    The caller's computed buy-side fee estimate in CNY. When omitted the
    controller reserves a conservative upper bound
    (:data:`DEFAULT_BUY_FEE_RATE` of notional, at least
    :data:`DEFAULT_MIN_COMMISSION`). Fees are only ever *reserved*, never
    charged, by the risk layer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from janus.domain.common.enums import (
    AccountState,
    AdjustmentMode,
    DataQualityStatus,
    InstrumentType,
    OrderSide,
    OrderType,
    RiskOutcome,
)
from janus.domain.common.errors import JanusError, ValidationError
from janus.domain.common.ids import new_id
from janus.domain.common.money import quantize_cny
from janus.domain.common.time import ensure_aware, market_date
from janus.domain.instruments.eligibility import EligibilityResult
from janus.domain.instruments.models import Instrument
from janus.domain.market_data.models import MarketBar
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.models import ResolvedTradingRules
from janus.domain.orders.models import OrderIntent
from janus.domain.portfolio.models import AccountMode, PortfolioAccount, PortfolioSnapshot, Position
from janus.domain.risk.models import RiskCheck, RiskDecision, RiskPolicy, RiskRuleId

__all__ = [
    "DEFAULT_BUY_FEE_RATE",
    "DEFAULT_MIN_COMMISSION",
    "RiskController",
]

DEFAULT_BUY_FEE_RATE: Final[Decimal] = Decimal("0.001")
"""Conservative buy-side fee reserve as a fraction of notional (10 bps).

Well above the illustrative Phase 0 fee schedule (commission plus transfer fee),
so affordability is never overstated. The real, versioned fee schedule is applied
by the execution/fee engine at fill time.
"""

DEFAULT_MIN_COMMISSION: Final[Decimal] = Decimal("5.00")
"""Minimum per-order buy fee reserve in CNY (broker minimum commission style)."""

_WEIGHT_QUANTUM: Final[Decimal] = Decimal("0.000001")
"""Weights are compared/recorded at six decimal places."""

_SEMVER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")

_DERIVATIVE_INSTRUMENT_TYPES: Final[frozenset[InstrumentType]] = frozenset(
    {
        InstrumentType.OPTION,
        InstrumentType.FUTURE,
        InstrumentType.CONVERTIBLE_BOND,
        InstrumentType.LEVERAGED_ETF,
        InstrumentType.INVERSE_ETF,
    }
)
"""Instrument families Phase 0 never trades (spec s1.3, s8.1)."""


@dataclass(frozen=True, slots=True)
class _CheckContext:
    """Inputs plus derived plain values shared by every rule check."""

    policy: RiskPolicy
    intent: OrderIntent
    account: PortfolioAccount
    portfolio: PortfolioSnapshot
    rules: ResolvedTradingRules
    calendar: TradingCalendar
    bar: MarketBar | None
    prev_close: Decimal | None
    instrument: Instrument | None
    eligibility: EligibilityResult | None
    known_decision_ids: set[str]
    known_snapshot_ids: set[str]
    orders_today: int
    turnover_today: Decimal
    sector_weights: Mapping[str, Decimal]
    drawn_down: Decimal
    now: datetime
    sector: str | None = None
    estimated_fees: Decimal | None = None

    # -- basic values ----------------------------------------------------
    @property
    def is_buy(self) -> bool:
        return self.intent.side is OrderSide.BUY

    @property
    def nav(self) -> Decimal:
        return self.portfolio.nav.amount

    @property
    def cash(self) -> Decimal:
        return self.portfolio.cash.amount

    @property
    def position(self) -> Position | None:
        return self.portfolio.position(self.intent.symbol)

    @property
    def notional(self) -> Decimal | None:
        """Limit-price notional in CNY, or None when the order cannot be priced."""
        price = self.intent.limit_price
        if price is None:
            return None
        return quantize_cny(price * Decimal(self.intent.quantity))

    @property
    def buy_fee_reserve(self) -> Decimal:
        """Conservative CNY fee reserve for a buy (never negative)."""
        if self.estimated_fees is not None:
            return max(quantize_cny(self.estimated_fees), Decimal("0"))
        notional = self.notional if self.notional is not None else Decimal("0")
        return max(quantize_cny(notional * DEFAULT_BUY_FEE_RATE), DEFAULT_MIN_COMMISSION)

    @property
    def required_cash(self) -> Decimal | None:
        """Cash needed now: notional plus reserved fees for a buy."""
        notional = self.notional
        if notional is None:
            return None
        if not self.is_buy:
            return Decimal("0")
        return notional + self.buy_fee_reserve

    @property
    def projected_cash(self) -> Decimal | None:
        """Cash after the order settles (sell fees are ignored, i.e. conservative)."""
        notional = self.notional
        if notional is None:
            return None
        if self.is_buy:
            return self.cash - notional - self.buy_fee_reserve
        return self.cash + notional

    @property
    def current_instrument_value(self) -> Decimal:
        position = self.position
        return position.market_value.amount if position is not None else Decimal("0")

    @property
    def projected_instrument_value(self) -> Decimal | None:
        notional = self.notional
        if notional is None:
            return None
        if self.is_buy:
            return self.current_instrument_value + notional
        return max(self.current_instrument_value - notional, Decimal("0"))

    @property
    def projected_gross_exposure(self) -> Decimal | None:
        notional = self.notional
        if notional is None:
            return None
        gross = self.portfolio.gross_exposure.amount
        if self.is_buy:
            return gross + notional
        return max(gross - notional, Decimal("0"))

    @property
    def sector_key(self) -> str | None:
        """Mapping key used for the sector limit, or None when unavailable."""
        if self.sector is not None:
            return self.sector
        if self.intent.symbol in self.sector_weights:
            return self.intent.symbol
        return None

    def weight_of(self, value: Decimal) -> Decimal:
        """Value as a fraction of NAV, quantized to six decimals."""
        return (value / self.nav).quantize(_WEIGHT_QUANTUM)


class RiskController:
    """Pure, deterministic pre-trade risk evaluator.

    Parameters
    ----------
    policy:
        The immutable, reviewed :class:`RiskPolicy`. The controller never mutates
        or re-interprets it; an invalid policy cannot even be constructed (see
        ``ExecutionPolicy`` / ``DrawdownPolicy`` validators).
    """

    def __init__(self, policy: RiskPolicy) -> None:
        # ``RiskPolicy`` validates itself on construction (paper-only environment,
        # no shorting/leverage/derivatives, no forced liquidation), so the
        # controller simply holds the immutable, reviewed policy.
        self._policy: RiskPolicy = policy

    @property
    def policy(self) -> RiskPolicy:
        """The read-only policy this controller enforces."""
        return self._policy

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def evaluate(
        self,
        *,
        intent: OrderIntent,
        account: PortfolioAccount,
        portfolio: PortfolioSnapshot,
        rules: ResolvedTradingRules,
        calendar: TradingCalendar,
        bar: MarketBar | None,
        prev_close: Decimal | None,
        instrument: Instrument | None,
        eligibility: EligibilityResult | None,
        known_decision_ids: set[str],
        known_snapshot_ids: set[str],
        orders_today: int,
        turnover_today: Decimal,
        sector_weights: Mapping[str, Decimal],
        drawn_down: Decimal,
        now: datetime,
        sector: str | None = None,
        estimated_fees: Decimal | None = None,
    ) -> RiskDecision:
        """Evaluate one order intent and return an immutable risk decision.

        Returns a ``RiskDecision`` whose ``outcome`` is ``REJECT`` as soon as any
        check fails; it never raises for a business rejection. A
        :class:`ValidationError` means the *caller* passed something impossible
        (naive datetime, negative counters), which is a programmer error.

        ``known_decision_ids`` / ``known_snapshot_ids`` are the **authoritative**
        sets of persisted ids for this evaluation: a decision/snapshot reference
        that is missing from the set fails closed. Callers must populate them
        from their repositories (``RiskService`` does).
        """
        now = ensure_aware(now, "now")
        self._validate_counters(
            orders_today=orders_today, turnover_today=turnover_today, drawn_down=drawn_down
        )
        context = _CheckContext(
            policy=self._policy,
            intent=intent,
            account=account,
            portfolio=portfolio,
            rules=rules,
            calendar=calendar,
            bar=bar,
            prev_close=prev_close,
            instrument=instrument,
            eligibility=eligibility,
            known_decision_ids=set(known_decision_ids),
            known_snapshot_ids=set(known_snapshot_ids),
            orders_today=orders_today,
            turnover_today=turnover_today,
            sector_weights=dict(sector_weights),
            drawn_down=drawn_down,
            now=now,
            sector=sector,
            estimated_fees=estimated_fees,
        )
        checks = self._run_checks(context)
        failed = tuple(check for check in checks if not check.passed)
        outcome = RiskOutcome.REJECT if failed else RiskOutcome.PASS
        if failed:
            summary = "risk rejected: " + ", ".join(check.rule_id.value for check in failed)
        else:
            summary = f"risk approved: {len(checks)} deterministic checks passed"
        return RiskDecision(
            risk_decision_id=new_id("riskdec"),
            order_intent_id=intent.intent_id,
            account_id=account.account_id,
            outcome=outcome,
            checks=checks,
            policy_version=self._policy.version,
            decided_at=now,
            rule_set_id=rules.rule_set_id,
            rule_set_version=rules.rule_set_version,
            data_snapshot_id=intent.data_snapshot_id,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Claim-free helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_counters(
        *, orders_today: int, turnover_today: Decimal, drawn_down: Decimal
    ) -> None:
        if orders_today < 0:
            raise ValidationError("orders_today must be non-negative", value=orders_today)
        if not isinstance(turnover_today, Decimal) or turnover_today < 0:
            raise ValidationError("turnover_today must be a non-negative Decimal")
        if not isinstance(drawn_down, Decimal) or drawn_down < 0:
            raise ValidationError("drawn_down must be a non-negative Decimal")

    def _run_checks(self, ctx: _CheckContext) -> tuple[RiskCheck, ...]:
        return (
            *self._environment_checks(ctx),
            *self._reference_checks(ctx),
            *self._instrument_checks(ctx),
            *self._market_data_checks(ctx),
            *self._order_checks(ctx),
            *self._exposure_checks(ctx),
            *self._restriction_checks(ctx),
        )

    # ------------------------------------------------------------------
    # Environment and account state
    # ------------------------------------------------------------------
    def _environment_checks(self, ctx: _CheckContext) -> list[RiskCheck]:
        checks: list[RiskCheck] = []

        paper_only = (
            self._policy.execution.environment == "paper_only"
            and ctx.account.mode is AccountMode.PAPER
        )
        checks.append(
            RiskCheck.ok(RiskRuleId.ENVIRONMENT_PAPER_ONLY, measured=ctx.account.mode.value)
            if paper_only
            else RiskCheck.failed(
                RiskRuleId.ENVIRONMENT_PAPER_ONLY,
                "Phase 0 allows paper trading only; live execution is refused",
                measured=ctx.account.mode.value,
                threshold="PAPER",
            )
        )

        account_matches = ctx.intent.account_id == ctx.account.account_id
        account_active = account_matches and ctx.account.state is AccountState.ACTIVE
        checks.append(
            RiskCheck.ok(RiskRuleId.ACCOUNT_ACTIVE, measured=ctx.account.state.value)
            if account_active
            else RiskCheck.failed(
                RiskRuleId.ACCOUNT_ACTIVE,
                "account is not ACTIVE for new orders"
                if account_matches
                else "order intent belongs to a different account",
                measured=ctx.intent.account_id,
                threshold=ctx.account.account_id,
            )
        )

        paused = ctx.account.state is AccountState.PAUSED or ctx.account.pause_reason is not None
        checks.append(
            RiskCheck.failed(
                RiskRuleId.KILL_SWITCH_ENGAGED,
                "kill switch engaged: account is paused and refuses new orders",
                measured=ctx.account.pause_reason or ctx.account.state.value,
            )
            if paused
            else RiskCheck.ok(RiskRuleId.KILL_SWITCH_ENGAGED, measured="not_engaged")
        )
        return checks

    # ------------------------------------------------------------------
    # Traceability references (spec s8.1, acceptance criterion 9)
    # ------------------------------------------------------------------
    def _reference_checks(self, ctx: _CheckContext) -> list[RiskCheck]:
        checks: list[RiskCheck] = []

        decision_id = ctx.intent.decision_id
        if not self._policy.controls.require_decision_reference:
            checks.append(RiskCheck.ok(RiskRuleId.DECISION_REFERENCE, measured="not_required"))
        elif not decision_id:
            checks.append(
                RiskCheck.failed(
                    RiskRuleId.DECISION_REFERENCE,
                    "order intent has no decision reference",
                )
            )
        elif decision_id not in ctx.known_decision_ids:
            checks.append(
                RiskCheck.failed(
                    RiskRuleId.DECISION_REFERENCE,
                    "decision reference does not resolve to a stored decision",
                    measured=decision_id,
                )
            )
        else:
            checks.append(RiskCheck.ok(RiskRuleId.DECISION_REFERENCE, measured=decision_id))

        snapshot_id = ctx.intent.data_snapshot_id
        if not snapshot_id:
            checks.append(
                RiskCheck.failed(
                    RiskRuleId.DATA_SNAPSHOT_REFERENCE,
                    "order intent has no data-snapshot reference",
                )
            )
        elif snapshot_id not in ctx.known_snapshot_ids:
            checks.append(
                RiskCheck.failed(
                    RiskRuleId.DATA_SNAPSHOT_REFERENCE,
                    "data-snapshot reference does not resolve to a stored snapshot",
                    measured=snapshot_id,
                )
            )
        else:
            checks.append(RiskCheck.ok(RiskRuleId.DATA_SNAPSHOT_REFERENCE, measured=snapshot_id))

        strategy_id = ctx.intent.strategy_id
        strategy_version = ctx.intent.strategy_version
        if not strategy_id or not strategy_version:
            checks.append(
                RiskCheck.failed(
                    RiskRuleId.STRATEGY_VERSION,
                    "order intent must reference a registered strategy and version",
                    measured=f"{strategy_id or '-'}@{strategy_version or '-'}",
                )
            )
        elif not _SEMVER_PATTERN.match(strategy_version):
            checks.append(
                RiskCheck.failed(
                    RiskRuleId.STRATEGY_VERSION,
                    "strategy version must be semver-like, e.g. 0.1.0",
                    measured=strategy_version,
                )
            )
        else:
            checks.append(
                RiskCheck.ok(
                    RiskRuleId.STRATEGY_VERSION,
                    measured=f"{strategy_id}@{strategy_version}",
                )
            )

        checks.append(self._ruleset_check(ctx))
        return checks

    def _ruleset_check(self, ctx: _CheckContext) -> RiskCheck:
        if not self._policy.controls.require_known_ruleset:
            return RiskCheck.ok(RiskRuleId.RULESET_KNOWN, measured="not_required")
        rule_set_id = ctx.intent.rule_set_id
        rule_set_version = ctx.intent.rule_set_version
        if not rule_set_id or not rule_set_version:
            return RiskCheck.failed(
                RiskRuleId.RULESET_KNOWN,
                "order intent has no rule-set reference",
            )
        if (rule_set_id, rule_set_version) != (
            ctx.rules.rule_set_id,
            ctx.rules.rule_set_version,
        ):
            return RiskCheck.failed(
                RiskRuleId.RULESET_KNOWN,
                "order intent references a rule set that differs from the resolved rules",
                measured=f"{rule_set_id}@{rule_set_version}",
                threshold=f"{ctx.rules.rule_set_id}@{ctx.rules.rule_set_version}",
            )
        if ctx.rules.symbol != ctx.intent.symbol:
            return RiskCheck.failed(
                RiskRuleId.RULESET_KNOWN,
                "resolved rules belong to a different instrument",
                measured=ctx.rules.symbol,
                threshold=ctx.intent.symbol,
            )
        if ctx.rules.trade_date != ctx.intent.earliest_execution_date:
            return RiskCheck.failed(
                RiskRuleId.RULESET_KNOWN,
                "resolved rules are for a different trade date",
                measured=ctx.rules.trade_date.isoformat(),
                threshold=ctx.intent.earliest_execution_date.isoformat(),
            )
        return RiskCheck.ok(
            RiskRuleId.RULESET_KNOWN,
            measured=f"{rule_set_id}@{rule_set_version}/{ctx.rules.profile_id}",
        )

    # ------------------------------------------------------------------
    # Instrument identity and eligibility
    # ------------------------------------------------------------------
    def _instrument_checks(self, ctx: _CheckContext) -> list[RiskCheck]:
        instrument = ctx.instrument
        if instrument is None:
            known = RiskCheck.failed(
                RiskRuleId.INSTRUMENT_KNOWN,
                "instrument is not present in the security master",
                measured=ctx.intent.symbol,
            )
        elif instrument.symbol != ctx.intent.symbol:
            known = RiskCheck.failed(
                RiskRuleId.INSTRUMENT_KNOWN,
                "resolved instrument differs from the order symbol",
                measured=instrument.symbol,
                threshold=ctx.intent.symbol,
            )
        else:
            known = RiskCheck.ok(
                RiskRuleId.INSTRUMENT_KNOWN,
                measured=f"{instrument.symbol}/{instrument.instrument_type.value}",
            )

        eligibility = ctx.eligibility
        if not ctx.is_buy:
            eligible = RiskCheck.ok(
                RiskRuleId.INSTRUMENT_ELIGIBLE,
                measured="sell_of_existing_position",
            )
        elif eligibility is None:
            eligible = RiskCheck.failed(
                RiskRuleId.INSTRUMENT_ELIGIBLE,
                "eligibility could not be evaluated for this buy",
            )
        elif eligibility.symbol != ctx.intent.symbol:
            eligible = RiskCheck.failed(
                RiskRuleId.INSTRUMENT_ELIGIBLE,
                "eligibility result belongs to a different instrument",
                measured=eligibility.symbol,
                threshold=ctx.intent.symbol,
            )
        elif not eligibility.eligible:
            eligible = RiskCheck.failed(
                RiskRuleId.INSTRUMENT_ELIGIBLE,
                "instrument is not in the eligible buy universe",
                measured=",".join(eligibility.rejection_reasons()),
            )
        else:
            eligible = RiskCheck.ok(RiskRuleId.INSTRUMENT_ELIGIBLE, measured="eligible")
        return [known, eligible]

    # ------------------------------------------------------------------
    # Market data freshness and quality
    # ------------------------------------------------------------------
    def _market_data_checks(self, ctx: _CheckContext) -> list[RiskCheck]:
        checks: list[RiskCheck] = []
        bar = ctx.bar
        today = market_date(ctx.now)

        if not self._policy.controls.require_fresh_market_data:
            checks.append(RiskCheck.ok(RiskRuleId.MARKET_DATA_FRESH, measured="not_required"))
        elif bar is None:
            checks.append(
                RiskCheck.failed(
                    RiskRuleId.MARKET_DATA_FRESH,
                    "no market bar is available for the instrument",
                )
            )
        elif bar.symbol != ctx.intent.symbol:
            checks.append(
                RiskCheck.failed(
                    RiskRuleId.MARKET_DATA_FRESH,
                    "market bar belongs to a different instrument",
                    measured=bar.symbol,
                    threshold=ctx.intent.symbol,
                )
            )
        elif bar.trade_date > today:
            checks.append(
                RiskCheck.failed(
                    RiskRuleId.MARKET_DATA_FRESH,
                    "market bar is dated in the future",
                    measured=bar.trade_date.isoformat(),
                )
            )
        else:
            age_days = (today - bar.trade_date).days
            max_age = self._policy.controls.max_bar_age_days
            checks.append(
                RiskCheck.ok(
                    RiskRuleId.MARKET_DATA_FRESH,
                    measured=f"{age_days}d",
                    threshold=f"{max_age}d",
                )
                if age_days <= max_age
                else RiskCheck.failed(
                    RiskRuleId.MARKET_DATA_FRESH,
                    "market bar is stale and cannot back a new paper order",
                    measured=f"{age_days}d",
                    threshold=f"{max_age}d",
                )
            )

        checks.append(self._data_quality_check(ctx))
        return checks

    def _data_quality_check(self, ctx: _CheckContext) -> RiskCheck:
        bar = ctx.bar
        if bar is None:
            return RiskCheck.failed(
                RiskRuleId.DATA_QUALITY,
                "no market bar is available to satisfy the data-quality gate",
            )
        if bar.symbol != ctx.intent.symbol:
            return RiskCheck.failed(
                RiskRuleId.DATA_QUALITY,
                "market bar belongs to a different instrument",
                measured=bar.symbol,
                threshold=ctx.intent.symbol,
            )
        if bar.point_in_time.known_at > ctx.now:
            return RiskCheck.failed(
                RiskRuleId.DATA_QUALITY,
                "market bar is not yet known at the decision time (look-ahead data)",
                measured=bar.point_in_time.known_at.isoformat(),
                threshold=ctx.now.isoformat(),
            )
        if bar.adjustment is not AdjustmentMode.UNADJUSTED:
            return RiskCheck.failed(
                RiskRuleId.DATA_QUALITY,
                "only unadjusted prices may back simulated execution",
                measured=bar.adjustment.value,
                threshold=AdjustmentMode.UNADJUSTED.value,
            )
        if bar.quality is not DataQualityStatus.OK:
            return RiskCheck.failed(
                RiskRuleId.DATA_QUALITY,
                "market data is quarantined or not quality-checked",
                measured=bar.quality.value,
                threshold=DataQualityStatus.OK.value,
            )
        if bar.volume <= 0:
            return RiskCheck.failed(
                RiskRuleId.DATA_QUALITY,
                "zero-volume bar cannot back a paper order (suspended or untradable)",
                measured=bar.volume,
            )
        return RiskCheck.ok(
            RiskRuleId.DATA_QUALITY,
            measured=f"{bar.quality.value}/{bar.adjustment.value}",
        )

    # ------------------------------------------------------------------
    # Order shape, grid, price band, session and look-ahead
    # ------------------------------------------------------------------
    def _order_checks(self, ctx: _CheckContext) -> list[RiskCheck]:
        return [
            self._order_type_check(ctx),
            self._quantity_increment_check(ctx),
            self._price_tick_check(ctx),
            self._price_band_check(ctx),
            self._session_check(ctx),
            self._lookahead_check(ctx),
        ]

    def _order_type_check(self, ctx: _CheckContext) -> RiskCheck:
        order_type = ctx.intent.order_type
        if not order_type.is_supported:
            return RiskCheck.failed(
                RiskRuleId.ORDER_TYPE_SUPPORTED,
                "Phase 0 supports limit orders only",
                measured=order_type.value,
            )
        if not ctx.rules.allows(order_type, ctx.intent.validity):
            return RiskCheck.failed(
                RiskRuleId.ORDER_TYPE_SUPPORTED,
                "the resolved rule set does not allow this order type/validity",
                measured=f"{order_type.value}/{ctx.intent.validity.value}",
                threshold=",".join(
                    f"{t.value}/{v.value}"
                    for t in ctx.rules.allowed_order_types
                    for v in ctx.rules.allowed_validities
                ),
            )
        return RiskCheck.ok(
            RiskRuleId.ORDER_TYPE_SUPPORTED,
            measured=f"{order_type.value}/{ctx.intent.validity.value}",
        )

    def _quantity_increment_check(self, ctx: _CheckContext) -> RiskCheck:
        quantity = ctx.intent.quantity
        if quantity <= 0:  # pragma: no cover - model already enforces gt=0
            return RiskCheck.failed(
                RiskRuleId.QUANTITY_INCREMENT, "quantity must be positive", measured=quantity
            )
        if ctx.is_buy:
            increment = ctx.rules.buy_quantity_increment
            minimum = ctx.rules.min_buy_quantity
            if increment <= 0:  # pragma: no cover - model validates gt=0
                return RiskCheck.failed(
                    RiskRuleId.QUANTITY_INCREMENT,
                    "rule set declares an invalid buy increment",
                    measured=increment,
                )
            if quantity < minimum:
                return RiskCheck.failed(
                    RiskRuleId.QUANTITY_INCREMENT,
                    "buy quantity is below the minimum lot",
                    measured=quantity,
                    threshold=minimum,
                )
            if quantity % increment != 0:
                return RiskCheck.failed(
                    RiskRuleId.QUANTITY_INCREMENT,
                    "buy quantity is not a multiple of the lot increment",
                    measured=quantity,
                    threshold=increment,
                )
            return RiskCheck.ok(
                RiskRuleId.QUANTITY_INCREMENT,
                measured=quantity,
                threshold=increment,
            )

        increment = ctx.rules.sell_quantity_increment
        if increment <= 0:  # pragma: no cover - model validates gt=0
            return RiskCheck.failed(
                RiskRuleId.QUANTITY_INCREMENT,
                "rule set declares an invalid sell increment",
                measured=increment,
            )
        if quantity % increment != 0:
            return RiskCheck.failed(
                RiskRuleId.QUANTITY_INCREMENT,
                "sell quantity is not a multiple of the sell increment",
                measured=quantity,
                threshold=increment,
            )
        odd_lot = quantity % ctx.rules.buy_quantity_increment != 0
        if odd_lot and ctx.rules.odd_lot_sell_must_be_whole_position:
            position = ctx.position
            if position is None or quantity != position.quantity:
                return RiskCheck.failed(
                    RiskRuleId.QUANTITY_INCREMENT,
                    "an odd-lot sale must dispose of the whole remaining position",
                    measured=quantity,
                    threshold=position.quantity if position is not None else 0,
                )
        return RiskCheck.ok(
            RiskRuleId.QUANTITY_INCREMENT,
            measured=quantity,
            threshold=increment,
        )

    def _price_tick_check(self, ctx: _CheckContext) -> RiskCheck:
        price = ctx.intent.limit_price
        if ctx.intent.order_type is not OrderType.LIMIT:
            return RiskCheck.ok(RiskRuleId.PRICE_TICK, measured="not_a_limit_order")
        if price is None:
            return RiskCheck.failed(RiskRuleId.PRICE_TICK, "limit order has no limit price")
        tick = ctx.rules.tick_size
        if tick <= 0:
            return RiskCheck.failed(
                RiskRuleId.PRICE_TICK,
                "resolved rule set declares a non-positive tick size",
                measured=tick,
            )
        if price % tick != 0:
            return RiskCheck.failed(
                RiskRuleId.PRICE_TICK,
                "limit price is not a multiple of the instrument tick size",
                measured=price,
                threshold=tick,
            )
        return RiskCheck.ok(RiskRuleId.PRICE_TICK, measured=price, threshold=tick)

    def _price_band_check(self, ctx: _CheckContext) -> RiskCheck:
        price = ctx.intent.limit_price
        if price is None:
            return RiskCheck.failed(RiskRuleId.PRICE_LIMIT_BAND, "limit order has no limit price")
        prev_close = ctx.prev_close
        if prev_close is None:
            return RiskCheck.failed(
                RiskRuleId.PRICE_LIMIT_BAND,
                "previous close is unavailable, so the price-limit band cannot be proven",
            )
        if prev_close <= 0:
            return RiskCheck.failed(
                RiskRuleId.PRICE_LIMIT_BAND,
                "previous close must be positive",
                measured=prev_close,
            )
        is_risk_warning = ctx.instrument.is_risk_warning if ctx.instrument is not None else False
        try:
            limits = ctx.rules.compute_price_limits(prev_close, is_risk_warning=is_risk_warning)
        except JanusError as exc:  # pragma: no cover - defensive fail-closed path
            return RiskCheck.failed(
                RiskRuleId.PRICE_LIMIT_BAND,
                f"price limits could not be computed: {exc.message}",
            )
        if not limits.has_limits:
            return RiskCheck.ok(
                RiskRuleId.PRICE_LIMIT_BAND,
                measured=limits.no_limit_reason or "no_limit",
            )
        limit_up = limits.limit_up
        limit_down = limits.limit_down
        if limit_up is not None and price > limit_up:
            return RiskCheck.failed(
                RiskRuleId.PRICE_LIMIT_BAND,
                "limit price is above the daily price limit-up",
                measured=price,
                threshold=limit_up,
            )
        if limit_down is not None and price < limit_down:
            return RiskCheck.failed(
                RiskRuleId.PRICE_LIMIT_BAND,
                "limit price is below the daily price limit-down",
                measured=price,
                threshold=limit_down,
            )
        return RiskCheck.ok(
            RiskRuleId.PRICE_LIMIT_BAND,
            measured=price,
            threshold=f"[{limit_down}, {limit_up}]",
        )

    def _session_check(self, ctx: _CheckContext) -> RiskCheck:
        target = ctx.intent.earliest_execution_date
        today = market_date(ctx.now)
        if target < today:
            return RiskCheck.failed(
                RiskRuleId.SESSION_VALID,
                "earliest execution date is in the past",
                measured=target.isoformat(),
                threshold=today.isoformat(),
            )
        try:
            ctx.calendar.ensure_trading_day(target)
        except JanusError as exc:
            return RiskCheck.failed(
                RiskRuleId.SESSION_VALID,
                f"target trade date is not a supported trading session: {exc.message}",
                measured=target.isoformat(),
            )
        if target == today and not ctx.calendar.is_open_for_trading(ctx.now):
            return RiskCheck.failed(
                RiskRuleId.SESSION_VALID,
                "the market session is closed for execution at the decision time",
                measured=ctx.now.isoformat(),
            )
        return RiskCheck.ok(RiskRuleId.SESSION_VALID, measured=target.isoformat())

    def _lookahead_check(self, ctx: _CheckContext) -> RiskCheck:
        target = ctx.intent.earliest_execution_date
        signal_date = ctx.intent.signal_bar_date
        if signal_date is None and ctx.bar is not None:
            signal_date = ctx.bar.trade_date
        if signal_date is None:
            return RiskCheck.failed(
                RiskRuleId.SIGNAL_LOOKAHEAD,
                "signal bar date is unavailable, so look-ahead safety cannot be proven",
            )
        if signal_date >= target:
            return RiskCheck.failed(
                RiskRuleId.SIGNAL_LOOKAHEAD,
                "signal data from T cannot fill on T or later than the target date",
                measured=signal_date.isoformat(),
                threshold=target.isoformat(),
            )
        if ctx.bar is not None and ctx.bar.trade_date >= target:
            return RiskCheck.failed(
                RiskRuleId.SIGNAL_LOOKAHEAD,
                "the supplied bar is the fill-day bar; same-day fill is forbidden",
                measured=ctx.bar.trade_date.isoformat(),
                threshold=target.isoformat(),
            )
        return RiskCheck.ok(
            RiskRuleId.SIGNAL_LOOKAHEAD,
            measured=f"{signal_date.isoformat()}->{target.isoformat()}",
        )

    # ------------------------------------------------------------------
    # Cash / quantity affordability and exposure limits
    # ------------------------------------------------------------------
    def _exposure_checks(self, ctx: _CheckContext) -> list[RiskCheck]:
        return [
            self._available_cash_check(ctx),
            self._available_quantity_check(ctx),
            self._single_instrument_weight_check(ctx),
            self._min_cash_weight_check(ctx),
            self._daily_turnover_check(ctx),
            self._max_orders_check(ctx),
            self._gross_exposure_check(ctx),
            self._sector_exposure_check(ctx),
            self._drawdown_check(ctx),
        ]

    def _available_cash_check(self, ctx: _CheckContext) -> RiskCheck:
        if not ctx.is_buy:
            return RiskCheck.ok(RiskRuleId.AVAILABLE_CASH, measured="sell_no_cash_required")
        required = ctx.required_cash
        if required is None:
            return RiskCheck.failed(
                RiskRuleId.AVAILABLE_CASH, "order notional cannot be determined"
            )
        max_order_notional = self._policy.limits.max_order_notional
        if max_order_notional is not None and required > max_order_notional:
            return RiskCheck.failed(
                RiskRuleId.AVAILABLE_CASH,
                "order notional exceeds the configured per-order cap",
                measured=required,
                threshold=max_order_notional,
            )
        if required > ctx.cash:
            return RiskCheck.failed(
                RiskRuleId.AVAILABLE_CASH,
                "insufficient available cash for notional plus estimated fees",
                measured=required,
                threshold=ctx.cash,
            )
        return RiskCheck.ok(RiskRuleId.AVAILABLE_CASH, measured=required, threshold=ctx.cash)

    def _available_quantity_check(self, ctx: _CheckContext) -> RiskCheck:
        if ctx.is_buy:
            return RiskCheck.ok(RiskRuleId.AVAILABLE_QUANTITY, measured="buy_no_quantity_required")
        position = ctx.position
        if position is None:
            return RiskCheck.failed(
                RiskRuleId.AVAILABLE_QUANTITY,
                "no position exists to sell",
                measured=ctx.intent.quantity,
                threshold=0,
            )
        if ctx.intent.quantity > position.available_quantity:
            return RiskCheck.failed(
                RiskRuleId.AVAILABLE_QUANTITY,
                "sell quantity exceeds the available-to-sell quantity (T+1)",
                measured=ctx.intent.quantity,
                threshold=position.available_quantity,
            )
        return RiskCheck.ok(
            RiskRuleId.AVAILABLE_QUANTITY,
            measured=ctx.intent.quantity,
            threshold=position.available_quantity,
        )

    def _single_instrument_weight_check(self, ctx: _CheckContext) -> RiskCheck:
        """Enforce the per-instrument limit with explicit, fail-closed semantics.

        Semantics (documented because there are two legitimate readings):

        * **Buy** -- the intent's symbol *post-trade* weight must not exceed the
          limit, **and** the portfolio must not already hold another instrument
          above the limit. A pre-existing breach blocks new risk-taking until it
          is reduced (spec s8.1: a risk breach pauses new orders).
        * **Sell** -- always passes this rule: a sale can only reduce exposure.
          This keeps a breach reducible; the controller never forces a
          liquidation, but it must not block reducing the position either.
        """
        if not ctx.is_buy:
            return RiskCheck.ok(RiskRuleId.SINGLE_INSTRUMENT_WEIGHT, measured="risk_reducing_sell")
        if ctx.nav <= 0:
            return RiskCheck.failed(
                RiskRuleId.SINGLE_INSTRUMENT_WEIGHT,
                "portfolio NAV is non-positive",
                measured=ctx.nav,
            )
        projected = ctx.projected_instrument_value
        if projected is None:
            return RiskCheck.failed(
                RiskRuleId.SINGLE_INSTRUMENT_WEIGHT,
                "projected instrument value cannot be determined",
            )
        limit = self._policy.limits.max_single_instrument_weight
        weight = ctx.weight_of(projected)
        if weight > limit:
            return RiskCheck.failed(
                RiskRuleId.SINGLE_INSTRUMENT_WEIGHT,
                "post-trade single-instrument weight exceeds the policy limit",
                measured=weight,
                threshold=limit,
            )
        for position in ctx.portfolio.positions:
            if position.symbol == ctx.intent.symbol:
                continue
            existing_weight = ctx.weight_of(position.market_value.amount)
            if existing_weight > limit:
                return RiskCheck.failed(
                    RiskRuleId.SINGLE_INSTRUMENT_WEIGHT,
                    "portfolio already breaches the single-instrument limit for "
                    "another symbol; new buys fail closed",
                    measured=f"{position.symbol}={existing_weight}",
                    threshold=limit,
                )
        return RiskCheck.ok(RiskRuleId.SINGLE_INSTRUMENT_WEIGHT, measured=weight, threshold=limit)

    def _min_cash_weight_check(self, ctx: _CheckContext) -> RiskCheck:
        if ctx.nav <= 0:
            return RiskCheck.failed(
                RiskRuleId.MIN_CASH_WEIGHT,
                "portfolio NAV is non-positive",
                measured=ctx.nav,
            )
        projected_cash = ctx.projected_cash
        if projected_cash is None:
            return RiskCheck.failed(
                RiskRuleId.MIN_CASH_WEIGHT, "post-trade cash cannot be determined"
            )
        weight = ctx.weight_of(max(projected_cash, Decimal("0")))
        limit = self._policy.limits.min_cash_weight
        if weight < limit:
            return RiskCheck.failed(
                RiskRuleId.MIN_CASH_WEIGHT,
                "post-trade cash weight would fall below the policy minimum",
                measured=weight,
                threshold=limit,
            )
        return RiskCheck.ok(RiskRuleId.MIN_CASH_WEIGHT, measured=weight, threshold=limit)

    def _daily_turnover_check(self, ctx: _CheckContext) -> RiskCheck:
        if ctx.nav <= 0:
            return RiskCheck.failed(
                RiskRuleId.DAILY_TURNOVER, "portfolio NAV is non-positive", measured=ctx.nav
            )
        notional = ctx.notional
        if notional is None:
            return RiskCheck.failed(
                RiskRuleId.DAILY_TURNOVER, "order notional cannot be determined"
            )
        projected = (ctx.turnover_today + notional) / ctx.nav
        projected = projected.quantize(_WEIGHT_QUANTUM)
        limit = self._policy.limits.max_daily_turnover
        if projected > limit:
            return RiskCheck.failed(
                RiskRuleId.DAILY_TURNOVER,
                "projected daily turnover exceeds the policy limit",
                measured=projected,
                threshold=limit,
            )
        return RiskCheck.ok(RiskRuleId.DAILY_TURNOVER, measured=projected, threshold=limit)

    def _max_orders_check(self, ctx: _CheckContext) -> RiskCheck:
        limit = self._policy.limits.max_orders_per_day
        if ctx.orders_today >= limit:
            return RiskCheck.failed(
                RiskRuleId.MAX_ORDERS_PER_DAY,
                "daily order budget is exhausted",
                measured=ctx.orders_today,
                threshold=limit,
            )
        return RiskCheck.ok(
            RiskRuleId.MAX_ORDERS_PER_DAY, measured=ctx.orders_today, threshold=limit
        )

    def _gross_exposure_check(self, ctx: _CheckContext) -> RiskCheck:
        if ctx.nav <= 0:
            return RiskCheck.failed(
                RiskRuleId.GROSS_EXPOSURE, "portfolio NAV is non-positive", measured=ctx.nav
            )
        projected = ctx.projected_gross_exposure
        if projected is None:
            return RiskCheck.failed(
                RiskRuleId.GROSS_EXPOSURE, "order notional cannot be determined"
            )
        weight = ctx.weight_of(projected)
        limit = self._policy.limits.max_gross_exposure
        if weight > limit:
            return RiskCheck.failed(
                RiskRuleId.GROSS_EXPOSURE,
                "post-trade gross exposure exceeds the policy limit",
                measured=weight,
                threshold=limit,
            )
        return RiskCheck.ok(RiskRuleId.GROSS_EXPOSURE, measured=weight, threshold=limit)

    def _sector_exposure_check(self, ctx: _CheckContext) -> RiskCheck:
        if ctx.nav <= 0:
            return RiskCheck.failed(
                RiskRuleId.SECTOR_EXPOSURE,
                "portfolio NAV is non-positive",
                measured=ctx.nav,
            )
        notional = ctx.notional
        if notional is None:
            return RiskCheck.failed(
                RiskRuleId.SECTOR_EXPOSURE, "order notional cannot be determined"
            )
        key = ctx.sector_key
        if key is None:
            # Phase 0's instrument master has no sector field. The check stays
            # explicit rather than silently claiming compliance: a passing check
            # documents that no sector attribution was available to enforce.
            return RiskCheck.ok(
                RiskRuleId.SECTOR_EXPOSURE,
                measured="sector_attribution_unavailable",
                threshold=self._policy.limits.max_sector_weight,
            )
        current = ctx.sector_weights.get(key)
        if current is None:
            return RiskCheck.failed(
                RiskRuleId.SECTOR_EXPOSURE,
                "sector attribution was supplied but the sector weight is unknown",
                measured=key,
                threshold=self._policy.limits.max_sector_weight,
            )
        delta = notional / ctx.nav
        projected = current + delta if ctx.is_buy else max(current - delta, Decimal("0"))
        projected = projected.quantize(_WEIGHT_QUANTUM)
        limit = self._policy.limits.max_sector_weight
        if projected > limit:
            return RiskCheck.failed(
                RiskRuleId.SECTOR_EXPOSURE,
                "post-trade sector weight exceeds the policy limit",
                measured=projected,
                threshold=limit,
            )
        return RiskCheck.ok(RiskRuleId.SECTOR_EXPOSURE, measured=projected, threshold=limit)

    def _drawdown_check(self, ctx: _CheckContext) -> RiskCheck:
        threshold = self._policy.drawdown.pause_new_orders_at
        if ctx.drawn_down >= threshold:
            return RiskCheck.failed(
                RiskRuleId.DRAWDOWN_PAUSE,
                "drawdown pause is active: new orders are blocked (no forced liquidation)",
                measured=ctx.drawn_down,
                threshold=threshold,
            )
        return RiskCheck.ok(RiskRuleId.DRAWDOWN_PAUSE, measured=ctx.drawn_down, threshold=threshold)

    # ------------------------------------------------------------------
    # Hard Phase 0 restrictions
    # ------------------------------------------------------------------
    def _restriction_checks(self, ctx: _CheckContext) -> list[RiskCheck]:
        return [
            self._no_short_check(ctx),
            self._no_leverage_check(ctx),
            self._no_derivatives_check(ctx),
        ]

    def _no_short_check(self, ctx: _CheckContext) -> RiskCheck:
        if self._policy.execution.allow_short:  # pragma: no cover - policy rejects
            return RiskCheck.ok(RiskRuleId.NO_SHORT, measured="policy_allows_short")
        if ctx.is_buy:
            return RiskCheck.ok(RiskRuleId.NO_SHORT, measured="buy_not_a_short")
        position = ctx.position
        if position is None:
            return RiskCheck.failed(
                RiskRuleId.NO_SHORT,
                "selling without a position would be a short sale",
                measured=ctx.intent.quantity,
                threshold=0,
            )
        if ctx.intent.quantity > position.quantity:
            return RiskCheck.failed(
                RiskRuleId.NO_SHORT,
                "sell quantity exceeds the owned position (short selling is forbidden)",
                measured=ctx.intent.quantity,
                threshold=position.quantity,
            )
        return RiskCheck.ok(
            RiskRuleId.NO_SHORT, measured=ctx.intent.quantity, threshold=position.quantity
        )

    def _no_leverage_check(self, ctx: _CheckContext) -> RiskCheck:
        if self._policy.execution.allow_leverage:  # pragma: no cover - policy rejects
            return RiskCheck.failed(
                RiskRuleId.NO_LEVERAGE, "policy enables leverage; Phase 0 forbids it"
            )
        if not ctx.is_buy:
            return RiskCheck.ok(RiskRuleId.NO_LEVERAGE, measured="sell_reduces_exposure")
        projected_cash = ctx.projected_cash
        if projected_cash is None:
            return RiskCheck.failed(RiskRuleId.NO_LEVERAGE, "post-trade cash cannot be determined")
        if projected_cash < 0:
            return RiskCheck.failed(
                RiskRuleId.NO_LEVERAGE,
                "purchase would require borrowed cash (leverage is forbidden)",
                measured=projected_cash,
                threshold=0,
            )
        return RiskCheck.ok(RiskRuleId.NO_LEVERAGE, measured=projected_cash, threshold=0)

    def _no_derivatives_check(self, ctx: _CheckContext) -> RiskCheck:
        if self._policy.execution.allow_derivatives:  # pragma: no cover - policy rejects
            return RiskCheck.failed(
                RiskRuleId.NO_DERIVATIVES,
                "policy enables derivatives; Phase 0 forbids them",
            )
        instrument = ctx.instrument
        if instrument is not None and instrument.instrument_type in _DERIVATIVE_INSTRUMENT_TYPES:
            return RiskCheck.failed(
                RiskRuleId.NO_DERIVATIVES,
                "derivative and leveraged/inverse products are forbidden in Phase 0",
                measured=instrument.instrument_type.value,
            )
        return RiskCheck.ok(
            RiskRuleId.NO_DERIVATIVES,
            measured=(instrument.instrument_type.value if instrument is not None else "unknown"),
        )
