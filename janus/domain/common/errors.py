"""Typed, fail-closed error hierarchy.

Every domain failure is expressed as a :class:`JanusError` subclass carrying a
stable machine-readable :class:`ErrorCode`. Callers must treat unknown or
unsupported situations as *rejections*, never as guesses -- Phase 0 spec s2.2
("Unsupported or incomplete rules must reject the order rather than guess") and
s11 P0-D.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, NoReturn


class ErrorCode(StrEnum):
    """Stable, machine-readable failure categories (safe to persist/log)."""

    VALIDATION = "VALIDATION"
    INVARIANT = "INVARIANT"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"
    DATA_QUALITY = "DATA_QUALITY"
    STALE_DATA = "STALE_DATA"
    RISK_REJECTED = "RISK_REJECTED"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    SECURITY = "SECURITY"
    CONFIGURATION = "CONFIGURATION"
    ACCOUNT_PAUSED = "ACCOUNT_PAUSED"
    INTERNAL = "INTERNAL"


class JanusError(Exception):
    """Base class for every JANUS-controlled failure."""

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for audit records. Must never contain secrets."""
        return {
            "code": self.code.value,
            "error_type": type(self).__name__,
            "message": self.message,
            "details": self.details,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code.value!r}, message={self.message!r})"


class DomainError(JanusError):
    """Base class for business-rule failures."""

    code = ErrorCode.INVARIANT


class ValidationError(DomainError):
    """Input failed a schema or value-object validation rule."""

    code = ErrorCode.VALIDATION


class InvariantViolation(DomainError):
    """An accounting/domain invariant was about to be violated."""

    code = ErrorCode.INVARIANT


class NotFoundError(DomainError):
    """A referenced entity does not exist."""

    code = ErrorCode.NOT_FOUND


class UnsupportedEntityError(DomainError):
    """The system cannot represent the requested entity/rule combination.

    Phase 0 fails closed: an unsupported board, instrument type, session or
    rule-set is a rejection, never a silent default.
    """

    code = ErrorCode.UNSUPPORTED


class DataQualityError(DomainError):
    """Market data failed a quality check."""

    code = ErrorCode.DATA_QUALITY


class StaleDataError(DataQualityError):
    """Data is older than the configured freshness bound."""

    code = ErrorCode.STALE_DATA


class RiskRejection(DomainError):
    """The deterministic risk controller refused the order intent."""

    code = ErrorCode.RISK_REJECTED

    def __init__(
        self,
        message: str,
        *,
        rule_ids: tuple[str, ...] = (),
        **details: Any,
    ) -> None:
        super().__init__(message, rule_ids=list(rule_ids), **details)
        self.rule_ids = rule_ids


class InvalidTransitionError(DomainError):
    """A state machine was asked to perform a forbidden transition."""

    code = ErrorCode.INVALID_TRANSITION


class IdempotencyConflictError(DomainError):
    """The same idempotency key was replayed with different content."""

    code = ErrorCode.IDEMPOTENCY_CONFLICT


class SecurityViolationError(JanusError):
    """An agent/permission boundary was crossed."""

    code = ErrorCode.SECURITY


class ConfigurationError(JanusError):
    """Configuration is missing, contradictory, or unsupported."""

    code = ErrorCode.CONFIGURATION


class AccountPausedError(DomainError):
    """The account is paused (kill switch) and refuses new orders."""

    code = ErrorCode.ACCOUNT_PAUSED


def fail_closed(code: ErrorCode, message: str, **details: Any) -> NoReturn:
    """Raise the generic fail-closed error. Used where no richer type fits."""
    raise JanusError(message, code=code, **details)
