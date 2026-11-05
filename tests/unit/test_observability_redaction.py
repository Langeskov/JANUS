"""Unit tests: observability output must never contain a credential.

Phase 0 spec s9.6/s9.8 -- secrets live outside the repository and are never
included in prompts, logs, or memory; tool calls and decision lineage are logged
with tokens redacted. These tests pin the shared :class:`Redactor`, the logging
filter, and the append-only :class:`AuditTrail`.
"""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

from janus.infrastructure.observability.audit import AuditTrail
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

SECRET = "sk-live-abcdef1234567890"
PLAIN_TOKEN = "plain-tushare-value-123"


# ---------------------------------------------------------------------------
# Name classification
# ---------------------------------------------------------------------------
def test_sensitive_names_are_recognised() -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "TUSHARE_TOKEN",
        "CLIENT_SECRET",
        "authorization",
        "X-API-KEY",
        "access_key_id",
        "my_password",
    ):
        assert is_sensitive_name(name), name


def test_non_sensitive_names_are_not_redacted() -> None:
    for name in ("symbol", "cash", "SAFE_SETTING", "profit", "target_weight", "decision_id"):
        assert not is_sensitive_name(name), name


# ---------------------------------------------------------------------------
# Text redaction
# ---------------------------------------------------------------------------
def test_redact_env_style_assignment() -> None:
    redacted = redact(f"DEEPSEEK_API_KEY={SECRET}")
    assert SECRET not in redacted
    assert REDACTED in redacted


def test_redact_env_file_keeps_non_secret_lines() -> None:
    content = "\n".join(
        [
            "TUSHARE_TOKEN=" + PLAIN_TOKEN,
            "SAFE_SETTING=1",
            "DEEPSEEK_API_KEY=" + SECRET,
        ]
    )
    redacted = redact(content)
    assert PLAIN_TOKEN not in redacted
    assert SECRET not in redacted
    assert "SAFE_SETTING=1" in redacted


def test_redact_authorization_header() -> None:
    redacted = redact("Authorization: Bearer abc123def456")
    assert "abc123def456" not in redacted
    # The header name is not secret; only the credential value is removed.
    assert "Authorization" in redacted
    assert REDACTED in redacted


def test_redact_json_api_key_field_preserves_safe_fields() -> None:
    text = f'{{"api_key": "{SECRET}", "symbol": "510300.SH"}}'
    redacted = redact(text)
    assert SECRET not in redacted
    assert '"symbol": "510300.SH"' in redacted


def test_redact_known_token_shapes() -> None:
    shapes = [
        "sk-proj-1234567890abcdef",
        "ghp_" + "a" * 20,
        "xoxb-1234567890-abcdef",
        "AKIAIOSFODNN7EXAMPLE",
    ]
    for secret in shapes:
        assert secret not in redact(f"credential={secret}"), secret


def test_redact_url_credentials() -> None:
    redacted = redact("sqlite+postgresql://janus:supersecret@localhost/janus")
    assert "supersecret" not in redacted
    assert "localhost/janus" in redacted


def test_redact_leaves_ordinary_text_untouched() -> None:
    text = "symbol=510300.SH cash=100000.00 action=BUY"
    assert redact(text) == text


def test_redactor_extra_patterns() -> None:
    redactor = Redactor(extra_patterns=(re.compile(r"ACME-\d{4}"),))
    assert "ACME-1234" not in redactor.redact("support code ACME-1234")


# ---------------------------------------------------------------------------
# Mapping redaction
# ---------------------------------------------------------------------------
def test_redact_mapping_nested_case_insensitive() -> None:
    data = {
        "api_key": SECRET,
        "nested": {"DEEPSEEK_API_KEY": PLAIN_TOKEN, "symbol": "510300.SH"},
        "sequence": ["Authorization: Bearer tok123", "ordinary text"],
        "TUSHARE_TOKEN": PLAIN_TOKEN,
        "safe": "kept",
    }
    redacted = redact_mapping(data)
    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["DEEPSEEK_API_KEY"] == REDACTED
    assert redacted["nested"]["symbol"] == "510300.SH"
    assert redacted["safe"] == "kept"
    serialised = json.dumps(redacted)
    assert SECRET not in serialised
    assert PLAIN_TOKEN not in serialised
    assert "tok123" not in serialised
    # The original mapping is not mutated.
    assert data["api_key"] == SECRET


def test_redact_mapping_keeps_numbers_and_bools() -> None:
    redacted = redact_mapping({"quantity": 100, "ok": True, "cash": "100000.00"})
    assert redacted == {"quantity": 100, "ok": True, "cash": "100000.00"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def test_logging_filter_removes_secrets() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("janus.tests.redaction")
    previous_handlers = logger.handlers[:]
    previous_propagate = logger.propagate
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info("provider call Authorization: Bearer %s", SECRET)
        logger.info("api_key=%s", SECRET)
    finally:
        logger.handlers = previous_handlers
        logger.propagate = previous_propagate
    output = stream.getvalue()
    assert SECRET not in output
    assert "provider call" in output
    assert REDACTED in output


def test_configure_logging_is_idempotent_and_redacts() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    configure_logging("DEBUG", stream=stream)
    managed = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_janus_managed_handler", False)
    ]
    assert len(managed) == 1
    assert logging.getLogger().level == logging.DEBUG
    logger = get_logger("janus.tests.configure")
    logger.info("token=%s", SECRET)
    assert SECRET not in stream.getvalue()


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
def test_audit_trail_appends_redacted_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    trail = AuditTrail(path=path)
    trail.record(
        "proposal_submitted",
        "REJECTED",
        executable=False,
        api_key=SECRET,
        symbol="510300.SH",
        thesis="Authorization: Bearer tok123",
    )
    trail.record("cycle_run", "OK", executable=True, decision_id="decision-1")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["decision_type"] == "proposal_submitted"
    assert first["outcome"] == "REJECTED"
    assert first["executable"] is False
    assert first["fields"]["api_key"] == REDACTED
    assert first["fields"]["symbol"] == "510300.SH"

    written = path.read_text(encoding="utf-8")
    assert SECRET not in written
    assert "tok123" not in written


def test_audit_trail_reserved_fields_cannot_be_overridden(tmp_path: Path) -> None:
    trail = AuditTrail(path=tmp_path / "audit.jsonl")
    record = trail.record("proposal_submitted", "REJECTED", recorded_at="not-overridden")
    assert record.fields == {}
    assert "recorded_at" not in record.fields
    assert record.recorded_at != "not-overridden"
    assert record.outcome == "REJECTED"
