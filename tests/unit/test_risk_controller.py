"""Focused unit tests for the deterministic risk controller (Phase 0 spec s8).

Each rule gets its own focused test: the baseline intent is approved, and one
input is then perturbed so exactly the rule under test (plus any genuinely
related cascades) fails. Every test also proves that the controller *returns* a
REJECT decision instead of raising.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from janus.domain.common.enums import (
    AccountState,
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
from janus.domain.instruments.eligibility import EligibilityResult, EligibilityViolation
from janus.domain.instruments.models import Instrument
from janus.domain.market_data.models import MarketBar
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.models import (
    ResolvedTradingRules,
    TradingDay,
    TradingSession,
)
from janus.domain.orders.models import OrderIntent
from janus.domain.portfolio.models import (
    AccountMode,
    PortfolioAccount,
    PortfolioSnapshot,
    Position,
)
from janus.domain.risk.controller import RiskController
from janus.domain.risk.models import (
    RiskDecision,
    RiskLimits,
    RiskPolicy,
    RiskRuleId,
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
# Builders
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
    end = date(2026, 11, 10)
    while cursor <= end:
        is_trading = cursor.weekday() < 5
        days.append(
            TradingDay(
                trade_date=cursor,
                day_type=TradingDayType.TRADING if is_trading else TradingDayType.WEEKEND,
                sessions=_sessions() if is_trading else (),
            )
        )
        cursor += timedelta(days=1)
    return TradingCalendar(days, name="risk-test")


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


def _build_eligibility(
    *,
    eligible: bool = True,
    violations: tuple[EligibilityViolation, ...] = (),
    symbol: str = SYMBOL,
) -> EligibilityResult:
    return EligibilityResult(symbol=symbol, as_of=T_DATE, eligible=eligible, violations=violations)


def _build_bar(
    *,
    trade_date: date = T_DATE,
    symbol: str = SYMBOL,
    close: Decimal = PREV_CLOSE,
    quality: DataQualityStatus = DataQualityStatus.OK,
    adjustment: AdjustmentMode = AdjustmentMode.UNADJUSTED,
    volume: int = 1_000_000,
    point_in_time: PointInTime | None = None,
) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        trade_date=trade_date,
        open=close - Decimal("0.05"),
        high=close + Decimal("0.05"),
        low=close - Decimal("0.10"),
        close=close,
        volume=volume,
        adjustment=adjustment,
        quality=quality,
        point_in_time=point_in_time or _pit(trade_date),
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


def _failed(decision: RiskDecision) -> set[RiskRuleId]:
    return {check.rule_id for check in decision.checks if not check.passed}


@pytest.fixture
def evaluate() -> Callable[..., RiskDecision]:
    controller = RiskController(RiskPolicy())

    def _evaluate(**overrides: object) -> RiskDecision:
        params: dict[str, object] = {
            "intent": _build_intent(),
            "account": _build_account(),
            "portfolio": _build_portfolio(),
            "rules": _build_rules(),
            "calendar": _build_calendar(),
            "bar": _build_bar(),
            "prev_close": PREV_CLOSE,
            "instrument": _build_instrument(),
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
        return controller.evaluate(**params)  # type: ignore[arg-type]

    return _evaluate


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------
def test_baseline_buy_is_approved_and_emits_every_rule(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate()
    assert decision.outcome is RiskOutcome.PASS
    assert decision.approved
    assert decision.checks
    assert {check.rule_id for check in decision.checks} == set(RiskRuleId)
    assert len(decision.checks) == len(RiskRuleId)
    assert decision.rule_set_id == RULE_SET_ID
    assert decision.rule_set_version == RULE_SET_VERSION
    assert decision.data_snapshot_id == SNAPSHOT_ID
    assert decision.account_id == ACCOUNT_ID
    assert decision.order_intent_id == "intent-1"
    assert decision.policy_version == RiskPolicy().version
    assert "approved" in decision.summary


# ---------------------------------------------------------------------------
# Environment / account / kill switch
# ---------------------------------------------------------------------------
def test_live_account_mode_is_rejected(evaluate: Callable[..., RiskDecision]) -> None:
    account = _build_account().model_copy(update={"mode": AccountMode.LIVE})
    decision = evaluate(account=account)
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.ENVIRONMENT_PAPER_ONLY in _failed(decision)


def test_kill_switch_blocks_new_orders(evaluate: Callable[..., RiskDecision]) -> None:
    paused = _build_account().paused("operator kill switch", NOW, new_version=2)
    decision = evaluate(account=paused)
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.KILL_SWITCH_ENGAGED in _failed(decision)
    assert RiskRuleId.ACCOUNT_ACTIVE in _failed(decision)
    # The kill switch only blocks new orders; it never forces liquidation.
    assert any(
        "kill switch" in check.message.lower()
        for check in decision.checks
        if check.rule_id is RiskRuleId.KILL_SWITCH_ENGAGED
    )


def test_inactive_account_is_rejected(evaluate: Callable[..., RiskDecision]) -> None:
    closed = _build_account().model_copy(update={"state": AccountState.CLOSED})
    decision = evaluate(account=closed)
    assert RiskRuleId.ACCOUNT_ACTIVE in _failed(decision)
    assert RiskRuleId.KILL_SWITCH_ENGAGED not in _failed(decision)


def test_intent_for_another_account_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(account_id="janus-paper-cny-999"))
    assert RiskRuleId.ACCOUNT_ACTIVE in _failed(decision)


# ---------------------------------------------------------------------------
# Traceability references (acceptance criterion 9)
# ---------------------------------------------------------------------------
def test_missing_decision_reference_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(decision_id=None))
    assert RiskRuleId.DECISION_REFERENCE in _failed(decision)


def test_unknown_decision_reference_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(
        intent=_build_intent(decision_id="decision-unknown"),
        known_decision_ids={DECISION_ID},
    )
    assert RiskRuleId.DECISION_REFERENCE in _failed(decision)


def test_missing_snapshot_reference_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(data_snapshot_id=None))
    assert RiskRuleId.DATA_SNAPSHOT_REFERENCE in _failed(decision)


def test_unknown_snapshot_reference_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(
        intent=_build_intent(data_snapshot_id="dsnap-unknown"),
        known_snapshot_ids={SNAPSHOT_ID},
    )
    assert RiskRuleId.DATA_SNAPSHOT_REFERENCE in _failed(decision)


def test_missing_strategy_reference_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(strategy_id=None, strategy_version=None))
    assert RiskRuleId.STRATEGY_VERSION in _failed(decision)


def test_non_semver_strategy_version_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(strategy_version="latest"))
    assert RiskRuleId.STRATEGY_VERSION in _failed(decision)


# ---------------------------------------------------------------------------
# Rule set
# ---------------------------------------------------------------------------
def test_missing_rule_set_reference_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(rule_set_id=None))
    assert RiskRuleId.RULESET_KNOWN in _failed(decision)


def test_rule_set_version_mismatch_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(rule_set_version="2023.01.01"))
    assert RiskRuleId.RULESET_KNOWN in _failed(decision)


def test_rules_resolved_for_another_date_are_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(rules=_build_rules(trade_date=T_DATE))
    assert RiskRuleId.RULESET_KNOWN in _failed(decision)


# ---------------------------------------------------------------------------
# Instrument identity / eligibility
# ---------------------------------------------------------------------------
def test_unknown_instrument_is_rejected(evaluate: Callable[..., RiskDecision]) -> None:
    decision = evaluate(instrument=None)
    assert RiskRuleId.INSTRUMENT_KNOWN in _failed(decision)


def test_mismatched_instrument_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(instrument=_build_instrument(symbol="600036.SH"))
    assert RiskRuleId.INSTRUMENT_KNOWN in _failed(decision)


def test_ineligible_instrument_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    eligibility = _build_eligibility(eligible=False, violations=(EligibilityViolation.SUSPENDED,))
    decision = evaluate(eligibility=eligibility)
    assert RiskRuleId.INSTRUMENT_ELIGIBLE in _failed(decision)


def test_missing_eligibility_is_rejected_fail_closed(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(eligibility=None)
    assert RiskRuleId.INSTRUMENT_ELIGIBLE in _failed(decision)


# ---------------------------------------------------------------------------
# Market-data freshness and quality
# ---------------------------------------------------------------------------
def test_stale_bar_is_rejected(evaluate: Callable[..., RiskDecision]) -> None:
    decision = evaluate(bar=_build_bar(trade_date=date(2026, 10, 20)))
    assert RiskRuleId.MARKET_DATA_FRESH in _failed(decision)


def test_missing_bar_is_rejected(evaluate: Callable[..., RiskDecision]) -> None:
    decision = evaluate(bar=None)
    assert RiskRuleId.MARKET_DATA_FRESH in _failed(decision)
    assert RiskRuleId.DATA_QUALITY in _failed(decision)


def test_quarantined_bar_is_rejected(evaluate: Callable[..., RiskDecision]) -> None:
    decision = evaluate(bar=_build_bar(quality=DataQualityStatus.QUARANTINED))
    assert RiskRuleId.DATA_QUALITY in _failed(decision)
    assert RiskRuleId.MARKET_DATA_FRESH not in _failed(decision)


def test_adjusted_bar_is_rejected(evaluate: Callable[..., RiskDecision]) -> None:
    decision = evaluate(bar=_build_bar(adjustment=AdjustmentMode.FORWARD_ADJUSTED))
    assert RiskRuleId.DATA_QUALITY in _failed(decision)


def test_zero_volume_bar_is_rejected(evaluate: Callable[..., RiskDecision]) -> None:
    decision = evaluate(bar=_build_bar(volume=0))
    assert RiskRuleId.DATA_QUALITY in _failed(decision)


def test_bar_known_in_the_future_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    future_pit = PointInTime(
        event_time=NOW,
        published_at=NOW,
        retrieved_at=NOW + timedelta(hours=1),
        known_at=NOW + timedelta(hours=1),
    )
    decision = evaluate(bar=_build_bar(point_in_time=future_pit))
    assert RiskRuleId.DATA_QUALITY in _failed(decision)


def test_bar_for_another_symbol_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(bar=_build_bar(symbol="600036.SH"))
    assert RiskRuleId.MARKET_DATA_FRESH in _failed(decision)
    assert RiskRuleId.DATA_QUALITY in _failed(decision)


# ---------------------------------------------------------------------------
# Order type, quantity grid, price grid and price limits
# ---------------------------------------------------------------------------
def test_order_validity_not_allowed_by_rules_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(rules=_build_rules(allowed_validities=(OrderValidity.IOC,)))
    assert RiskRuleId.ORDER_TYPE_SUPPORTED in _failed(decision)


def test_quantity_increment_violation_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(quantity=150))
    assert RiskRuleId.QUANTITY_INCREMENT in _failed(decision)


def test_quantity_below_minimum_lot_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(quantity=50))
    assert RiskRuleId.QUANTITY_INCREMENT in _failed(decision)


def test_price_tick_violation_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(
        intent=_build_intent(limit_price=Decimal("10.005")),
        prev_close=Decimal("9.90"),
    )
    assert RiskRuleId.PRICE_TICK in _failed(decision)


def test_price_above_limit_up_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(limit_price=Decimal("11.00")))
    assert RiskRuleId.PRICE_LIMIT_BAND in _failed(decision)


def test_price_below_limit_down_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(limit_price=Decimal("8.00")))
    assert RiskRuleId.PRICE_LIMIT_BAND in _failed(decision)


def test_missing_previous_close_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(prev_close=None)
    assert RiskRuleId.PRICE_LIMIT_BAND in _failed(decision)


def test_explicit_no_limit_rule_passes(evaluate: Callable[..., RiskDecision]) -> None:
    decision = evaluate(
        rules=_build_rules(price_limit_pct=None, no_price_limit_reason="LISTING_STAGE")
    )
    assert RiskRuleId.PRICE_LIMIT_BAND not in _failed(decision)


# ---------------------------------------------------------------------------
# Session validity and signal look-ahead
# ---------------------------------------------------------------------------
def test_non_trading_execution_date_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    weekend = date(2026, 11, 7)
    decision = evaluate(
        intent=_build_intent(earliest_execution_date=weekend),
        rules=_build_rules(trade_date=weekend),
    )
    assert RiskRuleId.SESSION_VALID in _failed(decision)


def test_execution_date_outside_calendar_coverage_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    far_future = date(2026, 12, 15)
    decision = evaluate(
        intent=_build_intent(earliest_execution_date=far_future),
        rules=_build_rules(trade_date=far_future),
    )
    assert RiskRuleId.SESSION_VALID in _failed(decision)


def test_same_day_signal_cannot_fill_same_day(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(
        intent=_build_intent(earliest_execution_date=T_DATE),
        rules=_build_rules(trade_date=T_DATE),
    )
    assert RiskRuleId.SIGNAL_LOOKAHEAD in _failed(decision)


def test_bar_date_equal_to_execution_date_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    now_on_exec = datetime(2026, 11, 6, 15, 30, tzinfo=MARKET_TZ)
    decision = evaluate(
        intent=_build_intent(signal_bar_date=None),
        rules=_build_rules(trade_date=EXEC_DATE),
        bar=_build_bar(trade_date=EXEC_DATE),
        now=now_on_exec,
    )
    assert RiskRuleId.SIGNAL_LOOKAHEAD in _failed(decision)


# ---------------------------------------------------------------------------
# Cash / quantity affordability
# ---------------------------------------------------------------------------
def test_insufficient_cash_is_rejected(evaluate: Callable[..., RiskDecision]) -> None:
    decision = evaluate(portfolio=_build_portfolio(cash=Decimal("5000")))
    assert RiskRuleId.AVAILABLE_CASH in _failed(decision)


def test_conservative_fee_reserve_can_reject_a_borderline_buy(
    evaluate: Callable[..., RiskDecision],
) -> None:
    borderline = _build_portfolio(cash=Decimal("10005"))
    with_reserve = evaluate(portfolio=borderline)
    assert RiskRuleId.AVAILABLE_CASH in _failed(with_reserve)
    exact_fees = evaluate(portfolio=borderline, estimated_fees=Decimal("2.00"))
    assert RiskRuleId.AVAILABLE_CASH not in _failed(exact_fees)


def test_sell_allowed_quantity_respects_t_plus_one(
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
    assert RiskRuleId.AVAILABLE_QUANTITY in _failed(decision)


def test_sell_without_position_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(side=OrderSide.SELL, quantity=1000))
    assert RiskRuleId.AVAILABLE_QUANTITY in _failed(decision)
    assert RiskRuleId.NO_SHORT in _failed(decision)


def test_odd_lot_sell_of_whole_position_is_allowed(
    evaluate: Callable[..., RiskDecision],
) -> None:
    portfolio = _build_portfolio(
        cash=Decimal("98500"),
        positions=(_build_position(quantity=150, available_quantity=150),),
    )
    decision = evaluate(
        intent=_build_intent(side=OrderSide.SELL, quantity=150),
        portfolio=portfolio,
    )
    assert decision.outcome is RiskOutcome.PASS
    assert RiskRuleId.QUANTITY_INCREMENT not in _failed(decision)


def test_partial_odd_lot_sell_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    portfolio = _build_portfolio(
        cash=Decimal("98000"),
        positions=(_build_position(quantity=200, available_quantity=200),),
    )
    decision = evaluate(
        intent=_build_intent(side=OrderSide.SELL, quantity=150),
        portfolio=portfolio,
    )
    assert RiskRuleId.QUANTITY_INCREMENT in _failed(decision)


# ---------------------------------------------------------------------------
# Exposure and activity limits
# ---------------------------------------------------------------------------
def test_single_instrument_post_trade_weight_limit_is_enforced(
    evaluate: Callable[..., RiskDecision],
) -> None:
    # The intent's own symbol is already 50% of NAV; adding 10% more breaches it.
    portfolio = _build_portfolio(
        cash=Decimal("50000"),
        positions=(_build_position(symbol=SYMBOL, quantity=5000),),
    )
    decision = evaluate(portfolio=portfolio)
    assert RiskRuleId.SINGLE_INSTRUMENT_WEIGHT in _failed(decision)


def test_existing_portfolio_breach_blocks_new_buys(
    evaluate: Callable[..., RiskDecision],
) -> None:
    # Another instrument already sits at 50% of NAV (> 10% limit): new risk-taking
    # must fail closed even though the new order itself would only be 10%.
    portfolio = _build_portfolio(
        cash=Decimal("50000"),
        positions=(_build_position(symbol="600036.SH", quantity=5000),),
    )
    decision = evaluate(portfolio=portfolio)
    assert RiskRuleId.SINGLE_INSTRUMENT_WEIGHT in _failed(decision)


def test_risk_reducing_sell_of_a_concentrated_position_is_allowed(
    evaluate: Callable[..., RiskDecision],
) -> None:
    portfolio = _build_portfolio(
        cash=Decimal("50000"),
        positions=(_build_position(symbol=SYMBOL, quantity=5000),),
    )
    decision = evaluate(
        intent=_build_intent(side=OrderSide.SELL, quantity=1000),
        portfolio=portfolio,
    )
    assert RiskRuleId.SINGLE_INSTRUMENT_WEIGHT not in _failed(decision)
    assert decision.outcome is RiskOutcome.PASS


def test_min_cash_weight_limit_is_enforced(
    evaluate: Callable[..., RiskDecision],
) -> None:
    portfolio = _build_portfolio(
        cash=Decimal("25000"),
        positions=(
            _build_position(symbol="600036.SH", quantity=1000, market_price=Decimal("25")),
            _build_position(symbol="510300.SH", quantity=10000, market_price=Decimal("5")),
        ),
    )
    decision = evaluate(portfolio=portfolio)
    assert RiskRuleId.MIN_CASH_WEIGHT in _failed(decision)


def test_daily_turnover_limit_is_enforced(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(turnover_today=Decimal("15000"))
    assert RiskRuleId.DAILY_TURNOVER in _failed(decision)


def test_max_orders_per_day_is_enforced(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(orders_today=5)
    assert RiskRuleId.MAX_ORDERS_PER_DAY in _failed(decision)


def test_gross_exposure_limit_is_enforced() -> None:
    policy = RiskPolicy(
        limits=RiskLimits(
            max_single_instrument_weight=Decimal("1"),
            min_cash_weight=Decimal("0"),
            max_gross_exposure=Decimal("0.50"),
        )
    )
    controller = RiskController(policy)
    decision = controller.evaluate(
        intent=_build_intent(quantity=6000),
        account=_build_account(),
        portfolio=_build_portfolio(),
        rules=_build_rules(),
        calendar=_build_calendar(),
        bar=_build_bar(),
        prev_close=PREV_CLOSE,
        instrument=_build_instrument(),
        eligibility=_build_eligibility(),
        known_decision_ids={DECISION_ID},
        known_snapshot_ids={SNAPSHOT_ID},
        orders_today=0,
        turnover_today=Decimal("0"),
        sector_weights={},
        drawn_down=Decimal("0"),
        now=NOW,
    )
    assert RiskRuleId.GROSS_EXPOSURE in _failed(decision)


def test_sector_exposure_limit_is_enforced_when_attributed(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(
        sector="BANKS",
        sector_weights={"BANKS": Decimal("0.35")},
    )
    assert RiskRuleId.SECTOR_EXPOSURE in _failed(decision)


def test_unknown_sector_key_fails_closed(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(sector="BANKS", sector_weights={})
    assert RiskRuleId.SECTOR_EXPOSURE in _failed(decision)


def test_sector_attribution_unavailability_is_explicit(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate()
    sector_check = next(
        check for check in decision.checks if check.rule_id is RiskRuleId.SECTOR_EXPOSURE
    )
    assert sector_check.passed
    assert sector_check.measured == "sector_attribution_unavailable"


def test_drawdown_pause_blocks_new_orders(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(drawn_down=Decimal("0.12"))
    assert RiskRuleId.DRAWDOWN_PAUSE in _failed(decision)


def test_drawdown_at_threshold_pauses(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(drawn_down=Decimal("0.10"))
    assert RiskRuleId.DRAWDOWN_PAUSE in _failed(decision)


def test_drawdown_below_threshold_does_not_pause(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(drawn_down=Decimal("0.099"))
    assert RiskRuleId.DRAWDOWN_PAUSE not in _failed(decision)


# ---------------------------------------------------------------------------
# Hard Phase 0 restrictions
# ---------------------------------------------------------------------------
def test_short_sale_beyond_owned_position_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    portfolio = _build_portfolio(
        cash=Decimal("90000"),
        positions=(_build_position(quantity=1000, available_quantity=1000),),
    )
    decision = evaluate(
        intent=_build_intent(side=OrderSide.SELL, quantity=1100),
        portfolio=portfolio,
    )
    assert RiskRuleId.NO_SHORT in _failed(decision)


def test_leverage_attempt_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(portfolio=_build_portfolio(cash=Decimal("5000")))
    assert RiskRuleId.NO_LEVERAGE in _failed(decision)


def test_derivative_instrument_is_rejected(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(instrument=_build_instrument(instrument_type=InstrumentType.OPTION))
    assert RiskRuleId.NO_DERIVATIVES in _failed(decision)


# ---------------------------------------------------------------------------
# Programmer errors raise; business rejections do not
# ---------------------------------------------------------------------------
def test_naive_now_is_a_programmer_error(
    evaluate: Callable[..., RiskDecision],
) -> None:
    with pytest.raises(ValidationError):
        evaluate(now=datetime(2026, 11, 5, 15, 30))


def test_negative_drawdown_is_a_programmer_error(
    evaluate: Callable[..., RiskDecision],
) -> None:
    with pytest.raises(ValidationError):
        evaluate(drawn_down=Decimal("-0.01"))


def test_negative_order_count_is_a_programmer_error(
    evaluate: Callable[..., RiskDecision],
) -> None:
    with pytest.raises(ValidationError):
        evaluate(orders_today=-1)


def test_rejection_is_returned_not_raised(
    evaluate: Callable[..., RiskDecision],
) -> None:
    decision = evaluate(intent=_build_intent(quantity=150))
    assert decision.outcome is RiskOutcome.REJECT
    assert decision.failed_rule_ids
    assert "risk rejected" in decision.summary
