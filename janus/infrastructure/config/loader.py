"""Typed configuration loading.

Configuration is data, not code: every limit, rate and identifier lives in
``config/*.yaml`` so it can be reviewed and versioned independently of the
implementation (Phase 0 spec s2.2, s6.3, s13).

All loaders fail closed: a missing file, an unknown key, an unsupported value or
a contradictory setting raises :class:`ConfigurationError` with the file and
field named. Nothing silently falls back to a default that could trade.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from janus.domain.common.enums import Board, InstrumentType
from janus.domain.common.errors import ConfigurationError, UnsupportedEntityError
from janus.domain.common.money import CNY
from janus.domain.instruments.eligibility import UniversePolicy
from janus.domain.risk.models import RiskPolicy
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AccountConfig",
    "AppConfig",
    "LLMConfig",
    "MarketDataConfig",
    "PortfolioConfig",
    "ProvidersConfig",
    "UniverseConfig",
    "default_config_dir",
    "find_repo_root",
    "load_app_config",
]


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repository root (the directory holding ``pyproject.toml``)."""
    if start is not None:
        candidate = Path(start).resolve()
        for parent in (candidate, *candidate.parents):
            if (parent / "pyproject.toml").is_file():
                return parent
        raise ConfigurationError("could not locate repository root", start=str(start))
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file() and (parent / "config").is_dir():
            return parent
    # Installed-wheel fallback: allow the caller's working directory to hold config/.
    cwd = Path.cwd().resolve()
    if (cwd / "config").is_dir():
        return cwd
    raise ConfigurationError(
        "could not locate the JANUS repository root (needs pyproject.toml + config/)"
    )


def default_config_dir() -> Path:
    """Default ``config/`` directory for the current checkout."""
    return find_repo_root() / "config"


def _read_yaml(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.is_file():
        if not required:
            return {}
        raise ConfigurationError("configuration file is missing", file=str(path))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            "configuration file is not valid YAML", file=str(path), error=str(exc)
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be a mapping", file=str(path))
    return raw


def _enum_from_name[EnumT: StrEnum](
    enum_cls: type[EnumT], name: str, *, file: str, field: str
) -> EnumT:
    """Resolve an enum member by name, failing closed with file context."""
    member = getattr(enum_cls, name, None)
    if not isinstance(member, enum_cls):
        raise ConfigurationError(
            f"unknown {field} entry {name!r}",
            file=file,
            field=field,
            allowed=[item.value for item in enum_cls],
        )
    return member


class AccountConfig(BaseModel):
    """The fixed-capital paper account definition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = "janus-paper-cny-001"
    mode: str = "paper"
    currency: str = CNY
    initial_capital: Decimal = Field(default=Decimal("100000"), gt=0)
    initial_cash_ratio: Decimal = Decimal("1.0")
    allow_external_flows: bool = False
    reset_requires_admin: bool = True

    @field_validator("mode")
    @classmethod
    def _paper_only(cls, value: str) -> str:
        if value.lower() != "paper":
            raise UnsupportedEntityError(
                "Phase 0 configuration supports paper accounts only", mode=value
            )
        return "paper"

    @field_validator("currency")
    @classmethod
    def _cny(cls, value: str) -> str:
        if value.upper() != CNY:
            raise UnsupportedEntityError("Phase 0 supports a CNY cash account only", currency=value)
        return CNY

    @field_validator("initial_cash_ratio")
    @classmethod
    def _full_cash(cls, value: Decimal) -> Decimal:
        if value != Decimal("1.0"):
            raise UnsupportedEntityError(
                "Phase 0 accounts must start 100% in cash", initial_cash_ratio=str(value)
            )
        return value

    @model_validator(mode="after")
    def _no_external_flows(self) -> AccountConfig:
        if self.allow_external_flows:
            raise UnsupportedEntityError("external deposits/withdrawals are forbidden in Phase 0")
        if not self.reset_requires_admin:
            raise UnsupportedEntityError("account reset must require an administrative action")
        return self


class PortfolioConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account: AccountConfig = AccountConfig()


class UniverseConfig(BaseModel):
    """Raw universe configuration plus derivation of the domain policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exchanges: tuple[str, ...] = ("SSE", "SZSE")
    boards: tuple[str, ...] = ("SSE_MAIN", "SZSE_MAIN")
    include: tuple[str, ...] = ("A_SHARE", "DOMESTIC_EQUITY_ETF")
    exclude: tuple[str, ...] = ()
    exclude_st: bool = True
    exclude_suspended_for_new_orders: bool = True
    exclude_delisted: bool = True
    min_listing_trading_days_for_buy: int = Field(default=60, ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> UniverseConfig:
        include_types = set(self.include)
        known_types = set(InstrumentType.__members__)
        unknown = include_types - known_types
        if unknown:
            raise ConfigurationError(
                "universe include list has unknown instrument types", unknown=sorted(unknown)
            )
        for board_name in self.boards:
            if board_name not in Board.__members__:
                raise ConfigurationError(
                    "universe board list has an unknown board", board=board_name
                )
        # ``exclude`` mixes board names and product families (see spec s13); the
        # authoritative allow-lists above must not contradict it.
        for entry in self.exclude:
            if entry in include_types:
                raise ConfigurationError(
                    "universe config both includes and excludes the same entry", entry=entry
                )
            if entry in self.boards:
                raise ConfigurationError(
                    "universe config allows and excludes the same board", entry=entry
                )
        return self

    def boards_allowed(self) -> tuple[Board, ...]:
        return tuple(
            _enum_from_name(Board, name, file="universe.yaml", field="boards")
            for name in self.boards
        )

    def instrument_types_allowed(self) -> tuple[InstrumentType, ...]:
        return tuple(
            _enum_from_name(InstrumentType, name, file="universe.yaml", field="include")
            for name in self.include
        )

    def to_policy(self) -> UniversePolicy:
        return UniversePolicy(
            exchanges=self.exchanges,
            boards_allowed=self.boards_allowed(),
            instrument_types_allowed=self.instrument_types_allowed(),
            exclude_st=self.exclude_st,
            exclude_suspended_for_new_orders=self.exclude_suspended_for_new_orders,
            exclude_delisted=self.exclude_delisted,
            min_listing_trading_days_for_buy=self.min_listing_trading_days_for_buy,
        )


class MarketDataConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: str = "fixture"
    enabled: tuple[str, ...] = ("fixture",)
    fixture_dir: str = "data/fixtures"
    provider_settings: Mapping[str, Mapping[str, Any]] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    provider: str = "deepseek_compatible"
    api_key_env: str = "DEEPSEEK_API_KEY"
    execution_tools_enabled: bool = False
    model_id: str = "deepseek-chat"
    prompt_version: str = "research_agent_v0_1_0"
    note: str = ""
    """Free-form documentation only; never contains a secret."""

    @model_validator(mode="after")
    def _no_execution_tools(self) -> LLMConfig:
        if self.execution_tools_enabled:
            raise UnsupportedEntityError("Phase 0 forbids granting execution tools to a model")
        return self


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    market_data: MarketDataConfig = MarketDataConfig()
    llm: LLMConfig = LLMConfig()
    note: str = ""
    """Free-form documentation at the file level."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Fully validated application configuration."""

    config_dir: Path
    repo_root: Path
    portfolio: PortfolioConfig
    universe: UniverseConfig
    risk: RiskPolicy
    providers: ProvidersConfig

    @property
    def account(self) -> AccountConfig:
        return self.portfolio.account

    def universe_policy(self) -> UniversePolicy:
        return self.universe.to_policy()

    def fixture_dir(self) -> Path:
        candidate = Path(self.providers.market_data.fixture_dir)
        if candidate.is_absolute():
            return candidate
        return (self.repo_root / candidate).resolve()


def _load_portfolio(path: Path) -> PortfolioConfig:
    raw = _read_yaml(path)
    account_raw = raw.get("account", {})
    if not isinstance(account_raw, dict):
        raise ConfigurationError("portfolio.account must be a mapping", file=str(path))
    try:
        return PortfolioConfig.model_validate({"account": account_raw})
    except Exception as exc:
        raise ConfigurationError(
            "invalid portfolio configuration", file=str(path), error=str(exc)
        ) from exc


def _load_universe(path: Path) -> UniverseConfig:
    raw = _read_yaml(path)
    market = raw.get("market", {})
    instruments = raw.get("instruments", {})
    eligibility = raw.get("eligibility", {})
    if not all(isinstance(section, dict) for section in (market, instruments, eligibility)):
        raise ConfigurationError("universe sections must be mappings", file=str(path))
    payload = {
        "exchanges": tuple(market.get("exchanges", ())),
        "boards": tuple(market.get("boards", ())),
        "include": tuple(instruments.get("include", ())),
        "exclude": tuple(instruments.get("exclude", ())),
        **eligibility,
    }
    try:
        return UniverseConfig.model_validate(payload)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            "invalid universe configuration", file=str(path), error=str(exc)
        ) from exc


def _load_risk(path: Path) -> RiskPolicy:
    raw = _read_yaml(path)
    try:
        return RiskPolicy.model_validate(raw)
    except UnsupportedEntityError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            "invalid risk policy configuration", file=str(path), error=str(exc)
        ) from exc


def _load_providers(path: Path) -> ProvidersConfig:
    raw = _read_yaml(path)
    market_data_raw = raw.get("market_data", {})
    llm_raw = raw.get("llm", {})
    if not isinstance(market_data_raw, dict) or not isinstance(llm_raw, dict):
        raise ConfigurationError("provider sections must be mappings", file=str(path))
    providers_section = market_data_raw.get("providers", {})
    if not isinstance(providers_section, dict):
        raise ConfigurationError("market_data.providers must be a mapping", file=str(path))
    payload = {
        "market_data": {
            "primary": market_data_raw.get("primary", "fixture"),
            "enabled": tuple(market_data_raw.get("enabled", ("fixture",))),
            "fixture_dir": providers_section.get("fixture", {}).get("fixture_dir", "data/fixtures"),
            "provider_settings": providers_section,
        },
        "llm": llm_raw,
    }
    try:
        config = ProvidersConfig.model_validate(payload)
    except UnsupportedEntityError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            "invalid provider configuration", file=str(path), error=str(exc)
        ) from exc
    if config.market_data.primary not in ("fixture",):
        raise UnsupportedEntityError(
            "Phase 0 only enables the deterministic fixture market-data provider",
            primary=config.market_data.primary,
        )
    return config


def load_app_config(config_dir: Path | str | None = None) -> AppConfig:
    """Load and validate every configuration file in one place."""
    if config_dir is None:
        directory = default_config_dir()
        root = find_repo_root()
    else:
        directory = Path(config_dir).resolve()
        if not directory.is_dir():
            raise ConfigurationError("config directory does not exist", path=str(directory))
        try:
            root = find_repo_root(directory)
        except ConfigurationError:
            root = directory.parent
    return AppConfig(
        config_dir=directory,
        repo_root=root,
        portfolio=_load_portfolio(directory / "portfolio.yaml"),
        universe=_load_universe(directory / "universe.yaml"),
        risk=_load_risk(directory / "risk_policy.yaml"),
        providers=_load_providers(directory / "providers.yaml"),
    )
