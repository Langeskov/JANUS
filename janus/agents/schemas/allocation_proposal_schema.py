"""Strict JSON boundary for the agent's only output: an ``AllocationProposal``.

Phase 0 spec s3.3, s9.3 and s9.9: an agent (or an LLM behind one) may emit a
*validated allocation proposal*, never an executable broker instruction, and
never a policy/credential/tool directive. This module is the schema boundary:

* the payload may be a mapping or JSON text;
* it is size-limited in bytes and nesting depth;
* unknown keys are rejected (``extra="forbid"``);
* a recursive key scan rejects tool/policy/capital/credential fields with a
  :class:`~janus.domain.common.errors.SecurityViolationError`;
* the accepted shape is then mapped onto the domain
  :class:`~janus.domain.strategy.proposal.AllocationProposal`, whose own
  validators enforce duplicate symbols, weight range and weight-sum tolerance.

Anything that fails here produces **no order** (spec s9.9); semantic validation
against the strategy registry / data snapshots happens later in
:class:`~janus.application.services.proposal_service.ProposalService`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)

from janus.domain.common.errors import JanusError, SecurityViolationError
from janus.domain.common.time import ensure_aware
from janus.domain.instruments.symbol import validate_symbol
from janus.domain.portfolio.models import TargetWeight
from janus.domain.strategy.models import RegimeClass
from janus.domain.strategy.proposal import (
    MAX_EVIDENCE_REFS,
    MAX_HORIZON_DAYS,
    MAX_ITEM_CHARS,
    MAX_LIST_ITEMS,
    MAX_TARGET_WEIGHTS,
    MAX_THESIS_CHARS,
    AllocationProposal,
)

__all__ = [
    "ALLOWED_PROPOSAL_FIELDS",
    "FORBIDDEN_PROPOSAL_FIELDS",
    "MAX_PROPOSAL_PAYLOAD_BYTES",
    "AllocationProposalPayload",
    "TargetWeightPayload",
    "parse_proposal_payload",
]

MAX_PROPOSAL_PAYLOAD_BYTES: Final[int] = 32 * 1024
"""Maximum accepted proposal size (32 KiB). The spec's example is under 1 KiB."""

_MAX_PAYLOAD_DEPTH: Final[int] = 8
"""Maximum accepted JSON nesting depth (the real payload nests at most 3 deep)."""

_STRATEGY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STRATEGY_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")

FORBIDDEN_PROPOSAL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "tool",
        "tool_call",
        "tool_calls",
        "tools",
        "function_call",
        "policy",
        "risk_policy",
        "risk_limits",
        "capital",
        "add_capital",
        "deposit",
        "withdraw",
        "top_up",
        "ledger",
        "cash_ledger",
        "execute_order",
        "place_order",
        "submit_order",
        "broker_api",
        "approve_risk",
        "override_risk",
        "bypass_risk",
        "api_key",
        "secret_key",
        "credentials",
        ".env",
        "system_prompt",
        "live_trading",
    }
)
"""Explicitly named fields an agent may never place in a proposal.

Field *names* are additionally screened by :data:`_FORBIDDEN_KEY_TOKENS`, so a
creative variant such as ``"risk_policy_override"`` or ``"tool_calls"`` is
rejected as a security violation rather than a mere schema error.
"""

_FORBIDDEN_KEY_TOKENS: Final[tuple[str, ...]] = (
    "tool",
    "policy",
    "risk",
    "capital",
    "deposit",
    "withdraw",
    "ledger",
    "execute",
    "broker",
    "credential",
    "secret",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "env",
    "shell",
    "sql",
    "command",
    "script",
    "approve",
    "override",
    "bypass",
    "admin",
    "leverage",
    "short",
    "mutate",
    "permission",
)


def _json_default(value: object) -> str:
    """JSON fallback that keeps Decimal/datetime exact and rejects everything else."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"proposal payload contains a non-JSON value of type {type(value).__name__}")


def _reject_json_constant(token: str) -> object:
    raise ValueError(f"non-finite JSON constant {token!r} is not allowed")


def _plain_json_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Round-trip through JSON so only plain JSON types reach the schema."""
    try:
        serialised = json.dumps(dict(payload.items()), default=_json_default, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SecurityViolationError(
            "proposal payload is not serialisable as JSON", error=str(exc)
        ) from exc
    if len(serialised.encode("utf-8")) > MAX_PROPOSAL_PAYLOAD_BYTES:
        raise SecurityViolationError(
            "proposal payload exceeds the maximum accepted size",
            limit_bytes=MAX_PROPOSAL_PAYLOAD_BYTES,
        )
    parsed = json.loads(serialised)
    if not isinstance(parsed, dict):  # pragma: no cover - defensive
        raise SecurityViolationError("proposal payload must be a JSON object")
    return parsed


def _decode_json_text(payload: str) -> dict[str, object]:
    if len(payload.encode("utf-8")) > MAX_PROPOSAL_PAYLOAD_BYTES:
        raise SecurityViolationError(
            "proposal payload exceeds the maximum accepted size",
            limit_bytes=MAX_PROPOSAL_PAYLOAD_BYTES,
        )
    try:
        parsed = json.loads(payload, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise SecurityViolationError("proposal payload is not valid JSON", error=str(exc)) from exc
    if not isinstance(parsed, dict):
        raise SecurityViolationError(
            "proposal payload must be a JSON object", got=type(parsed).__name__
        )
    return parsed


def _scan_payload(value: object, *, path: str = "$", depth: int = 0) -> None:
    """Reject forbidden field names and excessive nesting anywhere in the payload."""
    if depth > _MAX_PAYLOAD_DEPTH:
        raise SecurityViolationError(
            "proposal payload nests too deeply", path=path, max_depth=_MAX_PAYLOAD_DEPTH
        )
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise SecurityViolationError("proposal payload keys must be strings", path=path)
            key = raw_key.strip()
            if key.lower() in FORBIDDEN_PROPOSAL_FIELDS or any(
                token in key.lower() for token in _FORBIDDEN_KEY_TOKENS
            ):
                raise SecurityViolationError(
                    "proposal payload attempts a forbidden tool/policy/credential field",
                    field=key,
                    path=path,
                )
            _scan_payload(item, path=f"{path}.{key}", depth=depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_payload(item, path=f"{path}[{index}]", depth=depth + 1)


def _validation_details(exc: Exception) -> list[dict[str, str]]:
    if isinstance(exc, PydanticValidationError):
        details: list[dict[str, str]] = []
        for error in exc.errors()[:10]:
            location = ".".join(str(part) for part in error["loc"])
            details.append(
                {
                    "location": location or "-",
                    "type": str(error["type"]),
                    "message": str(error["msg"]),
                }
            )
        return details
    message = getattr(exc, "message", None) or str(exc)
    return [{"location": "-", "type": type(exc).__name__, "message": message}]


class TargetWeightPayload(BaseModel):
    """One target-weight entry, exactly as the model may emit it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    weight: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))

    @field_validator("symbol", mode="before")
    @classmethod
    def _symbol(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("symbol must be a string")
        return validate_symbol(value)

    @field_validator("weight", mode="before")
    @classmethod
    def _weight(cls, value: object) -> Decimal:
        if isinstance(value, bool):
            raise ValueError("weight must be a decimal number, not a bool")
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, str):
            try:
                return Decimal(value.strip())
            except InvalidOperation as exc:
                raise ValueError("weight must be a decimal number") from exc
        raise ValueError("weight must be a decimal number")


class AllocationProposalPayload(BaseModel):
    """Strict, size-limited mirror of the specification's proposal payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    strategy_id: str = Field(min_length=1, max_length=64)
    strategy_version: str = Field(min_length=1, max_length=32)
    regime: RegimeClass = RegimeClass.UNKNOWN
    target_weights: list[TargetWeightPayload] = Field(min_length=1, max_length=MAX_TARGET_WEIGHTS)
    horizon_days: int = Field(ge=1, le=MAX_HORIZON_DAYS)
    thesis: str = Field(default="", max_length=MAX_THESIS_CHARS)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    evidence_refs: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_REFS)
    uncertainties: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    data_snapshot_id: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)
    prompt_version: str | None = Field(default=None, max_length=64)

    @field_validator("as_of")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "as_of")

    @field_validator("strategy_id")
    @classmethod
    def _strategy_id(cls, value: str) -> str:
        if _STRATEGY_ID_PATTERN.match(value) is None:
            raise ValueError("strategy_id must be a snake_case identifier")
        return value

    @field_validator("strategy_version")
    @classmethod
    def _strategy_version(cls, value: str) -> str:
        if _STRATEGY_VERSION_PATTERN.match(value) is None:
            raise ValueError("strategy_version must be semver-like, e.g. 0.1.0")
        return value

    @field_validator("regime", mode="before")
    @classmethod
    def _regime(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("invalidation_conditions", "evidence_refs", "uncertainties")
    @classmethod
    def _text_items(cls, value: list[str]) -> list[str]:
        for item in value:
            if len(item) > MAX_ITEM_CHARS:
                raise ValueError("list item exceeds the maximum allowed length")
        return value

    @model_validator(mode="after")
    def _duplicate_symbols(self) -> AllocationProposalPayload:
        symbols = [entry.symbol for entry in self.target_weights]
        if len(symbols) != len(set(symbols)):
            raise ValueError("duplicate symbol in target_weights")
        return self


ALLOWED_PROPOSAL_FIELDS: Final[frozenset[str]] = frozenset(AllocationProposalPayload.model_fields)
"""The complete, closed set of fields an agent may emit."""


def parse_proposal_payload(payload: Mapping[str, object] | str) -> AllocationProposal:
    """Validate an agent-emitted payload and return a domain ``AllocationProposal``.

    Parameters
    ----------
    payload:
        Either a mapping (fixtures, tests, already-parsed JSON) or raw JSON text.

    Raises
    ------
    SecurityViolationError
        For malformed JSON, oversized payloads, excessive nesting, unknown keys,
        tool/policy/capital/credential fields, or values that violate the strict
        schema or the domain proposal invariants.

    This function is the *only* supported way for agent output to become a
    domain proposal; it never returns a partially validated object.
    """
    if isinstance(payload, str):
        raw: dict[str, object] = _decode_json_text(payload)
    elif isinstance(payload, Mapping):
        raw = _plain_json_payload(payload)
    else:
        raise SecurityViolationError(
            "proposal payload must be a mapping or a JSON object string",
            got=type(payload).__name__,
        )

    _scan_payload(raw)

    try:
        boundary = AllocationProposalPayload.model_validate(raw)
    except (PydanticValidationError, JanusError) as exc:
        raise SecurityViolationError(
            "proposal payload failed the strict agent-output schema",
            details=_validation_details(exc),
        ) from exc

    try:
        return AllocationProposal(
            as_of=boundary.as_of,
            strategy_id=boundary.strategy_id,
            strategy_version=boundary.strategy_version,
            regime=boundary.regime,
            target_weights=tuple(
                TargetWeight(symbol=entry.symbol, weight=entry.weight)
                for entry in boundary.target_weights
            ),
            horizon_days=boundary.horizon_days,
            thesis=boundary.thesis,
            invalidation_conditions=tuple(boundary.invalidation_conditions),
            evidence_refs=tuple(boundary.evidence_refs),
            uncertainties=tuple(boundary.uncertainties),
            data_snapshot_id=boundary.data_snapshot_id,
            model_id=boundary.model_id,
            prompt_version=boundary.prompt_version,
        )
    except (PydanticValidationError, JanusError) as exc:
        raise SecurityViolationError(
            "proposal payload is not a valid allocation proposal",
            details=_validation_details(exc),
        ) from exc
