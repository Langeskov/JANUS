"""Deterministic identifier and content-hash helpers for data-foundation records.

Every persisted raw payload, provenance record and data revision gets an
identifier that is **derived from its content**, so that:

* re-ingesting the same bytes yields the same payload id (natural deduplication);
* re-ingesting the same fact at the same source revision yields the same
  revision id (no phantom revisions);
* two independently running processes agree on identifiers without coordination.

Deliberately *not* deterministic (they identify events, not content):
``ingestion_id`` (one per ingestion attempt) and ``data_source`` registration
timestamps.
"""

from __future__ import annotations

import hashlib
import re

from janus.domain.common.errors import ValidationError

__all__ = [
    "SHA256_HEX_PATTERN",
    "content_hash",
    "deterministic_id",
    "is_valid_content_hash",
    "require_content_hash",
]

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

SHA256_HEX_PATTERN: re.Pattern[str] = _SHA256_HEX_RE
"""Lower-case hex SHA-256, the only accepted content-hash form."""

_UNIT_SEPARATOR = "\x1f"
"""ASCII unit separator: cannot occur in normalised text fields, so joining is
unambiguous (unlike ``|`` or ``:`` which can appear inside record ids)."""


def content_hash(payload: str | bytes) -> str:
    """Return the lower-case hex SHA-256 of ``payload``.

    Text is hashed as UTF-8 without newline translation so a payload's hash does
    not depend on how it was transported.
    """
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


def is_valid_content_hash(value: str) -> bool:
    """True when ``value`` is a lower-case hex SHA-256 digest."""
    return bool(_SHA256_HEX_RE.match(value))


def require_content_hash(value: str, *, field: str = "payload_hash") -> str:
    """Validate a content hash, failing closed on anything else."""
    if not isinstance(value, str) or not is_valid_content_hash(value):
        raise ValidationError(
            f"{field} must be a lower-case hex SHA-256 digest", field=field, value=repr(value)
        )
    return value


def deterministic_id(prefix: str, *parts: str) -> str:
    """Build ``<prefix>-<32 hex chars>`` from the supplied identity parts.

    Parts are joined with an ASCII unit separator, so
    ``deterministic_id("rev", "a", "bc")`` and ``deterministic_id("rev", "ab", "c")``
    are guaranteed to differ.
    """
    if not prefix or not prefix.isalnum():
        raise ValidationError(f"identifier prefix must be alphanumeric, got {prefix!r}")
    if not parts:
        raise ValidationError("deterministic_id requires at least one identity part")
    joined = _UNIT_SEPARATOR.join(parts)
    return f"{prefix}-{hashlib.sha256(joined.encode('utf-8')).hexdigest()[:32]}"
