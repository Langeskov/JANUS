"""Adversarial tests for the agent permission boundary (Phase 0 spec s8.3, s9).

Proves that an ``AllocationProposal`` (or any agent object) cannot:

* change the risk policy or risk limits;
* add capital / deposit / withdraw / edit a balance;
* mutate the ledger or fills;
* read credentials, tokens or ``.env`` secrets;
* approve its own risk;
* invoke an execution tool or submit an order.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from janus.agents import (
    ALLOWED_PROPOSAL_FIELDS,
    FORBIDDEN_AGENT_CAPABILITIES,
    FORBIDDEN_PROPOSAL_FIELDS,
    DisabledLLMAdapter,
    FixtureAgent,
    ReadOnlyResearchAgent,
    load_fixture_payload,
    parse_proposal_payload,
)
from janus.domain.common.enums import AdjustmentMode, DataQualityStatus
from janus.domain.common.errors import (
    ConfigurationError,
    SecurityViolationError,
    UnsupportedEntityError,
)
from janus.domain.common.time import MARKET_TZ, PointInTime
from janus.domain.market_data.models import MarketBar
from janus.domain.strategy.proposal import AllocationProposal
from pydantic import ValidationError as PydanticValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "data" / "fixtures" / "proposals"
PROVIDERS_YAML = REPO_ROOT / "config" / "providers.yaml"
SYMBOL = "510300.SH"
NOW = datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ)
T_DATE = date(2026, 11, 5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _valid_payload(**overrides: object) -> dict[str, object]:
    payload = load_fixture_payload(FIXTURES / "broad_etf_core_v0_1_0.json")
    payload.update(overrides)
    return payload


def _bar() -> MarketBar:
    event = datetime.combine(T_DATE, time(15, 0), tzinfo=MARKET_TZ)
    close = Decimal("4.05")
    return MarketBar(
        symbol=SYMBOL,
        trade_date=T_DATE,
        open=close - Decimal("0.01"),
        high=close + Decimal("0.02"),
        low=close - Decimal("0.02"),
        close=close,
        volume=5_000_000,
        adjustment=AdjustmentMode.UNADJUSTED,
        quality=DataQualityStatus.OK,
        point_in_time=PointInTime(
            event_time=event,
            published_at=event,
            retrieved_at=event + timedelta(minutes=5),
            known_at=event + timedelta(minutes=5),
        ),
    )


class _ReadOnlySource:
    def __init__(self, bar: MarketBar) -> None:
        self._bar = bar

    def get_daily_bar(self, symbol: str, trade_date: date) -> MarketBar | None:
        if symbol == self._bar.symbol and trade_date == self._bar.trade_date:
            return self._bar
        return None

    def get_daily_bars(self, symbol: str, start: date, end: date) -> tuple[MarketBar, ...]:
        return (self._bar,)


class _WriteCapableSource(_ReadOnlySource):
    def place_order(self, *args: object, **kwargs: object) -> str:  # pragma: no cover
        raise AssertionError("the agent must never be able to call place_order")

    def append_fill(self, *args: object, **kwargs: object) -> str:  # pragma: no cover
        raise AssertionError("the agent must never be able to append fills")


def _read_only_agent() -> ReadOnlyResearchAgent:
    return ReadOnlyResearchAgent(_ReadOnlySource(_bar()))


# ---------------------------------------------------------------------------
# Capability surface
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("agent_type", [FixtureAgent, ReadOnlyResearchAgent, DisabledLLMAdapter])
def test_agent_classes_expose_no_forbidden_capabilities(agent_type: type) -> None:
    for capability in FORBIDDEN_AGENT_CAPABILITIES:
        assert not hasattr(agent_type, capability), (
            f"{agent_type.__name__} must not expose {capability!r}"
        )


def test_agent_instances_expose_no_forbidden_capabilities() -> None:
    instances = (
        FixtureAgent(FIXTURES / "broad_etf_core_v0_1_0.json"),
        _read_only_agent(),
        DisabledLLMAdapter(),
    )
    for instance in instances:
        for capability in FORBIDDEN_AGENT_CAPABILITIES:
            assert not hasattr(instance, capability), (
                f"{type(instance).__name__} instance must not expose {capability!r}"
            )


def test_read_only_agent_refuses_a_write_capable_data_source() -> None:
    with pytest.raises(SecurityViolationError):
        ReadOnlyResearchAgent(_WriteCapableSource(_bar()))


def test_read_only_agent_can_read_but_exposes_no_mutation_methods() -> None:
    agent = _read_only_agent()
    assert agent.read_bar(SYMBOL, T_DATE) is not None
    assert len(agent.read_bars(SYMBOL, T_DATE, T_DATE)) == 1
    for capability in ("place_order", "append_fill", "write_ledger", "approve_risk"):
        assert not hasattr(agent, capability)


def test_research_digest_has_no_execution_authority() -> None:
    digest = _read_only_agent().digest(symbol=SYMBOL, trade_date=T_DATE)
    assert digest.has_execution_authority is False
    for attribute in ("order", "order_intent", "risk_decision", "paper_order"):
        assert not hasattr(digest, attribute)


def test_agents_package_re_exports_no_order_or_risk_types() -> None:
    import janus.agents as agents_module

    exported = set(agents_module.__all__)
    for forbidden in (
        "PaperOrder",
        "ExecutionFill",
        "OrderIntent",
        "OrderStatus",
        "RiskController",
        "RiskDecision",
        "CashLedgerEntry",
    ):
        assert forbidden not in exported


# ---------------------------------------------------------------------------
# A proposal cannot smuggle privileged fields
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field_name",
    ["risk_policy", "min_cash_weight", "risk_limits", "policy", "risk_override"],
)
def test_proposal_cannot_change_risk_policy(field_name: str) -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_valid_payload(**{field_name: 0}))


@pytest.mark.parametrize(
    "field_name",
    ["capital", "add_capital", "deposit", "withdraw", "top_up", "initial_capital"],
)
def test_proposal_cannot_add_capital(field_name: str) -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_valid_payload(**{field_name: 100_000}))


@pytest.mark.parametrize(
    "field_name",
    ["ledger", "cash_ledger", "write_ledger", "fills", "append_fill", "balance"],
)
def test_proposal_cannot_mutate_the_ledger(field_name: str) -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_valid_payload(**{field_name: []}))


@pytest.mark.parametrize(
    "field_name",
    ["api_key", "secret_key", "credentials", ".env", "tushare_token", "env"],
)
def test_proposal_cannot_access_credentials(field_name: str) -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_valid_payload(**{field_name: "redacted"}))


@pytest.mark.parametrize(
    "field_name",
    ["approve_risk", "override_risk", "bypass_risk", "risk_decision", "risk_approval"],
)
def test_proposal_cannot_approve_its_own_risk(field_name: str) -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_valid_payload(**{field_name: "APPROVED"}))


@pytest.mark.parametrize(
    "field_name",
    ["tool_calls", "tools", "execute_order", "place_order", "broker_api", "system_prompt"],
)
def test_proposal_cannot_invoke_an_execution_tool(field_name: str) -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_valid_payload(**{field_name: []}))


def test_nested_privileged_field_is_rejected() -> None:
    weights = [{"symbol": SYMBOL, "weight": 0.2, "tool": "place_order"}]
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_valid_payload(target_weights=weights))


def test_allowed_fields_are_disjoint_from_privileged_fields() -> None:
    assert ALLOWED_PROPOSAL_FIELDS.isdisjoint(FORBIDDEN_PROPOSAL_FIELDS)
    for privileged in (
        "decision_id",
        "risk_decision_id",
        "order_id",
        "tool_calls",
        "risk_policy",
        "api_key",
    ):
        assert privileged not in ALLOWED_PROPOSAL_FIELDS


def test_parsed_proposal_model_is_closed_to_extra_fields() -> None:
    assert AllocationProposal.model_config["extra"] == "forbid"
    proposal = parse_proposal_payload(_valid_payload())
    assert not hasattr(proposal, "risk_limits")
    assert not hasattr(proposal, "order_id")
    with pytest.raises(PydanticValidationError):
        AllocationProposal.model_validate(
            {**proposal.model_dump(), "risk_policy": {"min_cash_weight": 0}}
        )


# ---------------------------------------------------------------------------
# Disabled live-LLM adapter
# ---------------------------------------------------------------------------
def test_disabled_llm_adapter_never_calls_a_model() -> None:
    adapter = DisabledLLMAdapter()
    assert adapter.enabled is False
    assert adapter.provider_id == "deepseek_compatible"
    with pytest.raises(UnsupportedEntityError):
        adapter.complete(system_prompt="system", user_content="news")


def test_disabled_llm_adapter_loads_the_committed_providers_config() -> None:
    adapter = DisabledLLMAdapter.from_config_file(PROVIDERS_YAML)
    assert adapter.enabled is False
    with pytest.raises(UnsupportedEntityError):
        adapter.complete(system_prompt="system", user_content="news")
    for secret_attribute in ("api_key", "token", "secret", "credentials"):
        assert not hasattr(adapter, secret_attribute)


def test_disabled_llm_adapter_refuses_an_enabled_config(tmp_path: Path) -> None:
    config = tmp_path / "providers.yaml"
    config.write_text("llm:\n  enabled: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        DisabledLLMAdapter.from_config_file(config)


def test_disabled_llm_adapter_refuses_execution_tools() -> None:
    with pytest.raises(UnsupportedEntityError):
        DisabledLLMAdapter(execution_tools_enabled=True)
