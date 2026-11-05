"""Versioned, effective-dated market-rules engine (Phase 0 spec s2.2, s2.3, P0-D).

Resolution model
----------------
The engine is *data driven*: it resolves rules from the effective-dated
:class:`~janus.domain.market_rules.models.TradingRuleSet` configuration. It never
hard-codes a global price-limit percentage, lot size, tick size or settlement
flag (spec s2.2).

Selection order:

1. pick the rule set effective at the requested instant (default: the trade
   date's 09:30 Asia/Shanghai market open);
2. pick the most specific :class:`~janus.domain.market_rules.models.RuleProfile`
   matching exchange / board / instrument type / risk-warning status / explicit
   symbol allow-list;
3. resolve the profile's effective-dated fee schedule.

Fail closed (spec s2.2, P0-D): an unsupported board/type, a missing profile, an
ambiguous tie, a missing trading session, a missing price-limit declaration or a
fee schedule that is not effective at the trade date is an
:class:`~janus.domain.common.errors.UnsupportedEntityError`, never a default.

New-listing no-limit stage
--------------------------
Profiles that declare ``no_price_limit_reason`` (instead of a numeric
``price_limit_pct``) are *stage* profiles: they are never selected during normal
resolution. The caller declares the stage explicitly with
``resolve(..., new_listing_stage=True)`` when the listing-stage window is known
from the official calendar/notice. The engine does not guess the window from
``Instrument.listing_date`` because Phase 0 has no rule to count "N trading days
since listing" inside the rules layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from itertools import pairwise
from typing import Final

from janus.domain.common.enums import OrderSide
from janus.domain.common.errors import ConfigurationError, UnsupportedEntityError
from janus.domain.common.time import MARKET_TZ, ensure_aware
from janus.domain.instruments.models import Instrument
from janus.domain.market_rules.fees import compute_fee_breakdown
from janus.domain.market_rules.models import (
    FeeBreakdown,
    FeeSchedule,
    ResolvedTradingRules,
    RuleProfile,
    RuleSetStatus,
    TradingRuleSet,
)

MARKET_OPEN_TIME: Final[time] = time(9, 30)
"""Default resolution instant for a trade date: the continuous-session open."""

__all__ = [
    "MARKET_OPEN_TIME",
    "MarketRulesEngine",
]


class MarketRulesEngine:
    """Effective-dated rules resolver over a set of versioned rule-sets."""

    __slots__ = ("_rule_sets", "_usable_rule_sets")

    def __init__(self, rule_sets: Sequence[TradingRuleSet]) -> None:
        """Build an engine from registered rule-sets.

        Validation (all fail closed with :class:`ConfigurationError`):
        at least one rule-set is required, ``(rule_set_id, version)`` pairs are
        unique, at least one rule-set is ``ACTIVE``, and the effective windows of
        the selectable rule-sets (``ACTIVE`` or ``SUPERSEDED`` replay baselines)
        do not overlap.
        """
        if not rule_sets:
            raise ConfigurationError("a market-rules engine requires at least one rule set")
        self._rule_sets: tuple[TradingRuleSet, ...] = tuple(rule_sets)
        seen: set[tuple[str, str]] = set()
        for rule_set in self._rule_sets:
            key = (rule_set.rule_set_id, rule_set.version)
            if key in seen:
                raise ConfigurationError(
                    "duplicate rule-set registration",
                    rule_set_id=rule_set.rule_set_id,
                    version=rule_set.version,
                )
            seen.add(key)
        if not any(rs.status is RuleSetStatus.ACTIVE for rs in self._rule_sets):
            raise ConfigurationError(
                "at least one ACTIVE rule set is required",
                rule_set_ids=[rs.rule_set_id for rs in self._rule_sets],
            )
        usable = tuple(
            sorted(
                (rs for rs in self._rule_sets if rs.status is not RuleSetStatus.DRAFT),
                key=lambda rs: rs.effective_from,
            )
        )
        if not usable:  # pragma: no cover - an ACTIVE set is always usable
            raise ConfigurationError("no selectable rule set (all are DRAFT)")
        for previous, following in pairwise(usable):
            if previous.effective_to is None or previous.effective_to > following.effective_from:
                raise ConfigurationError(
                    "rule-set effective windows overlap",
                    previous_rule_set_id=previous.rule_set_id,
                    previous_effective_to=(
                        previous.effective_to.isoformat()
                        if previous.effective_to is not None
                        else None
                    ),
                    next_rule_set_id=following.rule_set_id,
                    next_effective_from=following.effective_from.isoformat(),
                )
        self._usable_rule_sets: tuple[TradingRuleSet, ...] = usable

    # -- rule-set access ------------------------------------------------
    def rule_sets(self) -> tuple[TradingRuleSet, ...]:
        """Every registered rule-set, including ``DRAFT`` and superseded ones."""
        return self._rule_sets

    def rule_set_for(self, at: datetime) -> TradingRuleSet:
        """Select the rule-set effective at the aware instant ``at``."""
        moment = ensure_aware(at, "at")
        matches = tuple(rs for rs in self._usable_rule_sets if rs.is_effective_at(moment))
        if not matches:
            raise UnsupportedEntityError(
                "no registered rule set is effective at that instant",
                at=moment.isoformat(),
                rule_set_ids=[rs.rule_set_id for rs in self._usable_rule_sets],
            )
        if len(matches) > 1:  # pragma: no cover - prevented by constructor validation
            raise UnsupportedEntityError(
                "rule-set effective windows are ambiguous at that instant",
                at=moment.isoformat(),
                rule_set_ids=[rs.rule_set_id for rs in matches],
            )
        return matches[0]

    # -- resolution -----------------------------------------------------
    def resolve(
        self,
        instrument: Instrument,
        trade_date: date,
        *,
        at: datetime | None = None,
        new_listing_stage: bool = False,
    ) -> ResolvedTradingRules:
        """Flatten the applicable rules for ``instrument`` on ``trade_date``.

        ``at`` selects the rule set; when omitted the trade date's market open
        (09:30 Asia/Shanghai) is used. ``new_listing_stage`` declares that the
        security is inside a configured no-price-limit listing stage, which
        selects the rule-set's explicit stage profile instead of the normal one.
        """
        if instrument.is_cash:
            raise UnsupportedEntityError(
                "the CNY cash line has no trading rules", symbol=instrument.symbol
            )
        market_open = datetime.combine(trade_date, MARKET_OPEN_TIME, MARKET_TZ)
        moment = market_open if at is None else ensure_aware(at, "at")
        if not instrument.is_listed_on(trade_date):
            raise UnsupportedEntityError(
                "instrument is not listed on the trade date",
                symbol=instrument.symbol,
                trade_date=trade_date.isoformat(),
                status=instrument.status.value,
            )
        rule_set = self.rule_set_for(moment)
        if not rule_set.is_effective_at(market_open):
            raise UnsupportedEntityError(
                "the rule set selected at 'at' does not cover the trade date",
                symbol=instrument.symbol,
                at=moment.isoformat(),
                trade_date=trade_date.isoformat(),
                rule_set_id=rule_set.rule_set_id,
            )
        profile = self._select_profile(rule_set, instrument, new_listing_stage=new_listing_stage)
        self._validate_profile(
            rule_set=rule_set,
            profile=profile,
            instrument=instrument,
            new_listing_stage=new_listing_stage,
        )
        fee_schedule = self._fee_schedule_for(
            rule_set=rule_set,
            fee_schedule_id=profile.fee_schedule_id,
            at=market_open,
        )
        return ResolvedTradingRules(
            rule_set_id=rule_set.rule_set_id,
            rule_set_version=rule_set.version,
            profile_id=profile.profile_id,
            symbol=instrument.symbol,
            trade_date=trade_date,
            settlement_rule=profile.settlement_rule,
            tick_size=profile.tick_size,
            buy_quantity_increment=profile.buy_quantity_increment,
            min_buy_quantity=profile.min_buy_quantity,
            sell_quantity_increment=profile.sell_quantity_increment,
            odd_lot_sell_must_be_whole_position=(profile.odd_lot_sell_must_be_whole_position),
            price_limit_pct=profile.price_limit_pct,
            risk_warning_price_limit_pct=profile.risk_warning_price_limit_pct,
            no_price_limit_reason=profile.no_price_limit_reason,
            allowed_order_types=profile.allowed_order_types,
            allowed_validities=profile.allowed_validities,
            sessions=rule_set.default_sessions,
            fee_schedule_id=fee_schedule.fee_schedule_id,
            fee_schedule_version=fee_schedule.version,
            effective_from=rule_set.effective_from,
            effective_to=rule_set.effective_to,
            source_refs=rule_set.source_refs,
        )

    # -- profile selection ----------------------------------------------
    def _select_profile(
        self,
        rule_set: TradingRuleSet,
        instrument: Instrument,
        *,
        new_listing_stage: bool,
    ) -> RuleProfile:
        matching = [
            profile
            for profile in rule_set.profiles
            if (profile.no_price_limit_reason is not None) == new_listing_stage
            and profile.matches(
                exchange=instrument.exchange,
                board=instrument.board,
                instrument_type=instrument.instrument_type,
                is_risk_warning=instrument.is_risk_warning,
                symbol=instrument.symbol,
            )
        ]
        if not matching:
            raise UnsupportedEntityError(
                "no rule profile matches the instrument",
                symbol=instrument.symbol,
                exchange=instrument.exchange.value,
                board=instrument.board.value,
                instrument_type=instrument.instrument_type.value,
                is_risk_warning=instrument.is_risk_warning,
                new_listing_stage=new_listing_stage,
                rule_set_id=rule_set.rule_set_id,
            )
        best = max(profile.specificity() for profile in matching)
        winners = [profile for profile in matching if profile.specificity() == best]
        if len(winners) != 1:
            raise UnsupportedEntityError(
                "ambiguous rule profiles (equal specificity)",
                symbol=instrument.symbol,
                rule_set_id=rule_set.rule_set_id,
                profile_ids=sorted(profile.profile_id for profile in winners),
                specificity=best,
            )
        return winners[0]

    @staticmethod
    def _validate_profile(
        *,
        rule_set: TradingRuleSet,
        profile: RuleProfile,
        instrument: Instrument,
        new_listing_stage: bool,
    ) -> None:
        """Reject incomplete rule data instead of guessing a default."""
        if profile.tick_size <= 0:
            raise UnsupportedEntityError(
                "rule profile declares a non-positive tick size",
                profile_id=profile.profile_id,
                tick_size=str(profile.tick_size),
            )
        if not rule_set.default_sessions:
            raise UnsupportedEntityError(
                "rule set does not declare the trading sessions it simulates",
                rule_set_id=rule_set.rule_set_id,
            )
        if not profile.allowed_order_types or not profile.allowed_validities:
            raise UnsupportedEntityError(
                "rule profile does not declare eligible order types/validities",
                profile_id=profile.profile_id,
            )
        if new_listing_stage:
            if (
                profile.no_price_limit_reason is None
                or profile.price_limit_pct is not None
                or profile.risk_warning_price_limit_pct is not None
            ):
                raise UnsupportedEntityError(
                    "new-listing profile must declare an explicit no-limit reason "
                    "and no numeric price limit",
                    profile_id=profile.profile_id,
                )
        else:
            if profile.price_limit_pct is None:
                raise UnsupportedEntityError(
                    "rule profile neither declares a price limit nor an explicit no-limit reason",
                    profile_id=profile.profile_id,
                )
            _require_limit_pct(profile.price_limit_pct, profile=profile, field="price_limit_pct")
        if profile.risk_warning_price_limit_pct is not None:
            _require_limit_pct(
                profile.risk_warning_price_limit_pct,
                profile=profile,
                field="risk_warning_price_limit_pct",
            )
        if (
            instrument.is_risk_warning
            and not profile.risk_warning_only
            and profile.risk_warning_price_limit_pct is None
        ):
            raise UnsupportedEntityError(
                "risk-warning security resolved to a profile that does not declare "
                "a risk-warning price limit",
                symbol=instrument.symbol,
                profile_id=profile.profile_id,
            )

    # -- fees -----------------------------------------------------------
    def fee_schedule(self, rules: ResolvedTradingRules, at: datetime | None = None) -> FeeSchedule:
        """Effective-dated fee schedule referenced by already-resolved rules."""
        rule_set = self._rule_set_for_id(rules.rule_set_id, rules.rule_set_version)
        moment = (
            datetime.combine(rules.trade_date, MARKET_OPEN_TIME, MARKET_TZ)
            if at is None
            else ensure_aware(at, "at")
        )
        schedule = self._fee_schedule_for(
            rule_set=rule_set, fee_schedule_id=rules.fee_schedule_id, at=moment
        )
        if schedule.version != rules.fee_schedule_version:
            raise UnsupportedEntityError(
                "resolved rules reference a different fee-schedule version",
                fee_schedule_id=rules.fee_schedule_id,
                expected_version=rules.fee_schedule_version,
                actual_version=schedule.version,
            )
        return schedule

    def compute_fees(
        self,
        *,
        rules: ResolvedTradingRules,
        side: OrderSide,
        quantity: int,
        price: Decimal,
    ) -> FeeBreakdown:
        """Itemised fees for one execution under the resolved rules."""
        schedule = self.fee_schedule(rules)
        return compute_fee_breakdown(schedule, side=side, quantity=quantity, price=price)

    # -- internal lookup helpers ----------------------------------------
    def _rule_set_for_id(self, rule_set_id: str, version: str) -> TradingRuleSet:
        for rule_set in self._rule_sets:
            if rule_set.rule_set_id == rule_set_id and rule_set.version == version:
                return rule_set
        raise UnsupportedEntityError(
            "resolved rules reference an unregistered rule set",
            rule_set_id=rule_set_id,
            version=version,
        )

    @staticmethod
    def _fee_schedule_for(
        *, rule_set: TradingRuleSet, fee_schedule_id: str, at: datetime
    ) -> FeeSchedule:
        schedule = next(
            (
                candidate
                for candidate in rule_set.fee_schedules
                if candidate.fee_schedule_id == fee_schedule_id
            ),
            None,
        )
        if schedule is None:
            raise UnsupportedEntityError(
                "rule profile references a fee schedule missing from its rule set",
                rule_set_id=rule_set.rule_set_id,
                fee_schedule_id=fee_schedule_id,
            )
        if not schedule.is_effective_at(at):
            raise UnsupportedEntityError(
                "fee schedule is not effective at the requested instant",
                fee_schedule_id=fee_schedule_id,
                version=schedule.version,
                at=at.isoformat(),
                effective_from=schedule.effective_from.isoformat(),
                effective_to=(
                    schedule.effective_to.isoformat() if schedule.effective_to is not None else None
                ),
            )
        return schedule

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(rule_sets={len(self._rule_sets)}, "
            f"selectable={len(self._usable_rule_sets)})"
        )


def _require_limit_pct(value: Decimal, *, profile: RuleProfile, field: str) -> None:
    """A price-limit percentage must be a genuine fraction in ``(0, 1)``."""
    if not Decimal("0") < value < Decimal("1"):
        raise UnsupportedEntityError(
            "price-limit percentage must be within (0, 1)",
            profile_id=profile.profile_id,
            field=field,
            value=str(value),
        )
