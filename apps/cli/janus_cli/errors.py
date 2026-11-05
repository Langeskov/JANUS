"""Map controlled JANUS failures to the CLI's stable exit codes.

The CLI is a transport: it must never leak a traceback for an expected domain
failure (rejection, validation, paused account, unsupported entity, ...). This
module classifies an exception *without* importing anything from
``janus.domain`` -- the application facade contract is stable, the exact domain
class names are not.

Exit code contract (documented in README and ``--help``):

* ``0`` success;
* ``2`` invalid usage / arguments / input syntax (argparse's own code);
* ``3`` a controlled rejection or validation failure;
* ``4`` internal or configuration error (not the caller's fault).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

__all__ = ["REJECTION_CODES", "describe", "error_code", "is_rejection"]

#: ``JanusError.code`` values that describe a normal, structured rejection.
REJECTION_CODES: frozenset[str] = frozenset(
    {
        "VALIDATION",
        "INVARIANT",
        "NOT_FOUND",
        "UNSUPPORTED",
        "DATA_QUALITY",
        "STALE_DATA",
        "RISK_REJECTED",
        "INVALID_TRANSITION",
        "IDEMPOTENCY_CONFLICT",
        "SECURITY",
        "ACCOUNT_PAUSED",
    }
)


def error_code(exc: BaseException) -> str | None:
    """Return the stable machine-readable error code, if the exception has one."""
    raw = getattr(exc, "code", None)
    if raw is None:
        return None
    return str(getattr(raw, "value", raw))


def is_rejection(exc: BaseException) -> bool:
    """Return ``True`` when the failure is a caller-visible rejection (exit 3)."""
    if isinstance(exc, ValidationError):
        return True
    code = error_code(exc)
    if code is not None:
        return code in REJECTION_CODES
    # Plain input-shape ValueErrors (e.g. a proposal the container rejects while
    # constructing the schema model) are validation failures, not crashes.
    return isinstance(exc, ValueError)


def describe(exc: BaseException) -> str:
    """Render a short, secret-free description of a failure."""
    details = getattr(exc, "to_dict", None)
    if callable(details):
        try:
            payload: Any = details()
        except Exception:  # diagnostics must never raise
            payload = None
        if isinstance(payload, dict):
            code = str(payload.get("code") or error_code(exc) or "ERROR")
            message = str(payload.get("message") or exc)
            return f"{code}: {message}"
    if isinstance(exc, ValidationError):
        return f"VALIDATION: {_summarize_validation_error(exc)}"
    fallback_code = error_code(exc)
    if fallback_code is not None:
        return f"{fallback_code}: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _summarize_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = str(error.get("msg", "invalid value"))
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) if parts else "invalid input"
