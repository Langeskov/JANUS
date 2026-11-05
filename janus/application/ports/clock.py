"""Clock and LLM/adapter ports.

A clock port (rather than ``datetime.now()`` inside services) makes paper-trading
simulations deterministic and replayable.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from janus.domain.common.time import MARKET_TZ, market_date, utc_now

__all__ = ["Clock", "FixedClock", "LLMProvider", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """Injected time source. Always returns timezone-aware UTC datetimes."""

    def now(self) -> datetime: ...

    def today(self) -> date: ...


class SystemClock:
    """Real wall clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return utc_now()

    def today(self) -> date:
        return market_date(utc_now())


class FixedClock:
    """Deterministic clock for tests and fixture-driven simulations."""

    __slots__ = ("_frozen", "_today")

    def __init__(self, frozen: datetime) -> None:
        if frozen.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._frozen = frozen
        self._today = frozen.astimezone(MARKET_TZ).date()

    def now(self) -> datetime:
        return self._frozen

    def today(self) -> date:
        return self._today

    def advance(self, **kwargs: int) -> None:
        """Advance the frozen instant (minutes/hours/days)."""
        from datetime import timedelta

        self._frozen = self._frozen + timedelta(**kwargs)
        self._today = self._frozen.astimezone(MARKET_TZ).date()


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal LLM port. Phase 0 disables live LLMs by configuration.

    Implementations must never receive credentials through a prompt and must
    return raw structured text that the agent layer validates against a strict
    schema (spec s9.3).
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    def complete(self, *, system_prompt: str, user_content: str) -> str:
        """Return model output text. Callers must validate before use."""
        ...
