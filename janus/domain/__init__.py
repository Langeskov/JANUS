"""Domain layer: framework-free business concepts and invariants.

Nothing in this package may import an infrastructure, UI, LLM, broker or data
provider SDK. Only the Python standard library and ``pydantic`` (a general
validation library) are permitted.
"""

from __future__ import annotations

from janus.domain.common.errors import (
    AccountPausedError,
    ConfigurationError,
    DataQualityError,
    DomainError,
    ErrorCode,
    IdempotencyConflictError,
    InvalidTransitionError,
    InvariantViolation,
    JanusError,
    NotFoundError,
    RiskRejection,
    SecurityViolationError,
    StaleDataError,
    UnsupportedEntityError,
    ValidationError,
)

__all__ = [
    "AccountPausedError",
    "ConfigurationError",
    "DataQualityError",
    "DomainError",
    "ErrorCode",
    "IdempotencyConflictError",
    "InvalidTransitionError",
    "InvariantViolation",
    "JanusError",
    "NotFoundError",
    "RiskRejection",
    "SecurityViolationError",
    "StaleDataError",
    "UnsupportedEntityError",
    "ValidationError",
]
