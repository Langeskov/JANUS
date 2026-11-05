"""Agent boundary package (Phase 0 spec s9, s11 P0-G).

Everything an agent may produce enters the system through
:func:`janus.agents.schemas.parse_proposal_payload`, which returns a domain
``AllocationProposal`` or raises ``SecurityViolationError``. Agents never receive
execution, risk-approval, ledger, policy or credential capabilities.
"""

from __future__ import annotations

from janus.agents.adapters import (
    DISABLED_LLM_REASON,
    FORBIDDEN_AGENT_CAPABILITIES,
    DisabledLLMAdapter,
    FixtureAgent,
    QuotedExternalContent,
    ReadOnlyMarketData,
    ReadOnlyResearchAgent,
    ResearchDigest,
    load_fixture_payload,
    load_providers_config_file,
)
from janus.agents.schemas import (
    ALLOWED_PROPOSAL_FIELDS,
    FORBIDDEN_PROPOSAL_FIELDS,
    MAX_PROPOSAL_PAYLOAD_BYTES,
    AllocationProposalPayload,
    TargetWeightPayload,
    parse_proposal_payload,
)

__all__ = [
    "ALLOWED_PROPOSAL_FIELDS",
    "DISABLED_LLM_REASON",
    "FORBIDDEN_AGENT_CAPABILITIES",
    "FORBIDDEN_PROPOSAL_FIELDS",
    "MAX_PROPOSAL_PAYLOAD_BYTES",
    "AllocationProposalPayload",
    "DisabledLLMAdapter",
    "FixtureAgent",
    "QuotedExternalContent",
    "ReadOnlyMarketData",
    "ReadOnlyResearchAgent",
    "ResearchDigest",
    "TargetWeightPayload",
    "load_fixture_payload",
    "load_providers_config_file",
    "parse_proposal_payload",
]
