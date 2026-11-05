"""Decision-lineage records (Phase 0 spec s10).

A decision must be replayable from its immutable data snapshot plus versioned
rules. Records therefore capture the *inputs*, the *thesis/evidence* and every
version reference, but never credentials (only provider/model identifiers and,
optionally, the provider's request id).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from janus.domain.common.errors import ValidationError
from janus.domain.common.ids import ensure_id
from janus.domain.common.time import ensure_aware

__all__ = ["DecisionRecord", "DecisionType"]


class DecisionType(StrEnum):
    ALLOCATION_PROPOSAL = "ALLOCATION_PROPOSAL"
    REBALANCE_PLAN = "REBALANCE_PLAN"
    RISK_EVALUATION = "RISK_EVALUATION"
    EXECUTION = "EXECUTION"
    ADMIN_ACTION = "ADMIN_ACTION"
    DATA_INGESTION = "DATA_INGESTION"


_MAX_INPUT_ITEMS = 100
_MAX_INPUT_TEXT = 4000


class DecisionRecord(BaseModel):
    """Immutable lineage for one agent/portfolio/risk decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    decision_type: DecisionType
    created_at: datetime
    as_of: datetime

    account_id: str | None = None
    proposal_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None

    model_id: str | None = None
    prompt_version: str | None = None
    llm_request_id: str | None = None
    """Provider request id. Never an API key or token."""

    data_snapshot_id: str | None = None
    rule_set_id: str | None = None
    rule_set_version: str | None = None
    risk_decision_id: str | None = None

    inputs: dict[str, Any] = Field(default_factory=dict)
    """Structured, size-limited inputs used for the decision."""

    thesis: str = ""
    evidence_refs: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    outcome: str = ""
    executable: bool = False

    @field_validator("decision_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return ensure_id(value, "decision_id")

    @field_validator("created_at", "as_of")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "timestamp")

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > _MAX_INPUT_ITEMS:
            raise ValidationError("decision inputs exceed the maximum item count")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 200:
                raise ValidationError("decision input keys must be short strings")
            if isinstance(item, str) and len(item) > _MAX_INPUT_TEXT:
                raise ValidationError("decision input string value is too long", key=key)
        return value

    @field_validator("thesis")
    @classmethod
    def _thesis(cls, value: str) -> str:
        if len(value) > _MAX_INPUT_TEXT:
            raise ValidationError("decision thesis is too long")
        return value
