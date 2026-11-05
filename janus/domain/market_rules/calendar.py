"""Trading calendar.

The calendar is *data*, not logic: it is loaded from versioned rule-set
configuration and persisted through a repository. This module only provides the
deterministic query surface.

Fail-closed rule: a date outside the calendar's declared coverage raises
:class:`UnsupportedEntityError` rather than silently assuming "not a trading
day". Phase 0 spec s2.3: "fail closed when a security/order type requires a
session it does not simulate".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from janus.domain.common.enums import SessionType, TradingDayType
from janus.domain.common.errors import NotFoundError, UnsupportedEntityError
from janus.domain.common.time import MARKET_TZ, ensure_aware
from janus.domain.market_rules.models import TradingDay, TradingSession

__all__ = ["TradingCalendar"]


class TradingCalendar:
    """Immutable, fully-materialised trading calendar for a coverage window."""

    __slots__ = ("_days", "_max_date", "_min_date", "name")

    def __init__(self, days: Sequence[TradingDay], *, name: str = "default") -> None:
        if not days:
            raise UnsupportedEntityError("a trading calendar requires at least one day")
        index: dict[date, TradingDay] = {}
        for day in days:
            if day.trade_date in index:
                raise UnsupportedEntityError(
                    "duplicate calendar entry", trade_date=day.trade_date.isoformat()
                )
            index[day.trade_date] = day
        self._days: dict[date, TradingDay] = dict(sorted(index.items()))
        self._min_date: date = min(self._days)
        self._max_date: date = max(self._days)
        self.name: str = name

    # -- coverage -------------------------------------------------------
    @property
    def min_date(self) -> date:
        return self._min_date

    @property
    def max_date(self) -> date:
        return self._max_date

    def covers(self, day: date) -> bool:
        return self._min_date <= day <= self._max_date

    def _require_coverage(self, day: date, what: str = "date") -> TradingDay:
        if not self.covers(day):
            raise UnsupportedEntityError(
                f"{what} {day.isoformat()} is outside the loaded calendar coverage "
                f"[{self._min_date.isoformat()} .. {self._max_date.isoformat()}]",
                trade_date=day.isoformat(),
            )
        return self._days[day]

    # -- basic queries --------------------------------------------------
    def day(self, day: date) -> TradingDay:
        """Calendar entry for ``day`` (raises outside coverage)."""
        return self._require_coverage(day)

    def is_trading_day(self, day: date) -> bool:
        return self._require_coverage(day).is_trading

    def ensure_trading_day(self, day: date) -> TradingDay:
        entry = self._require_coverage(day, "trade date")
        if not entry.is_trading:
            raise NotFoundError(
                f"{day.isoformat()} is a {entry.day_type.value} day, not a trading day",
                trade_date=day.isoformat(),
            )
        return entry

    def sessions_on(self, day: date) -> tuple[TradingSession, ...]:
        return self.ensure_trading_day(day).sessions

    def trading_days(self) -> tuple[date, ...]:
        return tuple(d for d, entry in self._days.items() if entry.is_trading)

    def trading_days_between(self, start: date, end: date) -> tuple[date, ...]:
        """Inclusive list of trading days in ``[start, end]``."""
        if end < start:
            return ()
        self._require_coverage(start)
        self._require_coverage(end)
        return tuple(d for d in self.trading_days() if start <= d <= end)

    def count_trading_days(self, start: date, end: date) -> int:
        """Number of trading days in ``[start, end]`` inclusive."""
        return len(self.trading_days_between(start, end))

    def listed_trading_days(self, listing_date: date, as_of: date) -> int:
        """Trading days from ``listing_date`` up to and including ``as_of``."""
        return self.count_trading_days(listing_date, as_of)

    # -- navigation -----------------------------------------------------
    def next_trading_day(self, day: date, *, inclusive: bool = False) -> date:
        """Next trading day strictly after ``day`` (or on/after with inclusive)."""
        return self._navigate(day, forward=True, inclusive=inclusive)

    def previous_trading_day(self, day: date, *, inclusive: bool = False) -> date:
        """Previous trading day strictly before ``day`` (or on/before with inclusive)."""
        return self._navigate(day, forward=False, inclusive=inclusive)

    def _navigate(self, day: date, *, forward: bool, inclusive: bool) -> date:
        # Walking one calendar day at a time keeps the behaviour obvious and the
        # coverage check honest; calendars are materialised so this is cheap.
        step = timedelta(days=1 if forward else -1)
        cursor = day if inclusive else day + step
        limit = (self._max_date - self._min_date).days + 2
        for _ in range(limit):
            if not self.covers(cursor):
                break
            if self._days[cursor].is_trading:
                return cursor
            cursor += step
        raise UnsupportedEntityError(
            "no trading day found in that direction within calendar coverage",
            from_date=day.isoformat(),
            direction="forward" if forward else "backward",
        )

    def add_trading_days(self, day: date, count: int) -> date:
        """Advance ``count`` trading days from ``day`` (count>=1)."""
        if count < 1:
            raise UnsupportedEntityError("count must be >= 1", count=count)
        cursor = day
        for _ in range(count):
            cursor = self.next_trading_day(cursor)
        return cursor

    def settlement_available_date(self, trade_date: date) -> date:
        """Earliest date on which T+1 purchases become sellable."""
        return self.next_trading_day(trade_date)

    # -- intraday -------------------------------------------------------
    def session_at(self, at: datetime) -> TradingSession | None:
        """The session open at ``at``, or None when the market is closed."""
        local = ensure_aware(at, "at").astimezone(MARKET_TZ)
        day = local.date()
        if not self.covers(day):
            return None
        entry = self._days[day]
        if not entry.is_trading:
            return None
        clock: time = local.timetz().replace(tzinfo=None)
        for session in entry.sessions:
            if session.contains(clock):
                return session
        return None

    def is_open_for_trading(self, at: datetime) -> bool:
        session = self.session_at(at)
        return session is not None and session.allows_execution()

    def is_auction(self, at: datetime) -> bool:
        session = self.session_at(at)
        return session is not None and session.session_type in {
            SessionType.PRE_OPEN_AUCTION,
            SessionType.CLOSING_AUCTION,
        }

    # -- coverage-checked helpers ---------------------------------------
    def expect_trading_day(self, day: date) -> TradingDay:
        return self.ensure_trading_day(day)

    def day_type(self, day: date) -> TradingDayType:
        return self._require_coverage(day).day_type

    def __len__(self) -> int:
        return len(self._days)

    def __repr__(self) -> str:
        return (
            f"TradingCalendar(name={self.name!r}, days={len(self._days)}, "
            f"coverage={self._min_date}..{self._max_date})"
        )
