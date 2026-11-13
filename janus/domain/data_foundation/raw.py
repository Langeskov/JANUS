"""Immutable raw payload archive plus credential screening.

Requirements (PR-001 §4.2): preserve the original provider payload, hash it,
record provider/dataset scope, support deterministic lookup and duplicate
detection — and **never** persist credentials, authorisation headers, passwords,
session cookies or environment configuration.

Screening is deliberately conservative: raw payloads that appear to contain
credential material are refused, so a caller must redact before archiving. The
alternative (silently storing them) would violate the requirement.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from janus.domain.common.errors import ValidationError
from janus.domain.common.time import ensure_aware
from janus.domain.data_foundation.identifiers import content_hash, require_content_hash

__all__ = [
    "CREDENTIAL_PATTERNS",
    "RawPayload",
    "RedactionFinding",
    "find_credential_material",
    "require_clean_payload",
]

_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
"""Hard cap per archived payload (8 MiB). Phase 1 datasets are daily-frequency."""

_MAX_SCOPE_KEYS = 20
_MAX_SCOPE_VALUE_CHARS = 500


class RedactionFinding(StrEnum):
    """Categories of credential material that must never be archived."""

    AUTHORIZATION_HEADER = "AUTHORIZATION_HEADER"
    BEARER_TOKEN = "BEARER_TOKEN"
    API_KEY_ASSIGNMENT = "API_KEY_ASSIGNMENT"
    COOKIE_HEADER = "COOKIE_HEADER"
    PRIVATE_KEY_BLOCK = "PRIVATE_KEY_BLOCK"
    PASSWORD_ASSIGNMENT = "PASSWORD_ASSIGNMENT"


CREDENTIAL_PATTERNS: tuple[tuple[RedactionFinding, re.Pattern[str]], ...] = (
    (
        RedactionFinding.AUTHORIZATION_HEADER,
        # JSON-style quoted keys are common, so an optional quote may sit between
        # the key name and the separator.
        re.compile(r"(?i)\bauthorization\b[\"']?\s*[:=]\s*\S"),
    ),
    (
        RedactionFinding.BEARER_TOKEN,
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    ),
    (
        RedactionFinding.API_KEY_ASSIGNMENT,
        re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|access[_-]?token|secret[_-]?key|"
            r"client[_-]?secret|app[_-]?secret)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{8,}"
        ),
    ),
    (
        RedactionFinding.COOKIE_HEADER,
        re.compile(r"(?i)\b(?:set-)?cookie\b[\"']?\s*[:=]\s*\S"),
    ),
    (
        RedactionFinding.PRIVATE_KEY_BLOCK,
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ),
    (
        RedactionFinding.PASSWORD_ASSIGNMENT,
        re.compile(r"(?i)\b(?:password|passwd|pwd)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{6,}"),
    ),
)
"""Ordered (finding, pattern) pairs; the order makes findings deterministic."""


def find_credential_material(text: str) -> tuple[RedactionFinding, ...]:
    """Return every credential category detected in ``text`` (deterministic order)."""
    found: list[RedactionFinding] = []
    for finding, pattern in CREDENTIAL_PATTERNS:
        if pattern.search(text):
            found.append(finding)
    return tuple(dict.fromkeys(found))


def require_clean_payload(text: str) -> None:
    """Raise :class:`ValidationError` when credential material would be archived."""
    findings = find_credential_material(text)
    if findings:
        raise ValidationError(
            "raw payload appears to contain credential material; redact it before "
            "archiving (raw payloads must never store tokens, cookies, headers or "
            "passwords)",
            findings=[finding.value for finding in findings],
        )


class RawPayload(BaseModel):
    """One immutable archived provider response.

    ``payload_id`` is derived from the content hash, so identical bytes always
    map to the same row: re-retrieval cannot create a duplicate archive entry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_id: str
    content_hash: str
    byte_size: int = Field(ge=0, le=_MAX_PAYLOAD_BYTES)
    media_type: str = Field(default="application/json", min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=100)
    dataset: str = Field(min_length=1, max_length=100)
    request_scope: dict[str, str] = Field(default_factory=dict)
    """Non-secret request scope (e.g. symbol, date range). Never headers or tokens."""

    retrieved_at: datetime
    ingested_at: datetime
    payload_text: str
    redaction_findings: tuple[RedactionFinding, ...] = ()
    """Always empty for an archived payload: findings refuse the archive."""

    @field_validator("content_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return require_content_hash(value)

    @field_validator("retrieved_at", "ingested_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "timestamp")

    @field_validator("request_scope")
    @classmethod
    def _scope(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > _MAX_SCOPE_KEYS:
            raise ValidationError("request_scope has too many keys")
        cleaned: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValidationError("request_scope keys must be non-empty strings")
            if not isinstance(item, str):
                raise ValidationError("request_scope values must be strings")
            if len(item) > _MAX_SCOPE_VALUE_CHARS:
                raise ValidationError("request_scope value is too long", key=key)
            lowered = key.lower()
            if any(
                forbidden in lowered
                for forbidden in ("token", "secret", "key", "password", "cookie", "auth")
            ):
                raise ValidationError(
                    "request_scope must not contain credential-like keys", key=key
                )
            cleaned[key] = item
        return dict(sorted(cleaned.items()))

    @model_validator(mode="after")
    def _content_integrity(self) -> RawPayload:
        if self.ingested_at < self.retrieved_at:
            raise ValidationError("ingested_at must not precede retrieved_at")
        actual_hash = content_hash(self.payload_text)
        if actual_hash != self.content_hash:
            raise ValidationError(
                "content_hash does not match payload_text",
                expected=self.content_hash,
                actual=actual_hash,
            )
        if self.byte_size != len(self.payload_text.encode("utf-8")):
            raise ValidationError(
                "byte_size does not match the UTF-8 length of payload_text",
                declared=self.byte_size,
                actual=len(self.payload_text.encode("utf-8")),
            )
        if self.redaction_findings:
            raise ValidationError(
                "an archived payload must be free of credential material",
                findings=[finding.value for finding in self.redaction_findings],
            )
        return self

    @classmethod
    def archive(
        cls,
        *,
        source_id: str,
        dataset: str,
        payload_text: str,
        retrieved_at: datetime,
        ingested_at: datetime,
        media_type: str = "application/json",
        request_scope: dict[str, str] | None = None,
    ) -> RawPayload:
        """Screen, hash and build an archive record for ``payload_text``.

        Raises :class:`ValidationError` when credential material is detected.
        """
        require_clean_payload(payload_text)
        if len(payload_text.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValidationError(
                "raw payload exceeds the archive size limit", limit_bytes=_MAX_PAYLOAD_BYTES
            )
        digest = content_hash(payload_text)
        return cls(
            payload_id=f"raw-{digest[:32]}",
            content_hash=digest,
            byte_size=len(payload_text.encode("utf-8")),
            media_type=media_type,
            source_id=source_id,
            dataset=dataset,
            request_scope=dict(request_scope or {}),
            retrieved_at=ensure_aware(retrieved_at, "retrieved_at"),
            ingested_at=ensure_aware(ingested_at, "ingested_at"),
            payload_text=payload_text,
        )
