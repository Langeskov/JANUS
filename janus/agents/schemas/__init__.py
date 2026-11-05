"""Strict agent-output schemas and the only safe parsing entry point."""

from __future__ import annotations

from janus.agents.schemas.allocation_proposal_schema import (
    ALLOWED_PROPOSAL_FIELDS,
    FORBIDDEN_PROPOSAL_FIELDS,
    MAX_PROPOSAL_PAYLOAD_BYTES,
    AllocationProposalPayload,
    TargetWeightPayload,
    parse_proposal_payload,
)

__all__ = [
    "ALLOWED_PROPOSAL_FIELDS",
    "FORBIDDEN_PROPOSAL_FIELDS",
    "MAX_PROPOSAL_PAYLOAD_BYTES",
    "AllocationProposalPayload",
    "TargetWeightPayload",
    "parse_proposal_payload",
]
