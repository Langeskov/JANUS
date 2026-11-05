"""Agent adapters: deterministic stub agent, read-only research, disabled LLM."""

from __future__ import annotations

from janus.agents.adapters.disabled_llm_adapter import (
    DISABLED_LLM_REASON,
    DisabledLLMAdapter,
    load_providers_config_file,
)
from janus.agents.adapters.stub_agent import (
    FORBIDDEN_AGENT_CAPABILITIES,
    FixtureAgent,
    QuotedExternalContent,
    ReadOnlyMarketData,
    ReadOnlyResearchAgent,
    ResearchDigest,
    load_fixture_payload,
)

__all__ = [
    "DISABLED_LLM_REASON",
    "FORBIDDEN_AGENT_CAPABILITIES",
    "DisabledLLMAdapter",
    "FixtureAgent",
    "QuotedExternalContent",
    "ReadOnlyMarketData",
    "ReadOnlyResearchAgent",
    "ResearchDigest",
    "load_fixture_payload",
    "load_providers_config_file",
]
