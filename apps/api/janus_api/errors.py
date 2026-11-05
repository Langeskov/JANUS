"""Map controlled JANUS failures to HTTP status codes and JSON payloads.

The API is a transport: a controlled rejection (risk, validation, stale data,
paused account, ...) must never surface as a 500. Internal/unexpected failures
return a generic 500 body with no traceback and no internal detail.

Class-matched by the stable ``JanusError.code`` attribute rather than by
importing ``janus.domain`` classes, keeping ``apps`` free of domain imports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

__all__ = ["STATUS_BY_CODE", "http_error_for", "sanitize_validation_errors"]

#: Stable error code -> HTTP status.
STATUS_BY_CODE: dict[str, int] = {
    "VALIDATION": 422,
    "INVARIANT": 409,
    "NOT_FOUND": 404,
    "UNSUPPORTED": 422,
    "DATA_QUALITY": 422,
    "STALE_DATA": 422,
    "RISK_REJECTED": 422,
    "INVALID_TRANSITION": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "SECURITY": 403,
    "CONFIGURATION": 500,
    "ACCOUNT_PAUSED": 409,
    "INTERNAL": 500,
}


def _error_code(exc: BaseException) -> str | None:
    raw = getattr(exc, "code", None)
    if raw is None:
        return None
    return str(getattr(raw, "value", raw))


def _safe_payload(exc: BaseException, code: str) -> dict[str, Any]:
    """Build a JSON payload from a controlled error; never include a traceback."""
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        try:
            payload: Any = to_dict()
        except Exception:  # diagnostics must never raise
            payload = None
        if isinstance(payload, dict):
            normalised: dict[str, Any] = {str(key): value for key, value in payload.items()}
            normalised["code"] = code
            return normalised
    return {"code": code, "error_type": type(exc).__name__, "message": str(exc)}


def sanitize_validation_errors(
    errors: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only location/message/type: never echo raw request input back.

    Pydantic's raw error dicts carry an ``input`` element, which for an
    adversarial request would echo a credential or payload back in the response.
    """
    sanitized: list[dict[str, Any]] = []
    for error in errors[:20]:
        sanitized.append(
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "invalid value")),
                "type": str(error.get("type", "")),
            }
        )
    return sanitized


def _validation_payload(exc: ValidationError) -> dict[str, Any]:
    return {
        "code": "VALIDATION",
        "error_type": type(exc).__name__,
        "message": "request did not match the application schema",
        "errors": sanitize_validation_errors(exc.errors()),
    }


def http_error_for(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """Return ``(status_code, json_payload)`` for any exception at the API boundary."""
    if isinstance(exc, ValidationError):
        return 422, _validation_payload(exc)
    code = _error_code(exc)
    if code is not None:
        status_code = STATUS_BY_CODE.get(code, 422)
        return status_code, _safe_payload(exc, code)
    if isinstance(exc, ValueError):
        return 422, {
            "code": "VALIDATION",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    # Unknown failure: never leak internals.
    return 500, {
        "code": "INTERNAL",
        "error_type": type(exc).__name__,
        "message": "internal error",
    }
