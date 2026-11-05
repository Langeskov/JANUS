"""Unit tests for the effective-dated market-rules engine (P0-D).

These tests build *their own* small rule-sets so the resolution logic is exercised
independently of the checked-in configuration; the contract tests load the real
``config/market_rules/`` bundle.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

import pytest
from janus.domain.common.enums import (
    Board,
    Exchange,
    FeeComponent,
    InstrumentType,
    OrderSide,
    SecurityStatus,
    SessionType,
    SettlementRule,
)
from janus.domain.common.errors import (
    ConfigurationError,
    UnsupportedEntityError,
    ValidationError,
)
from janus.domain.common.time import MARKET_TZ
from janus.domain.instruments.models import Instrument
from janus.domain.market_rules.engine import MARKET_OPEN_TIME, MarketRulesEngine
from janus.domain.market_rules.models import (
    FeeBasis,
    FeeRate,
    FeeSchedule,
    FeeScheduleStatus,
    ResolvedTradingRules,
    RuleProfile,
    RuleSetStatus,
    TradingRuleSet,
    TradingSession,
)

SESSIONS = (
    TradingSession(
        session_type=SessionType.PRE_OPEN_AUCTION,
        start_time=time(9, 15),
        end_time=time(9, 25),
    ),
    TradingSession(
        session_type=SessionType.CONTINUOUS,
        start_time=time(9, 30),
        end_time=time(11, 30),
    ),
    TradingSession(
        session_type=SessionType.CONTINUOUS,
        start_time=time(13, 0),
        end_time=time(14, 57),
    ),
    TradingSession(
        session_type=SessionType.CLOSING_AUCTION,
        start_time=time(14, 57),
        end_time=time(15, 0),
    ),
)

FEE_EFFECTIVE_FROM = datetime(2023, 1, 1, tzinfo=MARKET_TZ)
RULES_2023_FROM = datetime(2023, 1, 1, tzinfo=MARKET_TZ)
RULES_2026_FROM = datetime(2026, 7, 6, tzinfo=MARKET_TZ)
TRADE_DATE = date(2026, 11, 5)


def _fee_schedule(
    schedule_id: str,
    *,
    version: str = "test-1",
    effective_from: datetime = FEE_EFFECTIVE_FROM,
    effective_to: datetime | None = None,
) -> FeeSchedule:
    return FeeSchedule(
        fee_schedule_id=schedule_id,
        version=version,
        status=FeeScheduleStatus.ESTIMATED,
        effective_from=effective_from,
        effective_to=effective_to,
        lines=(
            FeeRate(
                component=FeeComponent.BROKER_COMMISSION,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.00025"),
                min_amount=Decimal("5.00"),
            ),
            FeeRate(
                component=FeeComponent.STAMP_DUTY,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.0005"),
                applicable_sides=(OrderSide.SELL,),
            ),
        ),
    )


def _profiles() -> tuple[RuleProfile, ...]:
    """A compact version of the 2026 baseline profile grid."""
    return (
        RuleProfile(
            profile_id="SSE_MAIN-A_SHARE",
            exchange=Exchange.SSE,
            board=Board.SSE_MAIN,
            instrument_type=InstrumentType.A_SHARE,
            settlement_rule=SettlementRule.T_PLUS_1,
            tick_size=Decimal("0.01"),
            price_limit_pct=Decimal("0.10"),
            fee_schedule_id="CN-EQUITY-DEFAULT",
        ),
        RuleProfile(
            profile_id="SZSE_MAIN-A_SHARE",
            exchange=Exchange.SZSE,
            board=Board.SZSE_MAIN,
            instrument_type=InstrumentType.A_SHARE,
            settlement_rule=SettlementRule.T_PLUS_1,
            tick_size=Decimal("0.01"),
            price_limit_pct=Decimal("0.10"),
            fee_schedule_id="CN-EQUITY-DEFAULT",
        ),
        RuleProfile(
            profile_id="SSE_MAIN-A_SHARE-RISK-WARNING",
            exchange=Exchange.SSE,
            board=Board.SSE_MAIN,
            instrument_type=InstrumentType.A_SHARE,
            risk_warning_only=True,
            settlement_rule=SettlementRule.T_PLUS_1,
            tick_size=Decimal("0.01"),
            price_limit_pct=Decimal("0.05"),
            risk_warning_price_limit_pct=Decimal("0.05"),
            fee_schedule_id="CN-EQUITY-DEFAULT",
        ),
        RuleProfile(
            profile_id="SSE_MAIN-EQUITY-ETF",
            exchange=Exchange.SSE,
            board=Board.SSE_MAIN,
            instrument_type=InstrumentType.DOMESTIC_EQUITY_ETF,
            settlement_rule=SettlementRule.T_PLUS_1,
            tick_size=Decimal("0.001"),
            price_limit_pct=Decimal("0.10"),
            fee_schedule_id="CN-ETF-DEFAULT",
        ),
        RuleProfile(
            profile_id="SZSE_MAIN-EQUITY-ETF",
            exchange=Exchange.SZSE,
            board=Board.SZSE_MAIN,
            instrument_type=InstrumentType.DOMESTIC_EQUITY_ETF,
            settlement_rule=SettlementRule.T_PLUS_1,
            tick_size=Decimal("0.001"),
            price_limit_pct=Decimal("0.10"),
            fee_schedule_id="CN-ETF-DEFAULT",
        ),
        RuleProfile(
            profile_id="SSE_MAIN-EQUITY-ETF-T0-FIXTURE",
            symbols=("510999.SH",),
            exchange=Exchange.SSE,
            board=Board.SSE_MAIN,
            instrument_type=InstrumentType.DOMESTIC_EQUITY_ETF,
            settlement_rule=SettlementRule.T_PLUS_0,
            tick_size=Decimal("0.001"),
            price_limit_pct=Decimal("0.10"),
            fee_schedule_id="CN-ETF-DEFAULT",
        ),
        RuleProfile(
            profile_id="SSE_MAIN-A_SHARE-NEW-LISTING",
            exchange=Exchange.SSE,
            board=Board.SSE_MAIN,
            instrument_type=InstrumentType.A_SHARE,
            settlement_rule=SettlementRule.T_PLUS_1,
            tick_size=Decimal("0.01"),
            no_price_limit_reason="NEW_LISTING_NO_LIMIT_STAGE",
            fee_schedule_id="CN-EQUITY-DEFAULT",
        ),
    )


def _rule_set(
    *,
    rule_set_id: str = "CN-TEST-2026.1",
    version: str = "2026.1",
    status: RuleSetStatus = RuleSetStatus.ACTIVE,
    effective_from: datetime = RULES_2026_FROM,
    effective_to: datetime | None = None,
    profiles: tuple[RuleProfile, ...] | None = None,
) -> TradingRuleSet:
    return TradingRuleSet(
        rule_set_id=rule_set_id,
        version=version,
        status=status,
        effective_from=effective_from,
        effective_to=effective_to,
        default_sessions=SESSIONS,
        profiles=profiles if profiles is not None else _profiles(),
        fee_schedules=(
            _fee_schedule("CN-EQUITY-DEFAULT"),
            _fee_schedule("CN-ETF-DEFAULT"),
        ),
    )


def _engine() -> MarketRulesEngine:
    return MarketRulesEngine((_rule_set(),))


def _instrument(
    *,
    symbol: str = "600519.SH",
    exchange: Exchange = Exchange.SSE,
    board: Board = Board.SSE_MAIN,
    instrument_type: InstrumentType = InstrumentType.A_SHARE,
    status: SecurityStatus = SecurityStatus.ACTIVE,
    listing_date: date = date(2001, 8, 27),
) -> Instrument:
    return Instrument(
        symbol=symbol,
        name="Test Instrument",
        exchange=exchange,
        board=board,
        instrument_type=instrument_type,
        status=status,
        listing_date=listing_date,
    )


def _stock() -> Instrument:
    return _instrument()


def _etf(symbol: str = "510300.SH") -> Instrument:
    return _instrument(
        symbol=symbol,
        board=Board.SSE_MAIN,
        instrument_type=InstrumentType.DOMESTIC_EQUITY_ETF,
        listing_date=date(2012, 5, 28),
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_market_open_resolution_time_is_0930() -> None:
    assert time(9, 30) == MARKET_OPEN_TIME


def test_resolve_main_board_stock_grid() -> None:
    rules = _engine().resolve(_stock(), TRADE_DATE)

    assert rules.rule_set_id == "CN-TEST-2026.1"
    assert rules.rule_set_version == "2026.1"
    assert rules.profile_id == "SSE_MAIN-A_SHARE"
    assert rules.symbol == "600519.SH"
    assert rules.trade_date == TRADE_DATE
    assert rules.settlement_rule is SettlementRule.T_PLUS_1
    assert rules.tick_size == Decimal("0.01")
    assert rules.buy_quantity_increment == 100
    assert rules.min_buy_quantity == 100
    assert rules.sell_quantity_increment == 1
    assert rules.price_limit_pct == Decimal("0.10")
    assert rules.no_price_limit_reason is None
    assert rules.fee_schedule_id == "CN-EQUITY-DEFAULT"
    assert rules.fee_schedule_version == "test-1"
    assert rules.sessions == SESSIONS
    assert rules.effective_from == RULES_2026_FROM


def test_resolve_etf_uses_etf_tick_and_t_plus_1_baseline() -> None:
    rules = _engine().resolve(_etf(), TRADE_DATE)

    assert rules.profile_id == "SSE_MAIN-EQUITY-ETF"
    assert rules.tick_size == Decimal("0.001")
    assert rules.settlement_rule is SettlementRule.T_PLUS_1
    assert rules.fee_schedule_id == "CN-ETF-DEFAULT"


def test_symbol_scoped_t0_fixture_overrides_board_profile() -> None:
    engine = _engine()
    t0_rules = engine.resolve(_etf("510999.SH"), TRADE_DATE)
    t1_rules = engine.resolve(_etf("510300.SH"), TRADE_DATE)

    assert t0_rules.profile_id == "SSE_MAIN-EQUITY-ETF-T0-FIXTURE"
    assert t0_rules.settlement_rule is SettlementRule.T_PLUS_0
    assert t1_rules.profile_id == "SSE_MAIN-EQUITY-ETF"
    assert t1_rules.settlement_rule is SettlementRule.T_PLUS_1


# ---------------------------------------------------------------------------
# Price limits
# ---------------------------------------------------------------------------


def test_price_limit_half_up_rounding_stock_tick() -> None:
    rules = _engine().resolve(_stock(), TRADE_DATE)
    limits = rules.compute_price_limits(Decimal("10.05"), is_risk_warning=False)

    assert limits.pct == Decimal("0.10")
    assert limits.limit_up == Decimal("11.06")
    assert limits.limit_down == Decimal("9.05")
    assert limits.has_limits is True


def test_price_limit_half_up_rounding_etf_tick() -> None:
    rules = _engine().resolve(_etf(), TRADE_DATE)
    limits = rules.compute_price_limits(Decimal("3.5123"), is_risk_warning=False)

    assert limits.pct == Decimal("0.10")
    assert limits.limit_up == Decimal("3.864")
    assert limits.limit_down == Decimal("3.161")


def test_risk_warning_instrument_resolves_five_percent_profile() -> None:
    st = _instrument(status=SecurityStatus.ST)
    rules = _engine().resolve(st, TRADE_DATE)

    assert rules.profile_id == "SSE_MAIN-A_SHARE-RISK-WARNING"
    limits = rules.compute_price_limits(Decimal("10.00"), is_risk_warning=True)
    assert limits.pct == Decimal("0.05")
    assert limits.limit_up == Decimal("10.50")
    assert limits.limit_down == Decimal("9.50")


def test_new_listing_stage_resolves_explicit_no_limit_profile() -> None:
    rules = _engine().resolve(_stock(), TRADE_DATE, new_listing_stage=True)

    assert rules.profile_id == "SSE_MAIN-A_SHARE-NEW-LISTING"
    assert rules.no_price_limit_reason == "NEW_LISTING_NO_LIMIT_STAGE"
    assert rules.price_limit_pct is None
    limits = rules.compute_price_limits(Decimal("10.00"), is_risk_warning=False)
    assert limits.has_limits is False
    assert limits.no_limit_reason == "NEW_LISTING_NO_LIMIT_STAGE"


def test_normal_resolution_ignores_listing_stage_profile() -> None:
    rules = _engine().resolve(_stock(), TRADE_DATE)

    assert rules.profile_id == "SSE_MAIN-A_SHARE"
    assert rules.no_price_limit_reason is None


def test_new_listing_stage_without_matching_profile_fails_closed() -> None:
    with pytest.raises(UnsupportedEntityError, match="no rule profile matches"):
        _engine().resolve(_etf(), TRADE_DATE, new_listing_stage=True)


# ---------------------------------------------------------------------------
# Effective-dated rule-set selection
# ---------------------------------------------------------------------------


def _two_era_engine() -> MarketRulesEngine:
    return MarketRulesEngine(
        (
            _rule_set(
                rule_set_id="CN-TEST-2023.1",
                version="2023.1",
                status=RuleSetStatus.SUPERSEDED,
                effective_from=RULES_2023_FROM,
                effective_to=RULES_2026_FROM,
            ),
            _rule_set(),
        )
    )


def test_rule_set_switches_across_effective_date_boundary() -> None:
    engine = _two_era_engine()

    before = engine.rule_set_for(datetime(2026, 7, 3, 10, 0, tzinfo=MARKET_TZ))
    at_boundary = engine.rule_set_for(datetime(2026, 7, 6, 0, 0, tzinfo=MARKET_TZ))
    after = engine.rule_set_for(datetime(2026, 11, 5, 9, 30, tzinfo=MARKET_TZ))

    assert before.version == "2023.1"
    assert at_boundary.version == "2026.1"
    assert after.version == "2026.1"


def test_resolve_derives_rule_set_from_trade_date_open() -> None:
    engine = _two_era_engine()

    old_rules = engine.resolve(_stock(), date(2026, 7, 3))
    new_rules = engine.resolve(_stock(), date(2026, 7, 6))

    assert old_rules.rule_set_version == "2023.1"
    assert new_rules.rule_set_version == "2026.1"


def test_explicit_at_must_agree_with_trade_date() -> None:
    engine = _two_era_engine()

    # 'at' selects the superseded rule-set but the trade date is after the switch:
    # inconsistent request -> fail closed rather than silently mixing eras.
    with pytest.raises(UnsupportedEntityError, match="does not cover the trade date"):
        engine.resolve(_stock(), TRADE_DATE, at=datetime(2026, 7, 3, 10, 0, tzinfo=MARKET_TZ))


def test_rule_set_for_without_coverage_fails_closed() -> None:
    with pytest.raises(UnsupportedEntityError, match="no registered rule set"):
        _engine().rule_set_for(datetime(2022, 1, 4, 10, 0, tzinfo=MARKET_TZ))


def test_rule_set_for_requires_aware_datetime() -> None:
    with pytest.raises(ValidationError):
        _engine().rule_set_for(datetime(2026, 11, 5, 10, 0))


def test_draft_rule_sets_are_not_selectable() -> None:
    draft = _rule_set(rule_set_id="CN-DRAFT", version="0.0.1", status=RuleSetStatus.DRAFT)
    engine = MarketRulesEngine((_rule_set(), draft))

    selected = engine.rule_set_for(datetime(2026, 11, 5, 10, 0, tzinfo=MARKET_TZ))

    assert selected.version == "2026.1"


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


def test_engine_rejects_empty_registry() -> None:
    with pytest.raises(ConfigurationError, match="at least one rule set"):
        MarketRulesEngine(())


def test_engine_requires_an_active_rule_set() -> None:
    superseded = _rule_set(status=RuleSetStatus.SUPERSEDED)
    with pytest.raises(ConfigurationError, match="ACTIVE"):
        MarketRulesEngine((superseded,))


def test_engine_rejects_duplicate_registration() -> None:
    rule_set = _rule_set()
    with pytest.raises(ConfigurationError, match="duplicate rule-set"):
        MarketRulesEngine((rule_set, rule_set))


def test_engine_rejects_overlapping_effective_windows() -> None:
    first = _rule_set(
        rule_set_id="CN-ONE",
        version="1",
        status=RuleSetStatus.SUPERSEDED,
        effective_from=RULES_2023_FROM,
        effective_to=None,
    )
    second = _rule_set(rule_set_id="CN-TWO", version="2")
    with pytest.raises(ConfigurationError, match="overlap"):
        MarketRulesEngine((first, second))


def test_resolve_rejects_cash_line() -> None:
    with pytest.raises(UnsupportedEntityError, match="cash line"):
        _engine().resolve(Instrument.cash(), TRADE_DATE)


def test_resolve_rejects_instrument_not_yet_listed() -> None:
    future_listing = _instrument(listing_date=date(2026, 11, 10))
    with pytest.raises(UnsupportedEntityError, match="not listed"):
        _engine().resolve(future_listing, TRADE_DATE)


def test_resolve_rejects_delisted_instrument() -> None:
    delisted = _instrument(status=SecurityStatus.DELISTED)
    with pytest.raises(UnsupportedEntityError, match="not listed"):
        _engine().resolve(delisted, TRADE_DATE)


def test_resolve_rejects_unsupported_board() -> None:
    chi_next = _instrument(
        symbol="300750.SZ",
        exchange=Exchange.SZSE,
        board=Board.SZSE_CHINEXT,
        listing_date=date(2018, 6, 11),
    )
    with pytest.raises(UnsupportedEntityError, match="no rule profile matches"):
        _engine().resolve(chi_next, TRADE_DATE)


def test_resolve_rejects_unsupported_instrument_type() -> None:
    option = _instrument(instrument_type=InstrumentType.OPTION)
    with pytest.raises(UnsupportedEntityError, match="no rule profile matches"):
        _engine().resolve(option, TRADE_DATE)


def test_resolve_rejects_ambiguous_profile_tie() -> None:
    duplicate = RuleProfile(
        profile_id="SSE_MAIN-A_SHARE-DUPLICATE",
        exchange=Exchange.SSE,
        board=Board.SSE_MAIN,
        instrument_type=InstrumentType.A_SHARE,
        settlement_rule=SettlementRule.T_PLUS_1,
        tick_size=Decimal("0.02"),
        price_limit_pct=Decimal("0.10"),
        fee_schedule_id="CN-EQUITY-DEFAULT",
    )
    duplicate_specificity = (*_profiles(), duplicate)
    engine = MarketRulesEngine((_rule_set(profiles=duplicate_specificity),))
    with pytest.raises(UnsupportedEntityError, match="ambiguous rule profiles"):
        engine.resolve(_stock(), TRADE_DATE)


def test_resolve_rejects_profile_without_price_limit() -> None:
    profiles = (
        RuleProfile(
            profile_id="SSE_MAIN-A_SHARE-NO-LIMIT-DECLARATION",
            exchange=Exchange.SSE,
            board=Board.SSE_MAIN,
            instrument_type=InstrumentType.A_SHARE,
            settlement_rule=SettlementRule.T_PLUS_1,
            tick_size=Decimal("0.01"),
            fee_schedule_id="CN-EQUITY-DEFAULT",
        ),
    )
    engine = MarketRulesEngine((_rule_set(profiles=profiles),))
    with pytest.raises(UnsupportedEntityError, match="neither declares a price limit"):
        engine.resolve(_stock(), TRADE_DATE)


def test_resolve_rejects_risk_warning_without_risk_warning_limit() -> None:
    profiles = tuple(
        profile for profile in _profiles() if profile.profile_id != "SSE_MAIN-A_SHARE-RISK-WARNING"
    )
    engine = MarketRulesEngine((_rule_set(profiles=profiles),))
    with pytest.raises(UnsupportedEntityError, match="risk-warning"):
        engine.resolve(_instrument(status=SecurityStatus.ST), TRADE_DATE)


def test_resolve_rejects_tick_size_not_positive() -> None:
    profiles = (
        RuleProfile(
            profile_id="SSE_MAIN-A_SHARE-ZERO-TICK",
            exchange=Exchange.SSE,
            board=Board.SSE_MAIN,
            instrument_type=InstrumentType.A_SHARE,
            settlement_rule=SettlementRule.T_PLUS_1,
            tick_size=Decimal("0"),
            price_limit_pct=Decimal("0.10"),
            fee_schedule_id="CN-EQUITY-DEFAULT",
        ),
    )
    engine = MarketRulesEngine((_rule_set(profiles=profiles),))
    with pytest.raises(UnsupportedEntityError, match="non-positive tick size"):
        engine.resolve(_stock(), TRADE_DATE)


def test_resolve_rejects_fee_schedule_not_effective_yet() -> None:
    later_fee = FeeSchedule(
        fee_schedule_id="CN-EQUITY-DEFAULT",
        version="test-later",
        effective_from=datetime(2026, 12, 1, tzinfo=MARKET_TZ),
        lines=(
            FeeRate(
                component=FeeComponent.BROKER_COMMISSION,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.00025"),
                min_amount=Decimal("5.00"),
            ),
        ),
    )
    profiles = (
        RuleProfile(
            profile_id="SSE_MAIN-A_SHARE",
            exchange=Exchange.SSE,
            board=Board.SSE_MAIN,
            instrument_type=InstrumentType.A_SHARE,
            settlement_rule=SettlementRule.T_PLUS_1,
            tick_size=Decimal("0.01"),
            price_limit_pct=Decimal("0.10"),
            fee_schedule_id="CN-EQUITY-DEFAULT",
        ),
    )
    rule_set = TradingRuleSet(
        rule_set_id="CN-TEST-2026.1",
        version="2026.1",
        effective_from=RULES_2026_FROM,
        default_sessions=SESSIONS,
        profiles=profiles,
        fee_schedules=(later_fee,),
    )
    with pytest.raises(UnsupportedEntityError, match="not effective"):
        MarketRulesEngine((rule_set,)).resolve(_stock(), TRADE_DATE)


# ---------------------------------------------------------------------------
# Fee delegation
# ---------------------------------------------------------------------------


def test_compute_fees_delegates_to_resolved_schedule() -> None:
    engine = _engine()
    rules = engine.resolve(_stock(), TRADE_DATE)

    breakdown = engine.compute_fees(
        rules=rules, side=OrderSide.BUY, quantity=1000, price=Decimal("10.00")
    )

    assert breakdown.fee_schedule_id == "CN-EQUITY-DEFAULT"
    assert breakdown.fee_schedule_version == "test-1"
    assert breakdown.notional == Decimal("10000.00")
    assert breakdown.total == Decimal("5.00")


def test_fee_schedule_returns_effective_schedule() -> None:
    engine = _engine()
    rules = engine.resolve(_stock(), TRADE_DATE)

    schedule = engine.fee_schedule(rules)

    assert schedule.fee_schedule_id == "CN-EQUITY-DEFAULT"
    assert schedule.version == "test-1"


def test_fee_schedule_rejects_point_in_time_outside_window() -> None:
    engine = _engine()
    rules = engine.resolve(_stock(), TRADE_DATE)

    with pytest.raises(UnsupportedEntityError, match="not effective"):
        engine.fee_schedule(rules, datetime(2022, 6, 1, tzinfo=MARKET_TZ))


def test_fee_schedule_rejects_unsupported_rule_set_reference() -> None:
    engine = _engine()
    rules = engine.resolve(_stock(), TRADE_DATE)
    forged = ResolvedTradingRules.model_validate(
        {**rules.model_dump(), "rule_set_id": "CN-UNKNOWN"}
    )

    with pytest.raises(UnsupportedEntityError, match="unregistered rule set"):
        engine.fee_schedule(forged)
