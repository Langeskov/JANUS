"""Market data domain models: bars, snapshots, corporate actions, quality.

Every fact carries a :class:`~janus.domain.common.time.PointInTime` so decisions
can be replayed point-in-time (Phase 0 spec s7.3).

Critical accounting rule (spec s7.4): simulated execution uses **unadjusted**
prices only. Adjusted series are for return analysis and must never be used to
fill an order.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from janus.domain.common.enums import AdjustmentMode, DataQualityStatus, QuoteSource
from janus.domain.common.errors import DataQualityError, ValidationError
from janus.domain.common.money import as_decimal
from janus.domain.common.time import PointInTime, ensure_aware
from janus.domain.instruments.symbol import validate_symbol

__all__ = [
    "AdjustmentFactor",
    "CorporateAction",
    "CorporateActionType",
    "DataQualityIssue",
    "DataQualityReport",
    "DataSnapshot",
    "MarketBar",
    "check_bar_quality",
]


class MarketBar(BaseModel):
    """One daily OHLCV bar.

    ``volume`` is in shares/units; ``amount`` is turnover in CNY when reported.
    Both raw and normalised provenance are retained (spec s7.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = Field(ge=0)
    amount: Decimal | None = None

    adjustment: AdjustmentMode = AdjustmentMode.UNADJUSTED
    quality: DataQualityStatus = DataQualityStatus.PENDING
    source: QuoteSource = QuoteSource.FIXTURE
    source_ref: str = ""
    raw_payload_hash: str | None = None

    point_in_time: PointInTime

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return validate_symbol(value)

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        amount = as_decimal(value, "price")
        if amount <= 0:
            raise ValidationError("OHLC prices must be strictly positive")
        return amount

    @field_validator("amount")
    @classmethod
    def _amount(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        amount = as_decimal(value, "amount")
        if amount < 0:
            raise ValidationError("amount must be non-negative")
        return amount

    @model_validator(mode="after")
    def _ohlc_consistency(self) -> MarketBar:
        if self.high < self.low:
            raise ValidationError("bar high is below low", symbol=self.symbol)
        if not (self.low <= self.open <= self.high):
            raise ValidationError("bar open outside [low, high]", symbol=self.symbol)
        if not (self.low <= self.close <= self.high):
            raise ValidationError("bar close outside [low, high]", symbol=self.symbol)
        return self

    @property
    def is_tradable(self) -> bool:
        """A bar can only back execution when it is raw, clean and has volume."""
        return (
            self.adjustment is AdjustmentMode.UNADJUSTED
            and self.quality is DataQualityStatus.OK
            and self.volume > 0
        )

    @property
    def is_zero_volume(self) -> bool:
        return self.volume == 0

    def is_locked(self, limit_up: Decimal | None, limit_down: Decimal | None) -> bool:
        """True when the whole bar sits at a limit price (locked board).

        Daily OHLC cannot reveal intraday ordering or queue priority, so a bar
        whose low equals its high equals a limit price is treated as
        un-fillable (spec s6.2: never infer a fill from a crossed limit).
        """
        if limit_up is not None and self.low == self.high == limit_up:
            return True
        return limit_down is not None and self.low == self.high == limit_down


class CorporateActionType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    SPLIT = "SPLIT"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    ETF_UNIT_CHANGE = "ETF_UNIT_CHANGE"


class CorporateAction(BaseModel):
    """A corporate action, retained separately from execution prices."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str
    symbol: str
    action_type: CorporateActionType
    ex_date: date
    record_date: date | None = None
    pay_date: date | None = None
    cash_per_share: Decimal | None = None
    ratio: Decimal | None = None
    description: str = ""
    point_in_time: PointInTime

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return validate_symbol(value)

    @model_validator(mode="after")
    def _validate(self) -> CorporateAction:
        if self.action_type is CorporateActionType.CASH_DIVIDEND:
            if self.cash_per_share is None or self.cash_per_share <= 0:
                raise ValidationError("a cash dividend requires a positive cash_per_share")
        elif self.ratio is None or self.ratio <= 0:
            raise ValidationError("a share-changing action requires a positive ratio")
        return self


class AdjustmentFactor(BaseModel):
    """Per-date adjustment factor used for return series only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    effective_date: date
    factor: Decimal = Field(gt=0)
    action_id: str

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return validate_symbol(value)


class DataSnapshot(BaseModel):
    """Immutable, content-addressed provenance bundle for a decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str
    created_at: datetime
    as_of: datetime
    """Latest information time represented by the snapshot."""

    provider: QuoteSource
    symbols: tuple[str, ...]
    bar_dates: tuple[date, ...]
    bar_count: int = Field(ge=0)
    content_hash: str
    quality: DataQualityStatus = DataQualityStatus.OK
    rule_set_id: str | None = None
    notes: str = ""

    @field_validator("created_at", "as_of")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "timestamp")

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for symbol in value:
            validate_symbol(symbol)
        if len(value) != len(set(value)):
            raise ValidationError("duplicate symbol in data snapshot")
        return value

    def is_stale_at(self, at: datetime, *, max_age_seconds: int) -> bool:
        at = ensure_aware(at, "at")
        return (at - self.as_of).total_seconds() > max_age_seconds

    def is_usable(self) -> bool:
        return self.quality is DataQualityStatus.OK


class DataQualityIssue(StrEnum):
    DUPLICATE_KEY = "DUPLICATE_KEY"
    IMPOSSIBLE_OHLC = "IMPOSSIBLE_OHLC"
    INVALID_DATE = "INVALID_DATE"
    MISSING_FIELD = "MISSING_FIELD"
    STALE_TIMESTAMP = "STALE_TIMESTAMP"
    UNSUPPORTED_ADJUSTMENT = "UNSUPPORTED_ADJUSTMENT"
    INCONSISTENT_VOLUME_AMOUNT = "INCONSISTENT_VOLUME_AMOUNT"
    UNEXPLAINED_PRICE_JUMP = "UNEXPLAINED_PRICE_JUMP"
    ZERO_VOLUME = "ZERO_VOLUME"


class DataQualityReport(BaseModel):
    """Outcome of quality-checking one bar (or a small batch)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    trade_date: date
    status: DataQualityStatus
    issues: tuple[DataQualityIssue, ...] = ()
    checked_at: datetime
    message: str = ""

    @model_validator(mode="after")
    def _status_matches_issues(self) -> DataQualityReport:
        if self.status is DataQualityStatus.OK and self.issues:
            raise ValidationError("an OK quality report must not carry issues")
        if self.status is DataQualityStatus.QUARANTINED and not self.issues:
            raise ValidationError("a QUARANTINED report must name at least one issue")
        return self

    @property
    def is_executable(self) -> bool:
        return self.status is DataQualityStatus.OK


def check_bar_quality(
    bar: MarketBar,
    *,
    checked_at: datetime,
    previous: MarketBar | None = None,
    max_bar_age_days: int = 14,
    max_price_jump_ratio: Decimal = Decimal("0.5"),
    max_volume_amount_deviation: Decimal = Decimal("0.5"),
) -> DataQualityReport:
    """Apply Phase 0 spec s7.4 data-quality checks to one bar.

    Returns a report; never raises for bad data (the caller decides whether to
    quarantine). Structural model violations are impossible here because
    :class:`MarketBar` already enforces OHLC sanity.
    """
    issues: list[DataQualityIssue] = []
    messages: list[str] = []
    checked_at = ensure_aware(checked_at, "checked_at")

    if bar.adjustment is not AdjustmentMode.UNADJUSTED:
        issues.append(DataQualityIssue.UNSUPPORTED_ADJUSTMENT)
        messages.append("only unadjusted bars may back simulated execution")

    if bar.volume == 0:
        issues.append(DataQualityIssue.ZERO_VOLUME)
        messages.append("bar reports zero volume")

    if bar.volume > 0 and bar.amount is not None:
        vwap = bar.amount / Decimal(bar.volume)
        if vwap < bar.low * (Decimal(1) - max_volume_amount_deviation) or vwap > bar.high * (
            Decimal(1) + max_volume_amount_deviation
        ):
            issues.append(DataQualityIssue.INCONSISTENT_VOLUME_AMOUNT)
            messages.append(f"implied VWAP {vwap:.4f} is inconsistent with the [low, high] range")

    age_days = (checked_at.date() - bar.trade_date).days
    if age_days > max_bar_age_days:
        issues.append(DataQualityIssue.STALE_TIMESTAMP)
        messages.append(f"bar is {age_days} calendar days old")

    if bar.point_in_time.known_at > checked_at:
        issues.append(DataQualityIssue.INVALID_DATE)
        messages.append("bar is not yet known at the checking time (look-ahead)")

    if previous is not None and previous.close > 0:
        jump = abs(bar.close - previous.close) / previous.close
        if jump > max_price_jump_ratio:
            issues.append(DataQualityIssue.UNEXPLAINED_PRICE_JUMP)
            messages.append(
                f"close moved {jump:.2%} versus the previous bar without a known corporate action"
            )

    status = DataQualityStatus.OK if not issues else DataQualityStatus.QUARANTINED
    return DataQualityReport(
        symbol=bar.symbol,
        trade_date=bar.trade_date,
        status=status,
        issues=tuple(dict.fromkeys(issues)),
        checked_at=checked_at,
        message="; ".join(messages),
    )


def require_executable_bar(bar: MarketBar) -> None:
    """Raise :class:`DataQualityError` when a bar may not back execution."""
    if not bar.is_tradable:
        raise DataQualityError(
            "bar cannot back simulated execution",
            symbol=bar.symbol,
            trade_date=bar.trade_date.isoformat(),
            adjustment=bar.adjustment.value,
            quality=bar.quality.value,
            volume=bar.volume,
        )
