"""Unit tests for the session/calendar surface used by the rules data (P0-D)."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from janus.domain.common.enums import SessionType, TradingDayType
from janus.domain.common.errors import NotFoundError, UnsupportedEntityError
from janus.domain.common.time import MARKET_TZ
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.models import TradingDay, TradingSession

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


def _trading_day(day: date) -> TradingDay:
    return TradingDay(trade_date=day, day_type=TradingDayType.TRADING, sessions=SESSIONS)


def _calendar() -> TradingCalendar:
    """A deterministic two-week calendar: Mon-Thu trading, Fri holiday, weekend."""
    days = [
        _trading_day(date(2026, 11, 2)),
        _trading_day(date(2026, 11, 3)),
        _trading_day(date(2026, 11, 4)),
        _trading_day(date(2026, 11, 5)),
        TradingDay(trade_date=date(2026, 11, 6), day_type=TradingDayType.HOLIDAY),
        TradingDay(trade_date=date(2026, 11, 7), day_type=TradingDayType.WEEKEND),
        TradingDay(trade_date=date(2026, 11, 8), day_type=TradingDayType.WEEKEND),
        _trading_day(date(2026, 11, 9)),
    ]
    return TradingCalendar(days, name="test-week")


def test_calendar_navigates_across_holiday_and_weekend() -> None:
    calendar = _calendar()

    assert calendar.next_trading_day(date(2026, 11, 5)) == date(2026, 11, 9)
    assert calendar.previous_trading_day(date(2026, 11, 9)) == date(2026, 11, 5)
    assert calendar.trading_days_between(date(2026, 11, 2), date(2026, 11, 8)) == (
        date(2026, 11, 2),
        date(2026, 11, 3),
        date(2026, 11, 4),
        date(2026, 11, 5),
    )


def test_calendar_settlement_available_date_is_next_trading_day() -> None:
    calendar = _calendar()

    assert calendar.settlement_available_date(date(2026, 11, 5)) == date(2026, 11, 9)


def test_calendar_classifies_non_trading_day() -> None:
    calendar = _calendar()

    assert calendar.is_trading_day(date(2026, 11, 6)) is False
    assert calendar.day_type(date(2026, 11, 6)) is TradingDayType.HOLIDAY
    with pytest.raises(NotFoundError):
        calendar.ensure_trading_day(date(2026, 11, 6))


def test_calendar_session_windows_are_half_open() -> None:
    calendar = _calendar()

    assert calendar.session_at(datetime(2026, 11, 5, 9, 15, tzinfo=MARKET_TZ)).session_type is (
        SessionType.PRE_OPEN_AUCTION
    )
    assert calendar.session_at(datetime(2026, 11, 5, 11, 30, tzinfo=MARKET_TZ)) is None
    assert calendar.session_at(datetime(2026, 11, 5, 13, 0, tzinfo=MARKET_TZ)).session_type is (
        SessionType.CONTINUOUS
    )
    assert calendar.session_at(datetime(2026, 11, 5, 14, 57, tzinfo=MARKET_TZ)).session_type is (
        SessionType.CLOSING_AUCTION
    )
    assert calendar.session_at(datetime(2026, 11, 5, 15, 0, tzinfo=MARKET_TZ)) is None


def test_calendar_is_open_for_trading_only_during_executable_sessions() -> None:
    calendar = _calendar()

    assert calendar.is_open_for_trading(datetime(2026, 11, 5, 10, 0, tzinfo=MARKET_TZ)) is True
    assert calendar.is_auction(datetime(2026, 11, 5, 14, 58, tzinfo=MARKET_TZ)) is True
    # Pre-open auction is a session but not an execution window.
    assert calendar.is_open_for_trading(datetime(2026, 11, 5, 9, 20, tzinfo=MARKET_TZ)) is False


def test_calendar_fails_closed_outside_coverage() -> None:
    calendar = _calendar()

    with pytest.raises(UnsupportedEntityError):
        calendar.is_trading_day(date(2026, 12, 1))
    with pytest.raises(UnsupportedEntityError):
        calendar.next_trading_day(date(2026, 11, 9))
