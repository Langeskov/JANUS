"""Default market-rules registry.

Composition root for the market-rules slice: loads ``config/market_rules/``
(rule-sets, fee schedules, trading calendar) and exposes a validated
:class:`~janus.domain.market_rules.engine.MarketRulesEngine`.

The registry is the only place that knows where the configuration lives; the
domain stays pure and the application layer receives the engine/calendar objects
through the composition root.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from janus.domain.common.errors import ConfigurationError
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.engine import MarketRulesEngine
from janus.domain.market_rules.models import TradingRuleSet
from janus.infrastructure.rulesets.loader import (
    CALENDAR_FILE_GLOB,
    load_calendar,
    load_fee_schedules,
    load_rule_sets,
)

CONFIG_DIR_ENV_VAR = "JANUS_MARKET_RULES_DIR"
"""Optional environment override for the market-rules configuration directory."""

__all__ = [
    "CONFIG_DIR_ENV_VAR",
    "RuleSetRegistry",
    "default_config_dir",
]


def default_config_dir() -> Path:
    """Resolve the repository's ``config/market_rules/`` directory.

    Resolution order: ``JANUS_MARKET_RULES_DIR`` environment variable, then the
    checked-in configuration next to the source tree (repo root layout).
    """
    override = os.environ.get(CONFIG_DIR_ENV_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config" / "market_rules"


class RuleSetRegistry:
    """Immutable registry of versioned rule-sets plus the trading calendar."""

    __slots__ = ("_calendar", "_config_dir", "_engine", "_rule_sets")

    def __init__(
        self,
        rule_sets: Sequence[TradingRuleSet],
        *,
        calendar: TradingCalendar | None = None,
        config_dir: Path | None = None,
    ) -> None:
        """Register rule-sets and optionally the calendar they are used with.

        The engine is constructed eagerly so that configuration contradictions
        (duplicate ids, overlapping effective windows, no ACTIVE rule-set) surface
        at load time instead of on the first order.
        """
        self._rule_sets: tuple[TradingRuleSet, ...] = tuple(rule_sets)
        self._engine = MarketRulesEngine(self._rule_sets)
        self._calendar: TradingCalendar | None = calendar
        self._config_dir: Path | None = config_dir

    # -- constructors ---------------------------------------------------
    @classmethod
    def from_default_config(cls, *, config_dir: Path | str | None = None) -> RuleSetRegistry:
        """Load the default ``config/market_rules/`` bundle.

        Raises :class:`ConfigurationError` when the directory, the fee schedules,
        exactly one calendar file, or a valid rule-set set is missing.
        """
        root = Path(config_dir) if config_dir is not None else default_config_dir()
        if not root.is_dir():
            raise ConfigurationError(
                "market-rules configuration directory not found", directory=str(root)
            )
        fee_schedules = load_fee_schedules(root)
        rule_sets = load_rule_sets(root, fee_schedules=fee_schedules)
        calendar_files = tuple(sorted(root.glob(CALENDAR_FILE_GLOB)))
        if len(calendar_files) != 1:
            raise ConfigurationError(
                "exactly one calendar file is required",
                directory=str(root),
                files=[file.name for file in calendar_files],
            )
        calendar = load_calendar(calendar_files[0])
        return cls(rule_sets, calendar=calendar, config_dir=root)

    # -- accessors ------------------------------------------------------
    def rule_sets(self) -> tuple[TradingRuleSet, ...]:
        """Every registered rule-set (including ``SUPERSEDED`` replay baselines)."""
        return self._rule_sets

    def engine(self) -> MarketRulesEngine:
        """The validated rules engine built from the registered rule-sets."""
        return self._engine

    def calendar(self) -> TradingCalendar:
        """The configured trading calendar.

        Fail closed: a registry that was built without a calendar refuses to
        pretend one exists.
        """
        if self._calendar is None:
            raise ConfigurationError(
                "this registry was built without a trading calendar",
                rule_set_ids=[rule_set.rule_set_id for rule_set in self._rule_sets],
            )
        return self._calendar

    @property
    def config_dir(self) -> Path | None:
        """Configuration directory this registry was loaded from, if known."""
        return self._config_dir

    def __repr__(self) -> str:
        calendar_name = self._calendar.name if self._calendar is not None else None
        return (
            f"{type(self).__name__}(rule_sets={len(self._rule_sets)}, calendar={calendar_name!r})"
        )
