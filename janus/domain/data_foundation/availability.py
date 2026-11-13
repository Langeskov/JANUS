"""Availability metadata and the conservative availability policy.

The whole point of the data foundation is that four different times are never
conflated:

``published_at``
    What the *source* claims about when the information became public.
``public_available_at``
    JANUS's estimate or verification of when the information was publicly
    accessible, at the precision the source actually provides.
``system_first_observed_at``
    When JANUS itself first saw the information. Always a real system timestamp;
    a historical bulk import therefore has a *modern* observation time.
``strategy_usable_at``
    The earliest instant the selected knowledge policy permits a strategy query
    to see the fact.

A date-only publication timestamp is **not** an exact timestamp. This module
never manufactures sub-day precision: it either derives a provably conservative
lower bound (next calendar day, midnight Asia/Shanghai) or marks availability
unknown, which removes the fact from strategy-visible queries entirely.

No trading calendar is consulted here. Phase 0's calendar covers only
2026-07-06..2026-12-31; using it to infer historical session boundaries would be
fabrication, so the policy deliberately relies on nothing but calendar-day
boundaries.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from janus.domain.common.errors import ValidationError
from janus.domain.common.time import MARKET_TZ, ensure_aware

__all__ = [
    "AvailabilityBasis",
    "AvailabilityPolicy",
    "AvailabilityPrecision",
    "DateOnlyAvailabilityMode",
    "derive_strategy_usable_at",
    "market_day_start",
    "next_market_day_start",
]

_MARKET_MIDNIGHT = time(0, 0)


class AvailabilityBasis(StrEnum):
    """How the availability time was established (evidence classification)."""

    SOURCE_TIMESTAMP = "SOURCE_TIMESTAMP"
    """The source supplied an exact timestamp for the public release."""

    SOURCE_PUBLICATION_DATE = "SOURCE_PUBLICATION_DATE"
    """The source supplied only a calendar date."""

    OFFICIAL_RELEASE_SCHEDULE = "OFFICIAL_RELEASE_SCHEDULE"
    """An official, effective-dated release schedule fixes the time."""

    PROVIDER_TIMESTAMP = "PROVIDER_TIMESTAMP"
    """The vendor's own timestamp; trusted only as far as the vendor contract."""

    CONSERVATIVE_POLICY = "CONSERVATIVE_POLICY"
    """JANUS derived a deliberately late lower bound from a documented policy."""

    UNKNOWN = "UNKNOWN"
    """Nothing establishes when the information became public."""

    @property
    def is_verified(self) -> bool:
        """True only for bases that represent positive evidence about the release."""
        return self in {
            AvailabilityBasis.SOURCE_TIMESTAMP,
            AvailabilityBasis.SOURCE_PUBLICATION_DATE,
            AvailabilityBasis.OFFICIAL_RELEASE_SCHEDULE,
            AvailabilityBasis.PROVIDER_TIMESTAMP,
        }


class AvailabilityPrecision(StrEnum):
    """Resolution of the availability information that was actually supplied."""

    INSTANT = "INSTANT"
    MINUTE = "MINUTE"
    DATE = "DATE"
    UNKNOWN = "UNKNOWN"

    @property
    def is_exact(self) -> bool:
        return self in {AvailabilityPrecision.INSTANT, AvailabilityPrecision.MINUTE}


class DateOnlyAvailabilityMode(StrEnum):
    """How a date-only publication timestamp is turned into strategy availability."""

    STRICT_UNKNOWN = "STRICT_UNKNOWN"
    """Do not infer anything: the fact is not strategy-usable (unknown availability)."""

    CONSERVATIVE_NEXT_CALENDAR_DAY = "CONSERVATIVE_NEXT_CALENDAR_DAY"
    """Usable from the next calendar day's 00:00 Asia/Shanghai.

    Sound because a fact published on date ``D`` cannot be shown to have been
    public before ``D`` ends; ``D + 1`` 00:00 is a *lower bound* that needs no
    trading calendar. It may delay availability by up to a day (conservative).
    """


def market_day_start(value: datetime) -> datetime:
    """Midnight Asia/Shanghai of the market date containing ``value``."""
    local = ensure_aware(value, "value").astimezone(MARKET_TZ)
    return datetime.combine(local.date(), _MARKET_MIDNIGHT, tzinfo=MARKET_TZ)


def next_market_day_start(value: datetime) -> datetime:
    """Midnight Asia/Shanghai of the next calendar day after ``value``."""
    return market_day_start(value) + timedelta(days=1)


class AvailabilityPolicy(BaseModel):
    """Versioned knowledge policy applied to every ingested fact.

    ``policy_version`` is copied onto every provenance record, so an audit can
    always reconstruct *which rules* produced a given ``strategy_usable_at``
    even after the policy changes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = "p1a-availability-v1"
    date_only_mode: DateOnlyAvailabilityMode = (
        DateOnlyAvailabilityMode.CONSERVATIVE_NEXT_CALENDAR_DAY
    )

    @field_validator("policy_version")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValidationError("availability policy_version must not be empty")
        return value


def derive_strategy_usable_at(
    *,
    public_available_at: datetime | None,
    published_at: datetime | None,
    precision: AvailabilityPrecision,
    basis: AvailabilityBasis,
    policy: AvailabilityPolicy,
) -> tuple[datetime | None, AvailabilityBasis]:
    """Derive ``(strategy_usable_at, derivation_basis)`` for one fact.

    Fail-closed rules:

    * unknown precision or unknown basis -> availability unknown (``None``);
    * exact precision (instant/minute) -> usable at ``public_available_at``;
    * date-only precision -> governed by ``policy.date_only_mode``:
      ``STRICT_UNKNOWN`` yields unknown availability, while
      ``CONSERVATIVE_NEXT_CALENDAR_DAY`` yields the next calendar day's midnight;
    * a missing ``public_available_at`` yields unknown availability;
    * a date-only timestamp that arrives as an exact datetime is *downgraded* to
      the day boundary (the extra sub-day digits are never trusted).
    """
    if precision is AvailabilityPrecision.UNKNOWN or basis is AvailabilityBasis.UNKNOWN:
        return None, AvailabilityBasis.UNKNOWN
    if public_available_at is None:
        return None, AvailabilityBasis.UNKNOWN

    public_available_at = ensure_aware(public_available_at, "public_available_at")

    if precision.is_exact:
        if published_at is not None and public_available_at < ensure_aware(
            published_at, "published_at"
        ):
            raise ValidationError(
                "public_available_at must not precede published_at",
                public_available_at=public_available_at.isoformat(),
                published_at=ensure_aware(published_at, "published_at").isoformat(),
            )
        return public_available_at, basis

    if precision is AvailabilityPrecision.DATE:
        day_start = market_day_start(public_available_at)
        if published_at is not None and day_start < market_day_start(
            ensure_aware(published_at, "published_at")
        ):
            raise ValidationError(
                "date-only public availability must not precede the publication date",
                public_available_at=day_start.isoformat(),
                published_at=ensure_aware(published_at, "published_at").isoformat(),
            )
        if policy.date_only_mode is DateOnlyAvailabilityMode.STRICT_UNKNOWN:
            return None, AvailabilityBasis.UNKNOWN
        return next_market_day_start(day_start), AvailabilityBasis.CONSERVATIVE_POLICY

    # Defensive: a new precision member must be handled explicitly, never guessed.
    raise ValidationError(
        "unsupported availability precision; refusing to guess usability",
        precision=precision.value,
    )
