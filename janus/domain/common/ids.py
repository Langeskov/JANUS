"""Deterministic identifier helpers.

Identifiers are opaque, URL/log-safe strings. They are generated with a
time-ordered prefix plus a random suffix so that audit records sort naturally;
they are never derived from user input.
"""

from __future__ import annotations

import re
import secrets
from typing import Final, NewType
from uuid import uuid4

from janus.domain.common.errors import ValidationError

AccountId = NewType("AccountId", str)
OrderId = NewType("OrderId", str)
FillId = NewType("FillId", str)
LedgerEntryId = NewType("LedgerEntryId", str)
DecisionId = NewType("DecisionId", str)
SnapshotId = NewType("SnapshotId", str)
RuleSetId = NewType("RuleSetId", str)
FeeScheduleId = NewType("FeeScheduleId", str)
RiskDecisionId = NewType("RiskDecisionId", str)
StrategyId = NewType("StrategyId", str)

ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

__all__ = [
    "ID_PATTERN",
    "AccountId",
    "DecisionId",
    "FeeScheduleId",
    "FillId",
    "LedgerEntryId",
    "OrderId",
    "RiskDecisionId",
    "RuleSetId",
    "SnapshotId",
    "StrategyId",
    "ensure_id",
    "new_id",
]


def new_id(prefix: str) -> str:
    """Generate a prefixed, unique, log-safe identifier."""
    if not prefix or not prefix.isalnum():
        raise ValidationError(f"id prefix must be alphanumeric, got {prefix!r}")
    token = uuid4().hex[:20]
    checksum = secrets.token_hex(2)
    return f"{prefix}-{token}-{checksum}"


def ensure_id(value: str, field: str = "id") -> str:
    """Validate that a user/config supplied identifier is well formed."""
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise ValidationError(f"{field} is not a well-formed JANUS id: {value!r}", field=field)
    return value
