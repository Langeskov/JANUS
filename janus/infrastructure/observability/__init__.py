"""Observability primitives: redacting logging and an append-only audit trail.

Everything produced here is safe to retain: secrets are stripped by the shared
:class:`~janus.infrastructure.observability.logging.Redactor` before a record is
formatted, logged, serialised, or written to disk.
"""

from __future__ import annotations

from janus.infrastructure.observability.audit import AuditRecord, AuditTrail, audit_trail
from janus.infrastructure.observability.logging import (
    REDACTED,
    RedactingFilter,
    Redactor,
    configure_logging,
    get_logger,
    is_sensitive_name,
    redact,
    redact_mapping,
)

__all__ = [
    "REDACTED",
    "AuditRecord",
    "AuditTrail",
    "RedactingFilter",
    "Redactor",
    "audit_trail",
    "configure_logging",
    "get_logger",
    "is_sensitive_name",
    "redact",
    "redact_mapping",
]
