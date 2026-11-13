"""Risk-verdict semantics tests (PR-001 §8).

Proves that every check reports an honest verdict:

* ``PASS`` -- evaluated and compliant;
* ``FAIL`` -- evaluated and violated;
* ``NOT_APPLICABLE`` -- the rule provably does not apply (documented policy), and
  it does not block approval;
* ``INDETERMINATE`` -- a mandatory constraint could not be verified, and it
  blocks approval.

A skipped-because-unavailable check must never look like a pass, and a
``RiskDecision`` can never report an approved outcome over a blocking verdict.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from janus.domain.common.enums import (
    AdjustmentMode,
    Board,
    DataQualityStatus,
    Exchange,
    InstrumentType,
    OrderSide,
    OrderType,
    OrderValidity,
    RiskOutcome,
    SecurityStatus,
    SessionType,
    SettlementRule,
    TradingDayType,
)
from janus.domain.common.errors import ValidationError
from janus.domain.common.money import Money
from janus.domain.common.time import MARKET_TZ, PointInTime
from janus.domain.instruments.eligibility import EligibilityResult
from janus.domain.instruments.models import Instrument
from janus.domain.market_data.models import MarketBar
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.models import (
    ResolvedTradingRules,
    TradingDay,
    TradingSession,
)
from janus.domain.orders.models import OrderIntent
from janus.domain.portfolio.models import PortfolioAccount, PortfolioSnapshot, Position
from janus.domain.risk.controller import RiskController
from janus.domain.risk.models import (
    RiskCheck,
    RiskControls,
    RiskDecision,
    RiskPolicy,
    RiskRuleId,
    RiskVerdict,
)

ACCOUNT_ID = "janus-paper-cny-001"
SYMBOL = "600519.SH"
NOW = datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ)
T_DATE = date(2026, 11, 5)
EXEC_DATE = date(2026, 11, 6)
RULE_SET_ID = "SSE-SZSE-2026"
RULE_SET_VERSION = "2026.07.06"
DECISION_ID = "decision-1"
SNAPSHOT_ID = "dsnap-1"
BUY_PRICE = Decimal("10.00")
PREV_CLOSE = Decimal("9.90")


# ---------------------------------------------------------------------------
# Builders (mirrors tests/unit/test_risk_controller.py)
# ---------------------------------------------------------------------------
def _sessions() -> tuple[TradingSession, ...]:
    return (
        TradingSession(
            session_type=SessionType.CONTINUOUS,
            start_time=time(9, 30),
            end_time=time(11, 30),
        ),
        TradingSession(
            session_type=SessionType.CONTINUOUS,
            start_time=time(13, 0),
            end_time=time(15, 0),
        ),
    )


def _build_calendar() -> TradingCalendar:
    days: list[TradingDay] = []
    cursor = date(2026, 11, 2)
    while cursor <= date(2026, 11, 10):
        is_trading = cursor.weekday() < 5
        days.append(
            TradingDay(
                trade_date=cursor,
                day_type=TradingDayType.TRADING if is_trading else TradingDayType.WEEKEND,
                sessions=_sessions() if is_trading else (),
            )
        )
        cursor += timedelta(days=1)
    return TradingCalendar(days, name="risk-verdict-test")


def _pit(trade_date: date, *, offset: timedelta = timedelta(minutes=5)) -> PointInTime:
    event = datetime.combine(trade_date, time(15, 0), tzinfo=MARKET_TZ)
    return PointInTime(
        event_time=event,
        published_at=event,
        retrieved_at=event + offset,
        known_at=event + offset,
    )


def _build_account() -> PortfolioAccount:
    return PortfolioAccount.create_paper_account(
        account_id=ACCOUNT_ID,
        initial_capital=Money(amount=Decimal("100000")),
        created_at=NOW,
    )


def _build_position(
    *,
    symbol: str = SYMBOL,
    quantity: int = 1000,
    available_quantity: int | None = None,
    market_price: Decimal = BUY_PRICE,
) -> Position:
    available = quantity if available_quantity is None else available_quantity
    return Position(
        symbol=symbol,
        quantity=quantity,
        available_quantity=available,
        cost_basis=Money(amount=market_price * Decimal(quantity)),
        market_price=market_price,
        market_value=Money(amount=market_price * Decimal(quantity)),
    )


def _build_portfolio(
    *,
    cash: Decimal = Decimal("100000"),
    positions: tuple[Position, ...] = (),
) -> PortfolioSnapshot:
    gross = sum((position.market_value.amount for position in positions), Decimal("0"))
    return PortfolioSnapshot(
        snapshot_id="psnap-1",
        account_id=ACCOUNT_ID,
        as_of=NOW,
        valuation_date=T_DATE,
        cash=Money(amount=cash),
        positions=positions,
        nav=Money(amount=cash + gross),
    )


def _build_instrument(**overrides: object) -> Instrument:
    base: dict[str, object] = {
        "symbol": SYMBOL,
        "name": "Fixture Main Board Equity",
        "exchange": Exchange.SSE,
        "board": Board.SSE_MAIN,
        "instrument_type": InstrumentType.A_SHARE,
        "status": SecurityStatus.ACTIVE,
        "listing_date": date(2020, 1, 6),
    }
    base.update(overrides)
    return Instrument.model_validate(base)


def _build_rules(**overrides: object) -> ResolvedTradingRules:
    base: dict[str, object] = {
        "rule_set_id": RULE_SET_ID,
        "rule_set_version": RULE_SET_VERSION,
        "profile_id": "SSE-MAIN-A",
        "symbol": SYMBOL,
        "trade_date": EXEC_DATE,
        "settlement_rule": SettlementRule.T_PLUS_1,
        "tick_size": Decimal("0.01"),
        "buy_quantity_increment": 100,
        "min_buy_quantity": 100,
        "sell_quantity_increment": 1,
        "price_limit_pct": Decimal("0.10"),
        "risk_warning_price_limit_pct": Decimal("0.05"),
        "allowed_order_types": (OrderType.LIMIT,),
        "allowed_validities": (OrderValidity.DAY,),
        "sessions": _sessions(),
        "fee_schedule_id": "CN-EQUITY-DEFAULT",
        "fee_schedule_version": RULE_SET_VERSION,
        "effective_from": datetime(2026, 7, 6, 0, 0, tzinfo=MARKET_TZ),
    }
    base.update(overrides)
    return ResolvedTradingRules.model_validate(base)


def _build_eligibility() -> EligibilityResult:
    return EligibilityResult(symbol=SYMBOL, as_of=T_DATE, eligible=True)


def _build_bar(
    *,
    trade_date: date = T_DATE,
    quality: DataQualityStatus = DataQualityStatus.OK,
) -> MarketBar:
    close = PREV_CLOSE
    return MarketBar(
        symbol=SYMBOL,
        trade_date=trade_date,
        open=close - Decimal("0.05"),
        high=close + Decimal("0.05"),
        low=close - Decimal("0.10"),
        close=close,
        volume=1_000_000,
        adjustment=AdjustmentMode.UNADJUSTED,
        quality=quality,
        point_in_time=_pit(trade_date),
    )


def _build_intent(**overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "intent_id": "intent-1",
        "account_id": ACCOUNT_ID,
        "symbol": SYMBOL,
        "side": OrderSide.BUY,
        "quantity": 1000,
        "order_type": OrderType.LIMIT,
        "limit_price": BUY_PRICE,
        "validity": OrderValidity.DAY,
        "created_at": NOW,
        "earliest_execution_date": EXEC_DATE,
        "signal_bar_date": T_DATE,
        "decision_id": DECISION_ID,
        "data_snapshot_id": SNAPSHOT_ID,
        "strategy_id": "broad_etf_core",
        "strategy_version": "0.1.0",
        "rule_set_id": RULE_SET_ID,
        "rule_set_version": RULE_SET_VERSION,
    }
    base.update(overrides)
    return OrderIntent.model_validate(base)


def _verdict(decision: RiskDecision, rule_id: RiskRuleId) -> RiskVerdict:
    return next(check.verdict for check in decision.checks if check.rule_id is rule_id)


def _blocking_rule_ids(decision: RiskDecision) -> set[RiskRuleId]:
    return {check.rule_id for check in decision.checks if check.verdict.blocks}


_UNSET: object = object()
"""Sentinel so ``None`` can be passed explicitly (e.g. ``instrument=None``)."""


def _evaluate(
    *,
    controller: RiskController | None = None,
    intent: object = _UNSET,
    portfolio: object = _UNSET,
    rules: object = _UNSET,
    bar: object = _UNSET,
    instrument: object = _UNSET,
    **overrides: object,
) -> RiskDecision:
    active_controller = controller or RiskController(RiskPolicy())
    params: dict[str, object] = {
        "intent": _build_intent() if intent is _UNSET else intent,
        "account": _build_account(),
        "portfolio": _build_portfolio() if portfolio is _UNSET else portfolio,
        "rules": _build_rules() if rules is _UNSET else rules,
        "calendar": _build_calendar(),
        "bar": _build_bar() if bar is _UNSET else bar,
        "prev_close": PREV_CLOSE,
        "instrument": _build_instrument() if instrument is _UNSET else instrument,
        "eligibility": _build_eligibility(),
        "known_decision_ids": {DECISION_ID},
        "known_snapshot_ids": {SNAPSHOT_ID},
        "orders_today": 0,
        "turnover_today": Decimal("0"),
        "sector_weights": {},
        "drawn_down": Decimal("0"),
        "now": NOW,
    }
    params.update(overrides)
    return active_controller.evaluate(**params)  # type: ignore[arg-type]


@pytest.fixture
def evaluate() -> Callable[..., RiskDecision]:
    controller = RiskController(RiskPolicy())

    def _run(**overrides: object) -> RiskDecision:
        return _evaluate(controller=controller, **overrides)

    return _run


# ---------------------------------------------------------------------------
# RiskCheck verdict consistency
# ---------------------------------------------------------------------------
def test_ok_check_is_pass() -> None:
    check = RiskCheck.ok(RiskRuleId.PRICE_TICK, measured="1.00")
    assert check.verdict is RiskVerdict.PASS
    assert check.passed
    assert not check.verdict.blocks


def test_pass_flag_with_fail_verdict_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RiskCheck(
            rule_id=RiskRuleId.PRICE_TICK,
            verdict=RiskVerdict.FAIL,
            passed=True,
            message="contradictory",
        )


def test_fail_verdict_requires_a_message() -> None:
    with pytest.raises(ValidationError):
        RiskCheck(
            rule_id=RiskRuleId.PRICE_TICK,
            verdict=RiskVerdict.FAIL,
            passed=False,
            message="",
        )


def test_not_applicable_does_not_block() -> None:
    check = RiskCheck.not_applicable(RiskRuleId.AVAILABLE_CASH, "a sell does not consume cash")
    assert check.verdict is RiskVerdict.NOT_APPLICABLE
    assert check.passed
    assert check.verdict.is_pass
    assert not check.verdict.blocks


def test_indeterminate_blocks() -> None:
    check = RiskCheck.indeterminate(
        RiskRuleId.SECTOR_EXPOSURE, "attribution is mandatory but unavailable"
    )
    assert check.verdict is RiskVerdict.INDETERMINATE
    assert not check.passed
    assert not check.verdict.is_pass
    assert check.verdict.blocks


# ---------------------------------------------------------------------------
# RiskDecision must never approve an indeterminate mandatory check
# ---------------------------------------------------------------------------
def _decision(checks: tuple[RiskCheck, ...], outcome: RiskOutcome) -> RiskDecision:
    return RiskDecision(
        risk_decision_id="riskdec-1",
        order_intent_id="intent-1",
        account_id=ACCOUNT_ID,
        outcome=outcome,
        checks=checks,
        policy_version="0.1.0",
        decided_at=NOW,
    )


def test_pass_outcome_with_indeterminate_check_raises() -> None:
    checks = (
        RiskCheck.ok(RiskRuleId.PRICE_TICK, measured="1.00"),
        RiskCheck.indeterminate(RiskRuleId.SECTOR_EXPOSURE, "attribution unavailable"),
    )
    with pytest.raises(ValidationError):
        _decision(checks, RiskOutcome.PASS)


def test_reject_outcome_with_indeterminate_check_is_valid() -> None:
    checks = (
        RiskCheck.ok(RiskRuleId.PRICE_TICK, measured="1.00"),
        RiskCheck.indeterminate(RiskRuleId.SECTOR_EXPOSURE, "attribution unavailable"),
    )
    decision = _decision(checks, RiskOutcome.REJECT)
    assert not decision.approved
    assert decision.indeterminate_rule_ids == (RiskRuleId.SECTOR_EXPOSURE,)
    assert RiskRuleId.SECTOR_EXPOSURE in decision.failed_rule_ids
    assert any("[INDETERMINATE]" in reason for reason in decision.failed_reasons())


def test_pass_outcome_with_not_applicable_check_is_valid() -> None:
    checks = (
        RiskCheck.ok(RiskRuleId.PRICE_TICK, measured="1.00"),
        RiskCheck.not_applicable(RiskRuleId.AVAILABLE_CASH, "sell needs no cash"),
    )
    decision = _decision(checks, RiskOutcome.PASS)
    assert decision.approved
    assert decision.not_applicable_rule_ids == (RiskRuleId.AVAILABLE_CASH,)
    assert decision.failed_rule_ids == ()
    assert decision.indeterminate_rule_ids == ()


def test_reject_outcome_requires_a_blocking_check() -> None:
    checks = (RiskCheck.ok(RiskRuleId.PRICE_TICK, measured="1.00"),)
    with pytest.raises(ValidationError):
        _decision(checks, RiskOutcome.REJECT)


# ---------------------------------------------------------------------------
# SECTOR_EXPOSURE verdicts
# ---------------------------------------------------------------------------
def test_verified_sector_attribution_below_limit_is_pass(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(sector="BANKS", sector_weights={"BANKS": Decimal("0.20")})
    assert _verdict(decision, RiskRuleId.SECTOR_EXPOSURE) is RiskVerdict.PASS
    assert decision.outcome is RiskOutcome.PASS
    assert RiskRuleId.SECTOR_EXPOSURE not in _blocking_rule_ids(decision)
    assert RiskRuleId.SECTOR_EXPOSURE not in decision.not_applicable_rule_ids


def test_verified_sector_attribution_above_limit_is_fail(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(sector="BANKS", sector_weights={"BANKS": Decimal("0.35")})
    assert _verdict(decision, RiskRuleId.SECTOR_EXPOSURE) is RiskVerdict.FAIL
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.SECTOR_EXPOSURE in decision.failed_rule_ids
    assert RiskRuleId.SECTOR_EXPOSURE not in decision.indeterminate_rule_ids


def test_missing_attribution_not_required_is_not_applicable(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate()
    assert _verdict(decision, RiskRuleId.SECTOR_EXPOSURE) is RiskVerdict.NOT_APPLICABLE
    assert decision.outcome is RiskOutcome.PASS
    assert RiskRuleId.SECTOR_EXPOSURE in decision.not_applicable_rule_ids
    assert RiskRuleId.SECTOR_EXPOSURE not in decision.failed_rule_ids


def test_missing_attribution_required_is_indeterminate() -> None:
    policy = RiskPolicy(controls=RiskControls(require_sector_attribution=True))
    decision = _evaluate(controller=RiskController(policy))
    assert _verdict(decision, RiskRuleId.SECTOR_EXPOSURE) is RiskVerdict.INDETERMINATE
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.SECTOR_EXPOSURE in decision.failed_rule_ids
    assert decision.indeterminate_rule_ids == (RiskRuleId.SECTOR_EXPOSURE,)
    assert any("[INDETERMINATE]" in reason for reason in decision.failed_reasons())


def test_summary_distinguishes_fail_from_indeterminate() -> None:
    policy = RiskPolicy(controls=RiskControls(require_sector_attribution=True))
    decision = _evaluate(
        controller=RiskController(policy),
        intent=_build_intent(quantity=150),
    )
    assert _verdict(decision, RiskRuleId.QUANTITY_INCREMENT) is RiskVerdict.FAIL
    assert _verdict(decision, RiskRuleId.SECTOR_EXPOSURE) is RiskVerdict.INDETERMINATE
    assert "FAIL" in decision.summary
    assert "INDETERMINATE" in decision.summary
    reasons = decision.failed_reasons()
    assert any("[FAIL]" in reason for reason in reasons)
    assert any("[INDETERMINATE]" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# Other skipped/not-evaluated paths are honest NOT_APPLICABLE
# ---------------------------------------------------------------------------
def test_buy_path_not_applicable_rules(evaluate: Callable[..., RiskDecision]) -> None:
    decision = evaluate()
    assert decision.outcome is RiskOutcome.PASS
    assert _verdict(decision, RiskRuleId.AVAILABLE_QUANTITY) is RiskVerdict.NOT_APPLICABLE
    assert _verdict(decision, RiskRuleId.NO_SHORT) is RiskVerdict.NOT_APPLICABLE


def test_sell_path_not_applicable_rules(evaluate: Callable[..., RiskDecision]) -> None:
    portfolio = _build_portfolio(
        cash=Decimal("90000"),
        positions=(_build_position(quantity=1000, available_quantity=1000),),
    )
    decision = evaluate(
        intent=_build_intent(side=OrderSide.SELL, quantity=1000),
        portfolio=portfolio,
    )
    assert decision.outcome is RiskOutcome.PASS
    for rule_id in (
        RiskRuleId.AVAILABLE_CASH,
        RiskRuleId.INSTRUMENT_ELIGIBLE,
        RiskRuleId.SINGLE_INSTRUMENT_WEIGHT,
        RiskRuleId.NO_LEVERAGE,
    ):
        assert _verdict(decision, rule_id) is RiskVerdict.NOT_APPLICABLE
    assert _verdict(decision, RiskRuleId.AVAILABLE_QUANTITY) is RiskVerdict.PASS
    assert _verdict(decision, RiskRuleId.NO_SHORT) is RiskVerdict.PASS


def test_disabled_controls_are_not_applicable_and_do_not_block() -> None:
    policy = RiskPolicy(
        controls=RiskControls(
            require_decision_reference=False,
            require_known_ruleset=False,
            require_fresh_market_data=False,
        )
    )
    decision = _evaluate(
        controller=RiskController(policy),
        intent=_build_intent(decision_id=None, rule_set_id=None, rule_set_version=None),
        bar=_build_bar(trade_date=date(2026, 10, 20)),
    )
    assert decision.outcome is RiskOutcome.PASS
    for rule_id in (
        RiskRuleId.DECISION_REFERENCE,
        RiskRuleId.RULESET_KNOWN,
        RiskRuleId.MARKET_DATA_FRESH,
    ):
        assert _verdict(decision, rule_id) is RiskVerdict.NOT_APPLICABLE
        assert rule_id not in decision.failed_rule_ids


def test_declared_no_price_limit_is_not_applicable(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(
        rules=_build_rules(price_limit_pct=None, no_price_limit_reason="LISTING_STAGE")
    )
    assert decision.outcome is RiskOutcome.PASS
    assert _verdict(decision, RiskRuleId.PRICE_LIMIT_BAND) is RiskVerdict.NOT_APPLICABLE


def test_non_limit_order_tick_check_is_not_applicable(
    evaluate: Callable[..., RiskDecision],
) -> None:
    market_intent = _build_intent().model_copy(update={"order_type": OrderType.MARKET})
    decision = evaluate(intent=market_intent)
    assert _verdict(decision, RiskRuleId.PRICE_TICK) is RiskVerdict.NOT_APPLICABLE
    assert _verdict(decision, RiskRuleId.ORDER_TYPE_SUPPORTED) is RiskVerdict.FAIL
    assert decision.outcome is RiskOutcome.REJECT


def test_unverifiable_derivative_rule_is_indeterminate(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(instrument=None)
    assert _verdict(decision, RiskRuleId.NO_DERIVATIVES) is RiskVerdict.INDETERMINATE
    assert RiskRuleId.NO_DERIVATIVES in decision.indeterminate_rule_ids
    assert decision.outcome is RiskOutcome.REJECT


# ---------------------------------------------------------------------------
# Regression: a fully evaluable buy still approves and emits every rule
# ---------------------------------------------------------------------------
def test_fully_evaluable_buy_approves_with_all_rules_emitted(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate()
    assert decision.outcome is RiskOutcome.PASS
    assert decision.approved
    assert decision.failed_rule_ids == ()
    assert decision.indeterminate_rule_ids == ()
    assert {check.rule_id for check in decision.checks} == set(RiskRuleId)
    assert len(decision.checks) == len(RiskRuleId)
    assert "approved" in decision.summary
    assert "not applicable" in decision.summary


def test_blocking_sell_is_rejected_and_reports_verdicts(
    evaluate: Callable[..., RiskDecision],
) -> None:
    portfolio = _build_portfolio(
        cash=Decimal("90000"),
        positions=(_build_position(quantity=1000, available_quantity=0),),
    )
    decision = evaluate(
        intent=_build_intent(side=OrderSide.SELL, quantity=1000),
        portfolio=portfolio,
    )
    assert decision.outcome is RiskOutcome.REJECT
    assert _verdict(decision, RiskRuleId.AVAILABLE_QUANTITY) is RiskVerdict.FAIL
    assert "[FAIL]" in decision.summary
