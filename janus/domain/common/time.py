"""Time handling and point-in-time bookkeeping.

Phase 0 spec s7.3 requires every fact to distinguish ``event_time``,
``published_at``, ``retrieved_at``, ``known_at`` and an ``effective_from`` /
``effective_to`` interval. :class:`PointInTime` is the shared carrier.

All datetimes inside JANUS are timezone-aware. The market timezone is
``Asia/Shanghai``; storage and comparison happen in UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from janus.domain.common.errors import ValidationError

MARKET_TZ: ZoneInfo = ZoneInfo("Asia/Shanghai")
"""Primary market timezone (mainland China, no DST)."""

UTC_TZ = UTC

__all__ = [
    "MARKET_TZ",
    "UTC_TZ",
    "PointInTime",
    "ensure_aware",
    "is_trading_day_of_week",
    "market_date",
    "market_now",
    "parse_iso",
    "to_market_tz",
    "to_utc",
    "utc_now",
]


def utc_now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(tz=UTC)


def market_now() -> datetime:
    """Current time as an aware Asia/Shanghai datetime."""
    return utc_now().astimezone(MARKET_TZ)


def ensure_aware(value: datetime, field: str) -> datetime:
    """Raise :class:`ValidationError` unless ``value`` is timezone-aware."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationError(f"{field} must be timezone-aware", field=field)
    return value


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC."""
    return ensure_aware(value, "value").astimezone(UTC)


def to_market_tz(value: datetime) -> datetime:
    """Convert an aware datetime to the Asia/Shanghai market timezone."""
    return ensure_aware(value, "value").astimezone(MARKET_TZ)


def market_date(value: datetime) -> date:
    """The market calendar date of an aware datetime (Shanghai wall clock)."""
    return to_market_tz(value).date()


def parse_iso(value: str, field: str = "value") -> datetime:
    """Parse an ISO-8601 string that must carry an explicit UTC offset."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValidationError(f"{field} is not ISO-8601: {value!r}", field=field) from exc
    return ensure_aware(parsed, field)


def is_trading_day_of_week(day: date) -> bool:
    """Weekday-only check. The authoritative calendar lives in MarketRules."""
    return day.weekday() < 5


class PointInTime(BaseModel):
    """Provenance timestamps for one fact.

    ``known_at`` is the earliest instant JANUS is permitted to *use* the fact.
    For point-in-time-correct research this must be derived from publication
    time, not from when the data was fetched.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_time: datetime
    """When the underlying market/company event occurred."""

    published_at: datetime | None = None
    """When the fact became public, if known."""

    retrieved_at: datetime
    """When JANUS fetched the fact."""

    known_at: datetime
    """Earliest instant JANUS may use the fact for a decision."""

    effective_from: datetime | None = None
    """Start of the interval over which a rule/state applies."""

    effective_to: datetime | None = None
    """End (exclusive) of the applicable interval."""

    @field_validator("event_time", "published_at", "retrieved_at", "known_at")
    @classmethod
    def _aware(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return ensure_aware(value, "timestamp")

    @field_validator("effective_from", "effective_to")
    @classmethod
    def _aware_optional(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_aware(value, "effective")

    @model_validator(mode="after")
    def _check_ordering(self) -> PointInTime:
        # ``known_at`` is the point-in-time boundary: the earliest instant a fact
        # may influence a decision. It is anchored to *publication*, not to
        # retrieval, so that historical backfill remains possible (a fact can be
        # retrieved later, for a backtest, while still only being knowable from
        # its publication date).
        if self.published_at is not None and self.known_at < self.published_at:
            raise ValidationError(
                "known_at must not precede published_at "
                "(a fact cannot be used before it became public)",
                known_at=self.known_at.isoformat(),
                published_at=self.published_at.isoformat(),
            )
        if self.published_at is not None and self.published_at < self.event_time:
            raise ValidationError("published_at must not precede event_time", field="published_at")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to <= self.effective_from
        ):
            raise ValidationError("effective_to must be strictly after effective_from")
        return self

    def is_known_at(self, at: datetime) -> bool:
        """True when the fact was usable at ``at``."""
        return ensure_aware(at, "at") >= self.known_at

    def is_effective_at(self, at: datetime) -> bool:
        """True when the applicable interval covers ``at``."""
        at = ensure_aware(at, "at")
        if self.effective_from is not None and at < self.effective_from:
            return False
        return not (self.effective_to is not None and at >= self.effective_to)

    def age(self, at: datetime) -> timedelta:
        """Age of the fact relative to ``at`` (based on ``known_at``)."""
        return ensure_aware(at, "at") - self.known_at
