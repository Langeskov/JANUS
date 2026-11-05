"""Append-only audit trail for decisions and tool calls.

Phase 0 spec s9.8 and s10: every tool call and decision is logged with its
lineage, while credentials and personal data are redacted. Phase 0 keeps the
trail simple: JSON lines on an optional local path plus the observability
logger; a later phase can move the sink into the database without changing the
call sites.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from janus.domain.common.time import utc_now
from janus.infrastructure.observability.logging import Redactor, get_logger

__all__ = ["AuditRecord", "AuditTrail"]

_RESERVED_FIELDS = frozenset({"recorded_at", "decision_type", "outcome", "executable"})


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One immutable audit entry (already redacted before it is stored)."""

    decision_type: str
    outcome: str
    recorded_at: str
    executable: bool = False
    fields: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recorded_at": self.recorded_at,
            "decision_type": self.decision_type,
            "outcome": self.outcome,
            "executable": self.executable,
            "fields": dict(self.fields),
        }


class AuditTrail:
    """Writes redacted JSON-line audit records to a logger and optionally a file.

    The writer is append-only: it never rewrites or truncates an existing file.
    Callers must pass *facts* (ids, outcomes, rule versions) rather than raw
    payloads, but any secret that slips into a field is stripped by the shared
    redactor.
    """

    __slots__ = ("_logger", "_path", "_redactor")

    def __init__(
        self,
        *,
        logger_name: str = "janus.audit",
        path: str | Path | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._logger = get_logger(logger_name)
        self._path: Path | None = Path(path) if path is not None else None
        self._redactor: Redactor = redactor if redactor is not None else Redactor()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path | None:
        return self._path

    def record(
        self,
        decision_type: str,
        outcome: str,
        *,
        executable: bool = False,
        **fields: Any,
    ) -> AuditRecord:
        """Append one audit record and return it.

        Reserved keys (``recorded_at``/``decision_type``/``outcome``/
        ``executable``) cannot be overridden by ``fields``.
        """
        safe_fields = self._redactor.redact_mapping(
            {key: value for key, value in fields.items() if key not in _RESERVED_FIELDS}
        )
        record = AuditRecord(
            decision_type=decision_type,
            outcome=outcome,
            recorded_at=utc_now().isoformat(),
            executable=executable,
            fields=safe_fields,
        )
        line = json.dumps(record.as_dict(), sort_keys=True, ensure_ascii=False, default=str)
        self._logger.info("audit %s", line)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record


def audit_trail(path: str | Path | None = None) -> AuditTrail:
    """Convenience factory for the default audit trail."""
    return AuditTrail(path=path, redactor=Redactor())
