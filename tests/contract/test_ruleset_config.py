"""Contract tests for the checked-in market-rules configuration bundle (P0-D).

These tests load the real ``config/market_rules/`` documents through the
infrastructure registry. They pin the data contract the rest of JANUS depends on:
rule-set ids/effective dates/source references, the required profile grid, the
ESTIMATED fee schedules, the deterministic calendar, and the strict loader's
fail-closed behaviour.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
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
from janus.domain.common.errors import ConfigurationError, UnsupportedEntityError
from janus.domain.common.time import MARKET_TZ
from janus.domain.instruments.models import Instrument
from janus.domain.market_rules.models import RuleSetStatus
from janus.infrastructure.rulesets import (
    RuleSetRegistry,
    default_config_dir,
    load_calendar,
    load_fee_schedules,
    load_rule_sets,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRADE_DATE = date(2026, 11, 5)
SWITCH_DATE = datetime(2026, 7, 6, tzinfo=MARKET_TZ)

SSE_RULES_URL = (
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/"
    "c/c_20260424_10816492.shtml"
)
SZSE_RULES_URL = "https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html"


@pytest.fixture(scope="module")
def registry() -> RuleSetRegistry:
    return RuleSetRegistry.from_default_config()


def _instrument(
    symbol: str,
    exchange: Exchange,
    board: Board,
    instrument_type: InstrumentType,
    *,
    status: SecurityStatus = SecurityStatus.ACTIVE,
    listing_date: date = date(2010, 1, 4),
) -> Instrument:
    return Instrument(
        symbol=symbol,
        name=f"Test {symbol}",
        exchange=exchange,
        board=board,
        instrument_type=instrument_type,
        status=status,
        listing_date=listing_date,
    )


# ---------------------------------------------------------------------------
# Registry / rule-set baseline
# ---------------------------------------------------------------------------


def test_default_config_dir_points_at_checked_in_bundle() -> None:
    config_dir = default_config_dir()

    assert config_dir.is_dir()
    assert config_dir.name == "market_rules"
    assert (config_dir / "ruleset_cn_2026_07_06.yaml").is_file()


def test_registry_registers_both_baselines(registry: RuleSetRegistry) -> None:
    rule_sets = registry.rule_sets()
    by_id = {rule_set.rule_set_id: rule_set for rule_set in rule_sets}

    assert set(by_id) == {"CN-SSE-SZSE-2026.1", "CN-SSE-SZSE-2023.1"}
    current = by_id["CN-SSE-SZSE-2026.1"]
    previous = by_id["CN-SSE-SZSE-2023.1"]

    assert current.version == "2026.1"
    assert current.status is RuleSetStatus.ACTIVE
    assert current.effective_from == SWITCH_DATE
    assert current.effective_to is None

    assert previous.version == "2023.1"
    assert previous.status is RuleSetStatus.SUPERSEDED
    assert previous.effective_from == datetime(2023, 1, 1, tzinfo=MARKET_TZ)
    assert previous.effective_to == SWITCH_DATE


def test_2026_rule_set_cites_spec_sources(registry: RuleSetRegistry) -> None:
    rule_set = next(r for r in registry.rule_sets() if r.rule_set_id == "CN-SSE-SZSE-2026.1")

    assert rule_set.published_at == datetime(2026, 4, 24, tzinfo=MARKET_TZ)
    assert SSE_RULES_URL in rule_set.source_refs
    assert SZSE_RULES_URL in rule_set.source_refs
    assert len(rule_set.source_refs) >= 5
    assert rule_set.notes
    assert "after-hours" in rule_set.notes.lower()
    assert "risk-warning" in rule_set.notes.lower()


def test_required_profile_grid_is_registered(registry: RuleSetRegistry) -> None:
    rule_set = next(r for r in registry.rule_sets() if r.rule_set_id == "CN-SSE-SZSE-2026.1")
    profiles = {profile.profile_id: profile for profile in rule_set.profiles}

    expected = {
        "SSE_MAIN-A_SHARE",
        "SZSE_MAIN-A_SHARE",
        "SSE_MAIN-A_SHARE-RISK-WARNING",
        "SSE_MAIN-EQUITY-ETF",
        "SZSE_MAIN-EQUITY-ETF",
        "SSE_MAIN-EQUITY-ETF-T0-FIXTURE",
        "SSE_MAIN-A_SHARE-NEW-LISTING",
    }
    assert expected <= set(profiles)

    stock = profiles["SSE_MAIN-A_SHARE"]
    assert stock.tick_size == Decimal("0.01")
    assert stock.price_limit_pct == Decimal("0.10")
    assert stock.settlement_rule is SettlementRule.T_PLUS_1
    assert stock.buy_quantity_increment == 100
    assert stock.min_buy_quantity == 100
    assert stock.fee_schedule_id == "CN-EQUITY-DEFAULT"

    for etf_id in ("SSE_MAIN-EQUITY-ETF", "SZSE_MAIN-EQUITY-ETF"):
        assert profiles[etf_id].tick_size == Decimal("0.001")
        assert profiles[etf_id].settlement_rule is SettlementRule.T_PLUS_1

    risk_warning = profiles["SSE_MAIN-A_SHARE-RISK-WARNING"]
    assert risk_warning.risk_warning_only is True
    assert risk_warning.price_limit_pct == Decimal("0.05")

    fixture = profiles["SSE_MAIN-EQUITY-ETF-T0-FIXTURE"]
    assert fixture.symbols == ("510999.SH",)
    assert fixture.settlement_rule is SettlementRule.T_PLUS_0
    assert "TEST-ONLY" in fixture.description

    new_listing = profiles["SSE_MAIN-A_SHARE-NEW-LISTING"]
    assert new_listing.no_price_limit_reason == "NEW_LISTING_NO_LIMIT_STAGE"
    assert new_listing.price_limit_pct is None


def test_fee_schedules_are_estimated_and_componentised(registry: RuleSetRegistry) -> None:
    rule_set = next(r for r in registry.rule_sets() if r.rule_set_id == "CN-SSE-SZSE-2026.1")
    schedules = {schedule.fee_schedule_id: schedule for schedule in rule_set.fee_schedules}

    assert set(schedules) == {"CN-EQUITY-DEFAULT", "CN-ETF-DEFAULT"}

    equity = schedules["CN-EQUITY-DEFAULT"]
    assert equity.status.value == "ESTIMATED"
    assert equity.notes
    components = {line.component for line in equity.lines}
    assert components == {
        FeeComponent.BROKER_COMMISSION,
        FeeComponent.STAMP_DUTY,
        FeeComponent.TRANSFER_FEE,
        FeeComponent.EXCHANGE_HANDLING_FEE,
    }
    assert FeeComponent.SLIPPAGE not in components

    commission = next(
        line for line in equity.lines if line.component is FeeComponent.BROKER_COMMISSION
    )
    assert commission.rate == Decimal("0.00025")
    assert commission.min_amount == Decimal("5.00")
    assert commission.source

    stamp = next(line for line in equity.lines if line.component is FeeComponent.STAMP_DUTY)
    assert stamp.rate == Decimal("0.0005")
    assert stamp.applicable_sides == (OrderSide.SELL,)

    etf = schedules["CN-ETF-DEFAULT"]
    assert FeeComponent.STAMP_DUTY not in {line.component for line in etf.lines}


# ---------------------------------------------------------------------------
# Resolution contract
# ---------------------------------------------------------------------------


def test_resolve_contract_for_stock_and_etf(registry: RuleSetRegistry) -> None:
    engine = registry.engine()

    stock = engine.resolve(
        _instrument("600519.SH", Exchange.SSE, Board.SSE_MAIN, InstrumentType.A_SHARE),
        TRADE_DATE,
    )
    szse_stock = engine.resolve(
        _instrument("000001.SZ", Exchange.SZSE, Board.SZSE_MAIN, InstrumentType.A_SHARE),
        TRADE_DATE,
    )
    etf = engine.resolve(
        _instrument("510300.SH", Exchange.SSE, Board.SSE_MAIN, InstrumentType.DOMESTIC_EQUITY_ETF),
        TRADE_DATE,
    )
    szse_etf = engine.resolve(
        _instrument(
            "159915.SZ", Exchange.SZSE, Board.SZSE_MAIN, InstrumentType.DOMESTIC_EQUITY_ETF
        ),
        TRADE_DATE,
    )

    assert stock.tick_size == Decimal("0.01")
    assert stock.settlement_rule is SettlementRule.T_PLUS_1
    assert stock.rule_set_version == "2026.1"
    assert stock.fee_schedule_version == "phase0-estimated-1"
    assert szse_stock.profile_id == "SZSE_MAIN-A_SHARE"
    assert etf.tick_size == Decimal("0.001")
    assert etf.fee_schedule_id == "CN-ETF-DEFAULT"
    assert szse_etf.tick_size == Decimal("0.001")

    limits = stock.compute_price_limits(Decimal("1000"), is_risk_warning=False)
    assert limits.limit_up == Decimal("1100.00")
    assert limits.limit_down == Decimal("900.00")


def test_resolve_risk_warning_contract(registry: RuleSetRegistry) -> None:
    engine = registry.engine()
    st_stock = _instrument(
        "600519.SH",
        Exchange.SSE,
        Board.SSE_MAIN,
        InstrumentType.A_SHARE,
        status=SecurityStatus.ST,
    )

    rules = engine.resolve(st_stock, TRADE_DATE)
    limits = rules.compute_price_limits(Decimal("10.00"), is_risk_warning=True)

    assert rules.profile_id == "SSE_MAIN-A_SHARE-RISK-WARNING"
    assert limits.pct == Decimal("0.05")
    assert limits.limit_up == Decimal("10.50")
    assert limits.limit_down == Decimal("9.50")


def test_resolve_t0_fixture_contract(registry: RuleSetRegistry) -> None:
    engine = registry.engine()
    t0_etf = _instrument(
        "510999.SH", Exchange.SSE, Board.SSE_MAIN, InstrumentType.DOMESTIC_EQUITY_ETF
    )
    t1_etf = _instrument(
        "510300.SH", Exchange.SSE, Board.SSE_MAIN, InstrumentType.DOMESTIC_EQUITY_ETF
    )

    t0_rules = engine.resolve(t0_etf, TRADE_DATE)
    t1_rules = engine.resolve(t1_etf, TRADE_DATE)

    assert t0_rules.profile_id == "SSE_MAIN-EQUITY-ETF-T0-FIXTURE"
    assert t0_rules.settlement_rule is SettlementRule.T_PLUS_0
    assert t1_rules.settlement_rule is SettlementRule.T_PLUS_1


def test_resolve_new_listing_no_limit_contract(registry: RuleSetRegistry) -> None:
    engine = registry.engine()
    ipo = _instrument("600519.SH", Exchange.SSE, Board.SSE_MAIN, InstrumentType.A_SHARE)

    normal = engine.resolve(ipo, TRADE_DATE)
    listing = engine.resolve(ipo, TRADE_DATE, new_listing_stage=True)

    assert normal.price_limit_pct == Decimal("0.10")
    assert listing.price_limit_pct is None
    assert listing.no_price_limit_reason == "NEW_LISTING_NO_LIMIT_STAGE"
    assert listing.compute_price_limits(Decimal("10.00"), is_risk_warning=False).has_limits is False


def test_effective_date_switch_contract(registry: RuleSetRegistry) -> None:
    engine = registry.engine()

    assert engine.rule_set_for(datetime(2026, 7, 3, 10, 0, tzinfo=MARKET_TZ)).version == "2023.1"
    assert engine.rule_set_for(datetime(2026, 7, 6, 9, 30, tzinfo=MARKET_TZ)).version == "2026.1"
    assert (
        engine.resolve(
            _instrument("600519.SH", Exchange.SSE, Board.SSE_MAIN, InstrumentType.A_SHARE),
            date(2026, 7, 3),
        ).rule_set_version
        == "2023.1"
    )


def test_unsupported_symbol_fails_closed(registry: RuleSetRegistry) -> None:
    engine = registry.engine()
    chi_next = _instrument("300750.SZ", Exchange.SZSE, Board.SZSE_CHINEXT, InstrumentType.A_SHARE)

    with pytest.raises(UnsupportedEntityError):
        engine.resolve(chi_next, TRADE_DATE)


def test_compute_fees_contract(registry: RuleSetRegistry) -> None:
    engine = registry.engine()
    stock = engine.resolve(
        _instrument("600519.SH", Exchange.SSE, Board.SSE_MAIN, InstrumentType.A_SHARE),
        TRADE_DATE,
    )
    etf = engine.resolve(
        _instrument("510300.SH", Exchange.SSE, Board.SSE_MAIN, InstrumentType.DOMESTIC_EQUITY_ETF),
        TRADE_DATE,
    )

    buy = engine.compute_fees(
        rules=stock, side=OrderSide.BUY, quantity=1000, price=Decimal("10.00")
    )
    sell = engine.compute_fees(
        rules=stock, side=OrderSide.SELL, quantity=1000, price=Decimal("10.00")
    )
    etf_buy = engine.compute_fees(
        rules=etf, side=OrderSide.BUY, quantity=1000, price=Decimal("3.500")
    )

    assert buy.total == Decimal("5.44")
    assert sell.total == Decimal("10.44")
    assert etf_buy.total == Decimal("5.12")
    assert buy.fee_schedule_version == "phase0-estimated-1"


# ---------------------------------------------------------------------------
# Calendar contract
# ---------------------------------------------------------------------------


def test_calendar_contract(registry: RuleSetRegistry) -> None:
    calendar = registry.calendar()

    assert calendar.name == "CN-2026-H2"
    assert calendar.min_date == date(2026, 7, 6)
    assert calendar.max_date == date(2026, 12, 31)
    assert calendar.is_trading_day(TRADE_DATE) is True
    assert calendar.is_trading_day(date(2026, 9, 5)) is False
    assert calendar.is_trading_day(date(2026, 10, 1)) is False
    assert calendar.is_trading_day(date(2026, 9, 25)) is False
    assert calendar.next_trading_day(date(2026, 9, 30)) == date(2026, 10, 8)
    # Coverage begins at the ACTIVE rule-set effective date and contains at least
    # the 60 trading days the seasoned-listing eligibility window needs.
    assert calendar.count_trading_days(calendar.min_date, TRADE_DATE) >= 60

    sessions = calendar.sessions_on(TRADE_DATE)
    assert len(sessions) == 4
    assert calendar.session_at(datetime(2026, 11, 5, 10, 0, tzinfo=MARKET_TZ)).session_type is (
        SessionType.CONTINUOUS
    )
    assert calendar.session_at(datetime(2026, 11, 5, 12, 0, tzinfo=MARKET_TZ)) is None
    closing = calendar.session_at(datetime(2026, 11, 5, 14, 58, tzinfo=MARKET_TZ))
    assert closing is not None
    assert closing.session_type is SessionType.CLOSING_AUCTION
    # After-hours fixed-price trading is deliberately not modelled.
    assert calendar.session_at(datetime(2026, 11, 5, 15, 10, tzinfo=MARKET_TZ)) is None


def test_json_calendar_fixture_matches_yaml(registry: RuleSetRegistry) -> None:
    fixture = REPO_ROOT / "data" / "fixtures" / "calendar" / "cn_calendar_2026.json"
    fixture_calendar = load_calendar(fixture)
    config_calendar = registry.calendar()

    assert fixture_calendar.min_date == config_calendar.min_date
    assert fixture_calendar.max_date == config_calendar.max_date
    assert fixture_calendar.trading_days() == config_calendar.trading_days()
    for day in fixture_calendar.trading_days():
        assert fixture_calendar.day(day).day_type is config_calendar.day(day).day_type


# ---------------------------------------------------------------------------
# Strict loader / registry fail-closed behaviour
# ---------------------------------------------------------------------------

_MINIMAL_FEE_DOCUMENT = {
    "fee_schedules": [
        {
            "fee_schedule_id": "CN-EQUITY-DEFAULT",
            "version": "test",
            "status": "ESTIMATED",
            "effective_from": "2023-01-01T00:00:00+08:00",
            "lines": [
                {
                    "component": "BROKER_COMMISSION",
                    "basis": "NOTIONAL",
                    "rate": "0.00025",
                    "min_amount": "5.00",
                }
            ],
        }
    ]
}

_MINIMAL_RULE_SET_DOCUMENT = {
    "rule_set": {
        "rule_set_id": "CN-TEST-1",
        "version": "1",
        "status": "ACTIVE",
        "effective_from": "2026-07-06T00:00:00+08:00",
        "default_sessions": [
            {
                "session_type": "CONTINUOUS",
                "start_time": "09:30:00",
                "end_time": "11:30:00",
            }
        ],
        "profiles": [
            {
                "profile_id": "MEDIUM-LIQUID-A-SHARE",
                "exchange": "SSE",
                "board": "SSE_MAIN",
                "instrument_type": "A_SHARE",
                "settlement_rule": "T_PLUS_1",
                "tick_size": "0.01",
                "price_limit_pct": "0.10",
            }
        ],
        "fee_schedule_ids": ["CN-EQUITY-DEFAULT"],
    }
}


def _write_yaml(directory: Path, name: str, document: dict[str, object]) -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _minimal_config_dir(tmp_path: Path) -> Path:
    _write_yaml(tmp_path, "fees_test.yaml", _MINIMAL_FEE_DOCUMENT)
    _write_yaml(tmp_path, "ruleset_test.yaml", _MINIMAL_RULE_SET_DOCUMENT)
    return tmp_path


def test_loader_accepts_minimal_configuration(tmp_path: Path) -> None:
    config_dir = _minimal_config_dir(tmp_path)
    fee_schedules = load_fee_schedules(config_dir)
    rule_sets = load_rule_sets(config_dir)

    assert len(fee_schedules) == 1
    assert len(rule_sets) == 1
    assert rule_sets[0].rule_set_id == "CN-TEST-1"
    assert rule_sets[0].fee_schedules[0].fee_schedule_id == "CN-EQUITY-DEFAULT"


def test_loader_rejects_unknown_rule_set_key(tmp_path: Path) -> None:
    document = copy.deepcopy(_MINIMAL_RULE_SET_DOCUMENT)
    document["rule_set"]["unexpected_key"] = True
    _write_yaml(tmp_path, "fees_test.yaml", _MINIMAL_FEE_DOCUMENT)
    _write_yaml(tmp_path, "ruleset_test.yaml", document)

    with pytest.raises(ConfigurationError) as excinfo:
        load_rule_sets(tmp_path)

    assert "unexpected_key" in str(excinfo.value)
    assert excinfo.value.details["field"] == "rule_set.unexpected_key"


def test_loader_rejects_missing_required_field(tmp_path: Path) -> None:
    document = copy.deepcopy(_MINIMAL_RULE_SET_DOCUMENT)
    del document["rule_set"]["profiles"]
    _write_yaml(tmp_path, "fees_test.yaml", _MINIMAL_FEE_DOCUMENT)
    _write_yaml(tmp_path, "ruleset_test.yaml", document)

    with pytest.raises(ConfigurationError) as excinfo:
        load_rule_sets(tmp_path)

    assert "profiles" in str(excinfo.value).lower()


def test_loader_rejects_unknown_fee_schedule_reference(tmp_path: Path) -> None:
    document = copy.deepcopy(_MINIMAL_RULE_SET_DOCUMENT)
    document["rule_set"]["fee_schedule_ids"] = ["CN-MISSING"]
    _write_yaml(tmp_path, "fees_test.yaml", _MINIMAL_FEE_DOCUMENT)
    _write_yaml(tmp_path, "ruleset_test.yaml", document)

    with pytest.raises(ConfigurationError) as excinfo:
        load_rule_sets(tmp_path)

    assert excinfo.value.details["fee_schedule_id"] == "CN-MISSING"


def test_loader_rejects_missing_fee_schedule_ids(tmp_path: Path) -> None:
    document = copy.deepcopy(_MINIMAL_RULE_SET_DOCUMENT)
    del document["rule_set"]["fee_schedule_ids"]
    _write_yaml(tmp_path, "fees_test.yaml", _MINIMAL_FEE_DOCUMENT)
    _write_yaml(tmp_path, "ruleset_test.yaml", document)

    with pytest.raises(ConfigurationError) as excinfo:
        load_rule_sets(tmp_path)

    assert excinfo.value.details["field"] == "rule_set.fee_schedule_ids"


def test_loader_requires_fee_schedules_for_single_file(tmp_path: Path) -> None:
    document = _MINIMAL_RULE_SET_DOCUMENT
    path = _write_yaml(tmp_path, "ruleset_single.yaml", document)

    with pytest.raises(ConfigurationError, match="fee_schedules"):
        load_rule_sets(path)


_CALENDAR_SESSIONS = [
    {
        "session_type": "CONTINUOUS",
        "start_time": "09:30:00",
        "end_time": "11:30:00",
    }
]


def _calendar_document(days: list[dict[str, str]]) -> dict[str, object]:
    return {
        "calendar": {
            "calendar_id": "CN-TEST-CAL",
            "version": "test",
            "coverage_from": days[0]["date"],
            "coverage_to": days[-1]["date"],
            "default_sessions": _CALENDAR_SESSIONS,
            "days": days,
        }
    }


def _calendar_days(day_types: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"date": day, "day_type": day_type, "source": "TEST"} for day, day_type in day_types.items()
    ]


def test_calendar_loader_rejects_coverage_gap(tmp_path: Path) -> None:
    days = _calendar_days(
        {
            "2026-11-02": "TRADING",
            "2026-11-03": "TRADING",
            # 2026-11-04 deliberately missing
            "2026-11-05": "TRADING",
        }
    )
    path = _write_yaml(tmp_path, "calendar_test.yaml", _calendar_document(days))

    with pytest.raises(ConfigurationError) as excinfo:
        load_calendar(path)

    assert excinfo.value.details["missing_days"] == ["2026-11-04"]


def test_calendar_loader_rejects_weekend_trading(tmp_path: Path) -> None:
    days = _calendar_days(
        {
            "2026-11-06": "TRADING",
            "2026-11-07": "TRADING",  # Saturday
            "2026-11-08": "WEEKEND",
        }
    )
    path = _write_yaml(tmp_path, "calendar_test.yaml", _calendar_document(days))

    with pytest.raises(ConfigurationError, match="WEEKEND"):
        load_calendar(path)


def test_calendar_loader_rejects_unknown_key(tmp_path: Path) -> None:
    days = _calendar_days({"2026-11-02": "TRADING"})
    document = _calendar_document(days)
    document["calendar"]["unexpected"] = 1
    path = _write_yaml(tmp_path, "calendar_test.yaml", document)

    with pytest.raises(ConfigurationError) as excinfo:
        load_calendar(path)

    assert "unexpected" in str(excinfo.value)


def test_calendar_loader_rejects_incomplete_day_entry(tmp_path: Path) -> None:
    days = [{"date": "2026-11-02", "day_type": "TRADING"}]
    path = _write_yaml(tmp_path, "calendar_test.yaml", _calendar_document(days))

    # 'source' is optional, so this valid single-day calendar loads; the invalid
    # case is a day without day_type.
    calendar = load_calendar(path)
    assert calendar.trading_days() == (date(2026, 11, 2),)

    days = [{"date": "2026-11-02", "source": "TEST"}]
    path = _write_yaml(tmp_path, "calendar_test2.yaml", _calendar_document(days))
    with pytest.raises(ConfigurationError, match="day_type"):
        load_calendar(path)


def test_registry_calendar_fails_closed_when_not_configured(
    registry: RuleSetRegistry,
) -> None:
    bare = RuleSetRegistry(registry.rule_sets())

    with pytest.raises(ConfigurationError, match="without a trading calendar"):
        bare.calendar()


def test_registry_rejects_overlapping_configured_rule_sets(
    registry: RuleSetRegistry,
) -> None:
    rule_sets = registry.rule_sets()
    overlapping = (rule_sets[0], rule_sets[1], rule_sets[0])

    with pytest.raises(ConfigurationError):
        RuleSetRegistry(overlapping)


def test_calendar_session_windows_are_explicit(registry: RuleSetRegistry) -> None:
    sessions = registry.calendar().sessions_on(TRADE_DATE)
    windows = [(session.session_type, session.start_time, session.end_time) for session in sessions]

    assert windows == [
        (SessionType.PRE_OPEN_AUCTION, time(9, 15), time(9, 25)),
        (SessionType.CONTINUOUS, time(9, 30), time(11, 30)),
        (SessionType.CONTINUOUS, time(13, 0), time(14, 57)),
        (SessionType.CLOSING_AUCTION, time(14, 57), time(15, 0)),
    ]
    assert all(
        session.allows_execution()
        for session in sessions
        if session.session_type is not SessionType.PRE_OPEN_AUCTION
    )
