"""Effective-dated market-rule models.

Phase 0 spec s2.2 requires a *versioned* rules engine that resolves rules from
exchange, board, instrument type, listing/status, trade date and rule-set
effective dates. These models are the data contract; resolution logic lives in
:mod:`janus.domain.market_rules.engine` and the machine-readable registry lives
in ``config/market_rules/``.

Nothing here hard-codes a single global price-limit percentage, lot size, tick
size or settlement flag.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from janus.domain.common.enums import (
    Board,
    Exchange,
    FeeComponent,
    InstrumentType,
    OrderSide,
    OrderType,
    OrderValidity,
    SessionType,
    SettlementRule,
    TradingDayType,
)
from janus.domain.common.errors import ValidationError
from janus.domain.common.money import as_decimal
from janus.domain.common.time import ensure_aware

__all__ = [
    "FeeBasis",
    "FeeBreakdown",
    "FeeLine",
    "FeeRate",
    "FeeSchedule",
    "FeeScheduleStatus",
    "PriceLimits",
    "ResolvedTradingRules",
    "RuleProfile",
    "RuleSetStatus",
    "TradingDay",
    "TradingRuleSet",
    "TradingSession",
]


class FeeScheduleStatus(StrEnum):
    """How trustworthy the fee numbers are (spec s6.3)."""

    ESTIMATED = "ESTIMATED"
    VERIFIED = "VERIFIED"


class RuleSetStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


class FeeBasis(StrEnum):
    """What a fee rate applies to."""

    NOTIONAL = "NOTIONAL"
    """Rate x executed trade value (commission, stamp duty)."""

    QUANTITY = "QUANTITY"
    """Rate x executed share quantity (per-share handling fees)."""

    PER_ORDER = "PER_ORDER"
    """Flat amount per order (minimum-commission style)."""


# ---------------------------------------------------------------------------
# Sessions and calendar days
# ---------------------------------------------------------------------------


class TradingSession(BaseModel):
    """One explicit market session window, expressed in Asia/Shanghai wall time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_type: SessionType
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def _window(self) -> TradingSession:
        if self.start_time >= self.end_time:
            raise ValidationError(
                "session start_time must precede end_time",
                session_type=self.session_type.value,
            )
        return self

    def contains(self, at: time) -> bool:
        """Half-open containment: ``start_time <= at < end_time``."""
        return self.start_time <= at < self.end_time

    def allows_execution(self) -> bool:
        """Only continuous and closing-auction sessions support Phase 0 fills."""
        return self.session_type in {SessionType.CONTINUOUS, SessionType.CLOSING_AUCTION}


class TradingDay(BaseModel):
    """One calendar day, with its classified type and (if trading) sessions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date
    day_type: TradingDayType
    sessions: tuple[TradingSession, ...] = ()

    @model_validator(mode="after")
    def _sessions_match_type(self) -> TradingDay:
        if self.day_type is TradingDayType.TRADING:
            if not self.sessions:
                raise ValidationError("a TRADING day must declare its sessions")
        elif self.sessions:
            raise ValidationError("a non-trading day must not declare sessions")
        return self

    @property
    def is_trading(self) -> bool:
        return self.day_type is TradingDayType.TRADING


# ---------------------------------------------------------------------------
# Rule profiles
# ---------------------------------------------------------------------------


class RuleProfile(BaseModel):
    """Effective-dated rule bundle matched against an instrument's attributes.

    A profile declares *optional* match dimensions. The engine selects the most
    specific matching profile and fails closed when no profile matches
    (Phase 0 spec s2.2: "Unsupported or incomplete rules must reject the order").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    description: str = ""

    # -- match dimensions (None = wildcard) -----------------------------
    exchange: Exchange | None = None
    board: Board | None = None
    instrument_type: InstrumentType | None = None
    risk_warning_only: bool = False
    """When True this profile only applies to ST/*ST securities."""

    symbols: tuple[str, ...] | None = None
    """Explicit symbol allow-list (highest specificity).

    Used for genuine product-level exceptions that are not derivable from
    board/instrument-type alone -- e.g. an explicitly T+0 ETF fixture, or a
    security whose settlement treatment differs from its product family. When
    ``None`` the profile applies to any symbol.
    """

    # -- settlement ------------------------------------------------------
    settlement_rule: SettlementRule

    # -- price grid ------------------------------------------------------
    tick_size: Decimal
    """Minimum price increment in CNY (e.g. ``0.01`` stock, ``0.001`` ETF)."""

    price_limit_pct: Decimal | None = None
    """Normal daily price-limit percentage, e.g. ``0.10``; None = no limit."""

    risk_warning_price_limit_pct: Decimal | None = None
    """Price-limit percentage applied when the security carries a ST/*ST flag."""

    no_price_limit_reason: str | None = None
    """Set only when Phase 0 deliberately models a no-limit session/state."""

    # -- quantity grid ---------------------------------------------------
    buy_quantity_increment: int = Field(default=100, gt=0)
    min_buy_quantity: int = Field(default=100, gt=0)
    sell_quantity_increment: int = Field(default=1, gt=0)
    """Sells may dispose of an odd residual lot, hence increment 1."""

    odd_lot_sell_must_be_whole_position: bool = True

    # -- order types -----------------------------------------------------
    allowed_order_types: tuple[OrderType, ...] = (OrderType.LIMIT,)
    allowed_validities: tuple[OrderValidity, ...] = (OrderValidity.DAY,)

    # -- costs -----------------------------------------------------------
    fee_schedule_id: str = "CN-EQUITY-DEFAULT"

    def matches(
        self,
        *,
        exchange: Exchange,
        board: Board,
        instrument_type: InstrumentType,
        is_risk_warning: bool,
        symbol: str | None = None,
    ) -> bool:
        if self.risk_warning_only and not is_risk_warning:
            return False
        if self.symbols is not None and (symbol is None or symbol not in self.symbols):
            return False
        if self.exchange is not None and self.exchange is not exchange:
            return False
        if self.board is not None and self.board is not board:
            return False
        return not (
            self.instrument_type is not None and self.instrument_type is not instrument_type
        )

    def specificity(self) -> int:
        """Higher = more specific. Used to pick the winning profile."""
        score = 0
        if self.symbols is not None:
            score += 10
        if self.exchange is not None:
            score += 1
        if self.board is not None:
            score += 1
        if self.instrument_type is not None:
            score += 1
        if self.risk_warning_only:
            score += 2
        return score


class FeeRate(BaseModel):
    """One separately represented fee component."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component: FeeComponent
    basis: FeeBasis
    rate: Decimal = Field(ge=Decimal("0"))
    """Fraction for NOTIONAL/QUANTITY bases; absolute CNY for PER_ORDER."""

    applicable_sides: tuple[OrderSide, ...] = (OrderSide.BUY, OrderSide.SELL)
    min_amount: Decimal | None = Field(default=None, ge=Decimal("0"))
    max_amount: Decimal | None = Field(default=None, ge=Decimal("0"))
    source: str = ""
    note: str = ""

    @model_validator(mode="after")
    def _bounds(self) -> FeeRate:
        if (
            self.max_amount is not None
            and self.min_amount is not None
            and self.max_amount < self.min_amount
        ):
            raise ValidationError("fee max_amount must be >= min_amount")
        return self

    def applies_to(self, side: OrderSide) -> bool:
        return side in self.applicable_sides


class FeeSchedule(BaseModel):
    """Effective-dated, componentised fee schedule (spec s6.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fee_schedule_id: str
    version: str
    status: FeeScheduleStatus = FeeScheduleStatus.ESTIMATED
    effective_from: datetime
    effective_to: datetime | None = None
    source_refs: tuple[str, ...] = ()
    notes: str = ""
    lines: tuple[FeeRate, ...]

    @model_validator(mode="after")
    def _window(self) -> FeeSchedule:
        ensure_aware(self.effective_from, "effective_from")
        if self.effective_to is not None:
            ensure_aware(self.effective_to, "effective_to")
            if self.effective_to <= self.effective_from:
                raise ValidationError("fee schedule effective_to must be after effective_from")
        return self

    def is_effective_at(self, at: datetime) -> bool:
        at = ensure_aware(at, "at")
        if at < self.effective_from:
            return False
        return not (self.effective_to is not None and at >= self.effective_to)

    def lines_for(self, side: OrderSide) -> tuple[FeeRate, ...]:
        return tuple(line for line in self.lines if line.applies_to(side))


class FeeLine(BaseModel):
    """A computed fee amount for one component."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component: FeeComponent
    basis: FeeBasis
    rate: Decimal
    amount: Decimal

    def as_money(self) -> Decimal:
        return self.amount


class FeeBreakdown(BaseModel):
    """Itemised, reproducible cost calculation attached to a fill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fee_schedule_id: str
    fee_schedule_version: str
    side: OrderSide
    notional: Decimal
    lines: tuple[FeeLine, ...]

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0"))

    def by_component(self) -> dict[FeeComponent, Decimal]:
        totals: dict[FeeComponent, Decimal] = {}
        for line in self.lines:
            totals[line.component] = totals.get(line.component, Decimal("0")) + line.amount
        return totals


# ---------------------------------------------------------------------------
# Rule-set and resolved rules
# ---------------------------------------------------------------------------


class TradingRuleSet(BaseModel):
    """A versioned, effective-dated bundle of trading rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_set_id: str
    version: str
    market: str = "CN"
    status: RuleSetStatus = RuleSetStatus.ACTIVE
    effective_from: datetime
    effective_to: datetime | None = None
    published_at: datetime | None = None
    published_by: str = ""
    source_refs: tuple[str, ...] = ()
    notes: str = ""

    default_sessions: tuple[TradingSession, ...]
    profiles: tuple[RuleProfile, ...]
    fee_schedules: tuple[FeeSchedule, ...]

    @model_validator(mode="after")
    def _validate(self) -> TradingRuleSet:
        ensure_aware(self.effective_from, "effective_from")
        if self.effective_to is not None:
            ensure_aware(self.effective_to, "effective_to")
            if self.effective_to <= self.effective_from:
                raise ValidationError("rule set effective_to must be after effective_from")
        if not self.profiles:
            raise ValidationError("a rule set must declare at least one profile")
        ids = [p.profile_id for p in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValidationError("duplicate profile_id in rule set", duplicates=ids)
        fee_ids = {f.fee_schedule_id for f in self.fee_schedules}
        for profile in self.profiles:
            if profile.fee_schedule_id not in fee_ids:
                raise ValidationError(
                    "rule profile references an unknown fee schedule",
                    profile_id=profile.profile_id,
                    fee_schedule_id=profile.fee_schedule_id,
                )
        return self

    def is_effective_at(self, at: datetime) -> bool:
        at = ensure_aware(at, "at")
        if at < self.effective_from:
            return False
        return not (self.effective_to is not None and at >= self.effective_to)

    def fee_schedule(self, fee_schedule_id: str) -> FeeSchedule:
        for schedule in self.fee_schedules:
            if schedule.fee_schedule_id == fee_schedule_id:
                return schedule
        raise ValidationError("unknown fee schedule", fee_schedule_id=fee_schedule_id)


class PriceLimits(BaseModel):
    """Resolved absolute price limits for one instrument on one trading day."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit_up: Decimal | None = None
    limit_down: Decimal | None = None
    pct: Decimal | None = None
    no_limit_reason: str | None = None

    @property
    def has_limits(self) -> bool:
        return self.limit_up is not None and self.limit_down is not None

    def is_locked_at_upper(self, price: Decimal) -> bool:
        """True when ``price`` is at/above the upper limit (no trading can occur above)."""
        return self.limit_up is not None and price >= self.limit_up

    def is_locked_at_lower(self, price: Decimal) -> bool:
        return self.limit_down is not None and price <= self.limit_down


class ResolvedTradingRules(BaseModel):
    """The flattened, audit-ready rules for one symbol on one trade date.

    Every paper order and fill must retain the ``rule_set_id`` /
    ``rule_set_version`` / ``profile_id`` captured here (acceptance criterion 9).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_set_id: str
    rule_set_version: str
    profile_id: str
    symbol: str
    trade_date: date

    settlement_rule: SettlementRule
    tick_size: Decimal
    buy_quantity_increment: int
    min_buy_quantity: int
    sell_quantity_increment: int
    odd_lot_sell_must_be_whole_position: bool = True

    price_limit_pct: Decimal | None = None
    risk_warning_price_limit_pct: Decimal | None = None
    no_price_limit_reason: str | None = None

    allowed_order_types: tuple[OrderType, ...] = (OrderType.LIMIT,)
    allowed_validities: tuple[OrderValidity, ...] = (OrderValidity.DAY,)

    sessions: tuple[TradingSession, ...] = ()
    fee_schedule_id: str
    fee_schedule_version: str

    effective_from: datetime
    effective_to: datetime | None = None
    source_refs: tuple[str, ...] = ()

    # -- derived helpers -------------------------------------------------
    def effective_price_limit_pct(self, is_risk_warning: bool) -> Decimal | None:
        """Price-limit percentage for this security's current status."""
        if is_risk_warning and self.risk_warning_price_limit_pct is not None:
            return self.risk_warning_price_limit_pct
        return self.price_limit_pct

    def compute_price_limits(self, prev_close: Decimal, *, is_risk_warning: bool) -> PriceLimits:
        """Compute absolute limit-up/limit-down prices.

        Exchange convention: ``prev_close * (1 +/- pct)`` rounded half-up to the
        instrument's tick size. When the profile declares no limit (or the
        security's status resolves to no limit) the result carries the explicit
        ``no_limit_reason`` instead of an invented band.
        """
        pct = self.effective_price_limit_pct(is_risk_warning)
        if pct is None:
            return PriceLimits(
                limit_up=None,
                limit_down=None,
                pct=None,
                no_limit_reason=self.no_price_limit_reason or "NO_LIMIT_PROFILE",
            )
        prev_close = as_decimal(prev_close, "prev_close")
        if prev_close <= 0:
            raise ValidationError("prev_close must be positive to compute price limits")
        up = (prev_close * (Decimal(1) + pct)).quantize(self.tick_size, rounding="ROUND_HALF_UP")
        down = (prev_close * (Decimal(1) - pct)).quantize(self.tick_size, rounding="ROUND_HALF_UP")
        if down < self.tick_size:
            down = self.tick_size
        return PriceLimits(limit_up=up, limit_down=down, pct=pct)

    def allows(self, order_type: OrderType, validity: OrderValidity) -> bool:
        return order_type in self.allowed_order_types and validity in self.allowed_validities

    def session_at(self, at: time) -> TradingSession | None:
        for session in self.sessions:
            if session.contains(at):
                return session
        return None
