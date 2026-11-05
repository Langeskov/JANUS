"""Strategy registry and regime classification.

Phase 0 defines the contracts and data structures only; it does not implement a
profitable strategy (spec s3.2). A strategy may only be referenced by the
portfolio manager if it is registered *and* versioned here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from janus.domain.common.enums import InstrumentType
from janus.domain.common.errors import NotFoundError, UnsupportedEntityError, ValidationError
from janus.domain.common.time import ensure_aware

__all__ = [
    "ParameterBound",
    "RegimeClass",
    "StrategyDefinition",
    "StrategyRegistry",
    "StrategyStatus",
]


class StrategyStatus(StrEnum):
    REGISTERED = "REGISTERED"
    DISABLED = "DISABLED"
    RETIRED = "RETIRED"

    @property
    def is_usable(self) -> bool:
        return self is StrategyStatus.REGISTERED


class RegimeClass(StrEnum):
    """Coarse market-regime classification produced by a deterministic model."""

    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"
    UNKNOWN = "UNKNOWN"


class ParameterBound(BaseModel):
    """Declared bounds for one tunable strategy parameter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum: Decimal
    maximum: Decimal
    default: Decimal

    @model_validator(mode="after")
    def _bounds(self) -> ParameterBound:
        if self.minimum > self.maximum:
            raise ValidationError("parameter minimum exceeds maximum")
        if not (self.minimum <= self.default <= self.maximum):
            raise ValidationError("parameter default must lie within its declared bounds")
        return self

    def accepts(self, value: Decimal) -> bool:
        return self.minimum <= value <= self.maximum


class StrategyDefinition(BaseModel):
    """A registered, versioned strategy.

    A strategy must have deterministic inputs, parameters, rebalance cadence,
    eligibility rules and tests before registration (spec s3.2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    version: str
    name: str
    description: str
    status: StrategyStatus = StrategyStatus.REGISTERED

    rebalance_cadence_days: int = Field(default=20, ge=1, le=252)
    requires_min_history_days: int = Field(default=250, ge=0)
    max_horizon_days: int = Field(default=120, ge=1, le=3650)

    parameter_bounds: dict[str, ParameterBound] = Field(default_factory=dict)
    eligible_instrument_types: tuple[InstrumentType, ...] = (
        InstrumentType.A_SHARE,
        InstrumentType.DOMESTIC_EQUITY_ETF,
    )

    registered_at: datetime | None = None
    source_refs: tuple[str, ...] = ()
    is_benchmark: bool = False
    """A benchmark baseline is comparable but not an active strategy."""

    @field_validator("strategy_id")
    @classmethod
    def _strategy_id(cls, value: str) -> str:
        if not value or not value.replace("_", "").isalnum():
            raise ValidationError("strategy_id must be a snake_case identifier")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) < 3 or not all(p.isdigit() for p in parts):
            raise ValidationError("strategy version must be semver-like, e.g. 0.1.0")
        return value

    @field_validator("registered_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_aware(value, "registered_at")

    def validate_parameters(self, parameters: dict[str, Decimal]) -> None:
        """Reject undeclared or out-of-bounds parameter overrides."""
        unknown = set(parameters) - set(self.parameter_bounds)
        if unknown:
            raise UnsupportedEntityError(
                "strategy received undeclared parameters", unknown=sorted(unknown)
            )
        for name, value in parameters.items():
            bound = self.parameter_bounds[name]
            if not bound.accepts(value):
                raise ValidationError(
                    "strategy parameter outside declared bounds",
                    parameter=name,
                    value=str(value),
                    minimum=str(bound.minimum),
                    maximum=str(bound.maximum),
                )


class StrategyRegistry:
    """In-memory, explicit registry of approved strategies.

    The registry is *closed*: an unknown ``(strategy_id, version)`` pair is a
    rejection, never a fallback (Phase 0 spec s3.1: the agent may not invent a
    strategy).
    """

    __slots__ = ("_strategies",)

    def __init__(self, strategies: tuple[StrategyDefinition, ...] = ()) -> None:
        self._strategies: dict[tuple[str, str], StrategyDefinition] = {}
        for strategy in strategies:
            self.register(strategy)

    def register(self, strategy: StrategyDefinition) -> StrategyRegistry:
        key = (strategy.strategy_id, strategy.version)
        if key in self._strategies:
            raise ValidationError(
                "strategy version already registered",
                strategy_id=strategy.strategy_id,
                version=strategy.version,
            )
        self._strategies[key] = strategy
        return self

    def get(self, strategy_id: str, version: str) -> StrategyDefinition:
        try:
            return self._strategies[(strategy_id, version)]
        except KeyError as exc:
            raise NotFoundError(
                "unknown strategy or version",
                strategy_id=strategy_id,
                version=version,
                known=[f"{k[0]}@{k[1]}" for k in self._strategies],
            ) from exc

    def get_usable(self, strategy_id: str, version: str) -> StrategyDefinition:
        strategy = self.get(strategy_id, version)
        if not strategy.status.is_usable:
            raise UnsupportedEntityError(
                "strategy is not usable in its current status",
                strategy_id=strategy_id,
                version=version,
                status=strategy.status.value,
            )
        return strategy

    def versions_of(self, strategy_id: str) -> tuple[str, ...]:
        return tuple(sorted(v for (sid, v) in self._strategies if sid == strategy_id))

    def all(self) -> tuple[StrategyDefinition, ...]:
        return tuple(self._strategies.values())

    def __len__(self) -> int:
        return len(self._strategies)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, tuple) and key in self._strategies
