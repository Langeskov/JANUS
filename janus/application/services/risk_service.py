"""Risk-evaluation orchestration wrapper (Phase 0 spec s8, s11 P0-G).

This service is the *only* thing the rest of the application should call to have
an :class:`~janus.domain.orders.models.OrderIntent` risk-checked. It gathers the
trusted inputs, hands them to the pure
:class:`~janus.domain.risk.controller.RiskController`, persists the resulting
:class:`~janus.domain.risk.models.RiskDecision` together with its
:class:`~janus.domain.strategy.decision.DecisionRecord` lineage, and returns the
decision plus a :class:`~janus.domain.orders.models.PaperOrder` that is already in
``RISK_APPROVED`` or ``RISK_REJECTED`` state.

Constructor injection (everything is explicit; no global state, no hidden I/O)
--------------------------------------------------------------------------------
``uow_factory``
    Produces transactional :class:`~janus.application.ports.repositories.UnitOfWork`
    objects. The service reads accounts / portfolio snapshots / bars / orders /
    fills and appends risk decisions inside one unit of work.
``clock``
    Injected time source; the controller never calls ``datetime.now()``.
``policy``
    The immutable, reviewed :class:`~janus.domain.risk.models.RiskPolicy`.
``calendar``
    The effective trading calendar.
``engine``
    The market-rules engine (structurally :class:`MarketRulesResolver`, i.e. an
    object with ``resolve(instrument, trade_date, ...)`` and
    ``compute_fees(rules=..., side=..., quantity=..., price=...)``). Passing the
    real ``janus.domain.market_rules.engine.MarketRulesEngine`` instance works
    as-is; no engine implementation is imported here. ``resolve`` must fail
    closed (raise a ``JanusError``) when no rule set covers the instrument/date.
    ``compute_fees`` supplies the effective-dated itemised buy fee total used by
    the cash-affordability gate (a conservative reserve is used if it fails).
``controller``
    Optional pre-built controller (must use ``policy``); built automatically
    otherwise.
``universe_policy``
    Eligibility policy used to evaluate *buys*. Defaults to the conservative
    Phase 0 :class:`~janus.domain.instruments.eligibility.UniversePolicy`.
``eligibility``
    Optional pre-built eligibility evaluator, for callers that already own one.

Fail-closed behaviour
---------------------
If the account, a valuation snapshot, the instrument or the trading rules cannot
be loaded the service returns a ``REJECT`` decision whose single failed check
names the reason; it never raises a business exception and it never builds an
approved order.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final, Protocol

from janus.application.ports.clock import Clock
from janus.application.ports.repositories import UnitOfWork, UnitOfWorkFactory
from janus.domain.common.enums import OrderSide, OrderStatus, RiskOutcome
from janus.domain.common.errors import JanusError
from janus.domain.common.ids import new_id
from janus.domain.common.money import quantize_cny
from janus.domain.instruments.eligibility import (
    EligibilityPolicy,
    EligibilityResult,
    UniversePolicy,
)
from janus.domain.instruments.models import Instrument
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.models import FeeBreakdown, ResolvedTradingRules
from janus.domain.orders.models import OrderIntent, PaperOrder
from janus.domain.orders.state_machine import transition
from janus.domain.portfolio.models import PortfolioAccount, PortfolioSnapshot
from janus.domain.risk.controller import RiskController
from janus.domain.risk.models import RiskCheck, RiskDecision, RiskPolicy, RiskRuleId
from janus.domain.strategy.decision import DecisionRecord, DecisionType

__all__ = ["MarketRulesResolver", "RiskService"]

_UNRESOLVED_RULE_SET_ID: Final[str] = "UNRESOLVED-RULESET"
_UNRESOLVED_FEE_SCHEDULE_ID: Final[str] = "UNRESOLVED-FEE-SCHEDULE"


class MarketRulesResolver(Protocol):
    """Structural view of the shared market-rules engine.

    Deliberately a protocol so this service never imports the engine
    implementation (the engine itself belongs to the domain layer; the concrete
    instance is injected by the composition root).
    """

    def resolve(
        self,
        instrument: Instrument,
        trade_date: date,
        *,
        at: datetime | None = None,
        new_listing_stage: bool = False,
    ) -> ResolvedTradingRules: ...

    def compute_fees(
        self,
        *,
        rules: ResolvedTradingRules,
        side: OrderSide,
        quantity: int,
        price: Decimal,
    ) -> FeeBreakdown:
        """Effective-dated, itemised fees for one simulated order."""
        ...


class RiskService:
    """Gather inputs, evaluate risk, persist the decision, return the order."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        policy: RiskPolicy,
        calendar: TradingCalendar,
        engine: MarketRulesResolver,
        controller: RiskController | None = None,
        universe_policy: UniversePolicy | None = None,
        eligibility: EligibilityPolicy | None = None,
    ) -> None:
        self._uow_factory: UnitOfWorkFactory = uow_factory
        self._clock: Clock = clock
        self._policy: RiskPolicy = policy
        self._calendar: TradingCalendar = calendar
        self._engine: MarketRulesResolver = engine
        self._controller: RiskController = controller or RiskController(policy)
        self._eligibility: EligibilityPolicy = eligibility or EligibilityPolicy(
            universe_policy or UniversePolicy()
        )

    @property
    def policy(self) -> RiskPolicy:
        return self._policy

    @property
    def calendar(self) -> TradingCalendar:
        return self._calendar

    @property
    def controller(self) -> RiskController:
        return self._controller

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def evaluate(
        self,
        intent: OrderIntent,
        *,
        account_id: str | None = None,
        estimated_fees: Decimal | None = None,
    ) -> tuple[RiskDecision, PaperOrder]:
        """Risk-check ``intent``; return ``(decision, order)``.

        The returned order is ``RISK_APPROVED`` only when every check passed and
        is ``RISK_REJECTED`` otherwise. The decision is always persisted, even for
        a rejection (spec s10 decision lineage).
        """
        now = self._clock.now()
        evaluated_account_id = account_id or intent.account_id
        with self._uow_factory() as uow:
            account = uow.accounts.get(evaluated_account_id)
            if account is None:
                return self._persist_rejection(
                    uow,
                    intent,
                    evaluated_account_id,
                    now,
                    RiskRuleId.ACCOUNT_ACTIVE,
                    "account does not exist",
                )
            portfolio = uow.portfolio_snapshots.latest_for_account(account.account_id)
            if portfolio is None:
                return self._persist_rejection(
                    uow,
                    intent,
                    account.account_id,
                    now,
                    RiskRuleId.AVAILABLE_CASH,
                    "no portfolio snapshot exists for the account; valuation is unknown",
                )
            instrument = uow.instruments.get(intent.symbol)
            if instrument is None:
                return self._persist_rejection(
                    uow,
                    intent,
                    account.account_id,
                    now,
                    RiskRuleId.INSTRUMENT_KNOWN,
                    "instrument is not present in the security master",
                )
            try:
                rules = self._engine.resolve(instrument, intent.earliest_execution_date)
            except JanusError as exc:
                return self._persist_rejection(
                    uow,
                    intent,
                    account.account_id,
                    now,
                    RiskRuleId.RULESET_KNOWN,
                    f"trading rules could not be resolved: {exc.message}",
                )
            bar = uow.market_bars.latest(intent.symbol)
            eligibility = self._evaluate_eligibility(instrument, intent.earliest_execution_date)
            prev_close = bar.close if bar is not None else None
            known_decision_ids = self._known_decision_ids(uow, intent)
            known_snapshot_ids = self._known_snapshot_ids(uow, intent)
            orders_today = uow.orders.count_orders_on(
                account.account_id, intent.earliest_execution_date
            )
            turnover_today = self._turnover_on(
                uow, account.account_id, intent.earliest_execution_date
            )
            drawn_down = self._drawdown(account, portfolio)
            fee_estimate = estimated_fees
            if fee_estimate is None:
                # The controller can compute a conservative reserve on its own;
                # when the engine can price the order we pass the real,
                # effective-dated, itemised fee total instead.
                fee_estimate = self._estimate_fees(rules, intent)

            decision = self._controller.evaluate(
                intent=intent,
                account=account,
                portfolio=portfolio,
                rules=rules,
                calendar=self._calendar,
                bar=bar,
                prev_close=prev_close,
                instrument=instrument,
                eligibility=eligibility,
                known_decision_ids=known_decision_ids,
                known_snapshot_ids=known_snapshot_ids,
                orders_today=orders_today,
                turnover_today=turnover_today,
                # Phase 0 has no sector/industry attribution in the instrument
                # master; the controller emits an explicit check documenting the
                # unavailability instead of silently claiming compliance.
                sector_weights={},
                drawn_down=drawn_down,
                now=now,
                estimated_fees=fee_estimate,
            )
            self._persist(uow, intent, decision, now)
            order = self._build_order(intent, decision, rules=rules, now=now)
            return decision, order

    # ------------------------------------------------------------------
    # Input gathering
    # ------------------------------------------------------------------
    def _evaluate_eligibility(
        self, instrument: Instrument, trade_date: date
    ) -> EligibilityResult | None:
        """Evaluate buy eligibility with a proven lower bound on listing age.

        If the instrument was listed before the loaded calendar window, every
        trading day in ``[calendar.min_date, trade_date]`` is provably after the
        listing, so the window length is a *sound lower bound* on the seasoning
        period: it can only prove seasoning, never deny it. Dates outside the
        calendar coverage fail closed.
        """
        if trade_date < self._calendar.min_date:
            return None
        listed_trading_days: int | None = None
        try:
            if instrument.listing_date is None or instrument.listing_date < self._calendar.min_date:
                listed_trading_days = self._calendar.count_trading_days(
                    self._calendar.min_date, trade_date
                )
            else:
                listed_trading_days = self._calendar.count_trading_days(
                    instrument.listing_date, trade_date
                )
        except JanusError:
            listed_trading_days = None
        try:
            return self._eligibility.evaluate(
                instrument, trade_date, listed_trading_days=listed_trading_days
            )
        except JanusError:  # pragma: no cover - defensive fail-closed path
            return None

    def _estimate_fees(self, rules: ResolvedTradingRules, intent: OrderIntent) -> Decimal | None:
        """Real itemised fee total from the engine, or None to use the reserve.

        Only buy-side fees are needed for the cash-affordability gate; a sell
        increases cash, so the controller's conservative treatment is fine.
        """
        if intent.side is not OrderSide.BUY or intent.limit_price is None:
            return None
        try:
            breakdown = self._engine.compute_fees(
                rules=rules,
                side=intent.side,
                quantity=intent.quantity,
                price=intent.limit_price,
            )
        except JanusError:  # pragma: no cover - conservative fallback
            return None
        return breakdown.total

    @staticmethod
    def _known_decision_ids(uow: UnitOfWork, intent: OrderIntent) -> set[str]:
        if intent.decision_id is None:
            return set()
        stored = uow.decisions.get(intent.decision_id)
        return {intent.decision_id} if stored is not None else set()

    @staticmethod
    def _known_snapshot_ids(uow: UnitOfWork, intent: OrderIntent) -> set[str]:
        if intent.data_snapshot_id is None:
            return set()
        snapshot = uow.data_snapshots.get(intent.data_snapshot_id)
        return {intent.data_snapshot_id} if snapshot is not None else set()

    @staticmethod
    def _turnover_on(uow: UnitOfWork, account_id: str, trade_date: date) -> Decimal:
        """Turnover already committed for ``trade_date`` (orders plus fills)."""
        turnover = Decimal("0")
        for order in uow.orders.list_by_trade_date(account_id, trade_date):
            if order.status.is_terminal:
                continue
            if order.limit_price is not None and order.remaining_quantity > 0:
                turnover += order.limit_price * Decimal(order.remaining_quantity)
        for fill in uow.fills.list_by_trade_date(account_id, trade_date):
            turnover += fill.notional.amount
        return quantize_cny(turnover)

    @staticmethod
    def _drawdown(account: PortfolioAccount, portfolio: PortfolioSnapshot) -> Decimal:
        initial = account.initial_capital.amount
        if initial <= 0:  # pragma: no cover - account model requires positive capital
            return Decimal("0")
        if portfolio.nav.amount >= initial:
            return Decimal("0")
        return ((initial - portfolio.nav.amount) / initial).quantize(Decimal("0.000001"))

    # ------------------------------------------------------------------
    # Decision / order construction
    # ------------------------------------------------------------------
    def _persist(
        self, uow: UnitOfWork, intent: OrderIntent, decision: RiskDecision, now: datetime
    ) -> None:
        uow.risk_decisions.append(decision)
        uow.decisions.append(
            DecisionRecord(
                decision_id=new_id("decision"),
                decision_type=DecisionType.RISK_EVALUATION,
                created_at=now,
                as_of=now,
                account_id=decision.account_id,
                proposal_id=None,
                strategy_id=intent.strategy_id,
                strategy_version=intent.strategy_version,
                data_snapshot_id=intent.data_snapshot_id,
                rule_set_id=decision.rule_set_id,
                rule_set_version=decision.rule_set_version,
                risk_decision_id=decision.risk_decision_id,
                inputs={
                    "intent_id": intent.intent_id,
                    "symbol": intent.symbol,
                    "side": intent.side.value,
                    "quantity": intent.quantity,
                    "limit_price": (
                        str(intent.limit_price) if intent.limit_price is not None else None
                    ),
                    "outcome": decision.outcome.value,
                    "failed_rules": [
                        check.rule_id.value for check in decision.checks if not check.passed
                    ],
                    "policy_version": decision.policy_version,
                },
                outcome=decision.summary,
                executable=decision.approved,
            )
        )
        uow.commit()

    def _persist_rejection(
        self,
        uow: UnitOfWork,
        intent: OrderIntent,
        account_id: str,
        now: datetime,
        rule_id: RiskRuleId,
        message: str,
    ) -> tuple[RiskDecision, PaperOrder]:
        decision = RiskDecision(
            risk_decision_id=new_id("riskdec"),
            order_intent_id=intent.intent_id,
            account_id=account_id,
            outcome=RiskOutcome.REJECT,
            checks=(RiskCheck.failed(rule_id, message),),
            policy_version=self._policy.version,
            decided_at=now,
            data_snapshot_id=intent.data_snapshot_id,
            summary=f"risk rejected: {rule_id.value}",
        )
        self._persist(uow, intent, decision, now)
        order = self._build_order(intent, decision, rules=None, now=now)
        return decision, order

    def _build_order(
        self,
        intent: OrderIntent,
        decision: RiskDecision,
        *,
        rules: ResolvedTradingRules | None,
        now: datetime,
    ) -> PaperOrder:
        rule_set_id = (
            rules.rule_set_id
            if rules is not None
            else (intent.rule_set_id or _UNRESOLVED_RULE_SET_ID)
        )
        rule_set_version = (
            rules.rule_set_version
            if rules is not None
            else (intent.rule_set_version or "unresolved")
        )
        fee_schedule_id = (
            rules.fee_schedule_id if rules is not None else _UNRESOLVED_FEE_SCHEDULE_ID
        )
        fee_schedule_version = rules.fee_schedule_version if rules is not None else "unresolved"
        proposed = PaperOrder(
            order_id=new_id("order"),
            intent_id=intent.intent_id,
            account_id=intent.account_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            validity=intent.validity,
            status=OrderStatus.PROPOSED,
            created_at=now,
            updated_at=now,
            target_trade_date=intent.earliest_execution_date,
            earliest_execution_date=intent.earliest_execution_date,
            rule_set_id=rule_set_id,
            rule_set_version=rule_set_version,
            fee_schedule_id=fee_schedule_id,
            fee_schedule_version=fee_schedule_version,
            data_snapshot_id=intent.data_snapshot_id,
            decision_id=intent.decision_id,
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            risk_decision_id=decision.risk_decision_id,
        )
        if decision.approved:
            return transition(proposed, OrderStatus.RISK_APPROVED, at=now)
        reason = "; ".join(decision.failed_reasons()) or "risk controller rejected the order"
        return transition(
            proposed,
            OrderStatus.RISK_REJECTED,
            at=now,
            rejection_reason=reason,
        )
