"""Structured logging with mandatory secret redaction.

Phase 0 spec s9.6/s9.8: secrets live outside the repository and are never
included in prompts, logs, or memory; tool calls and decision lineage are logged
while tokens and personal data are redacted.

The :class:`Redactor` is deliberately *pattern based* -- it cannot know every
secret a caller might hold -- so code must still never hand a credential to a
logger or an audit line. Redaction is a last line of defence, not permission to
log secrets.

Redaction covers:
  * ``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` / password / credential / authorization
    named values, in ``KEY=value`` (.env style), ``key=value`` and JSON
    ``"key": "value"`` shapes;
  * HTTP ``Authorization`` / ``Cookie`` / ``X-Api-Key`` header lines;
  * ``Bearer <token>`` strings;
  * well-known token shapes (``sk-...``, ``ghp_...``, ``xoxb-...``, ``AKIA...``);
  * credentials embedded in URLs (``scheme://user:password@host``).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, Sequence
from typing import IO, Any, Final

__all__ = [
    "REDACTED",
    "RedactingFilter",
    "Redactor",
    "configure_logging",
    "get_logger",
    "is_sensitive_name",
    "redact",
    "redact_mapping",
]

REDACTED: Final = "<redacted>"

DEFAULT_LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

# A field name is sensitive when it ends (after optional separators/words) in
# one of the credential nouns below. Matches DEEPSEEK_API_KEY, TUSHARE_TOKEN,
# CLIENT_SECRET, X-API-KEY, Authorization, access_key_id, ...
_SENSITIVE_NAME_RE: Final = re.compile(
    r"(?i)(?:^|[_\-.])?(?:[a-z0-9]+[_-])*"
    r"(?:key|token|secret|password|passwd|credential|authorization)"
    r"(?:[_-](?:id|value|env))?$"
)

# "Authorization: <anything to end of line>" style header lines.
_AUTH_HEADER_RE: Final = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|cookie)\b(\s*[:=]\s*)([^\r\n]+)"
)

# .env / config file lines: KEY=VALUE, where the KEY decides sensitivity.
_ENV_LINE_RE: Final = re.compile(
    r"(?im)^[ \t]*(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(?P<value>[^\r\n]*)$"
)

# Generic "key = value" / '"key": "value"' pairs (JSON or Python/Mapping reprs).
_ASSIGNMENT_RE: Final = re.compile(
    r"(?P<key>\"?[A-Za-z_][A-Za-z0-9_.\-]*\"?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\]\}\r\n]+)"
)

_BEARER_RE: Final = re.compile(r"(?i)\b(bearer)(\s+)([A-Za-z0-9._~+/=\-]+)")

_URL_USERINFO_RE: Final = re.compile(r"(://[^/\s:@]+:)([^/\s@]+)(@)")

_TOKEN_SHAPE_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:gh[pousr]|glpat)_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def is_sensitive_name(name: str) -> bool:
    """Return ``True`` when a field/key name denotes a credential."""
    return bool(_SENSITIVE_NAME_RE.search(name.strip().strip("\"'")))


def _redact_env_line(match: re.Match[str]) -> str:
    if not is_sensitive_name(match.group("key")):
        return match.group(0)
    return f"{match.group('key')}={REDACTED}"


def _redact_assignment(match: re.Match[str]) -> str:
    raw_key = match.group("key")
    if not is_sensitive_name(raw_key):
        return match.group(0)
    value = match.group("value")
    quote = value[0] if value and value[0] in "\"'" else ""
    return f"{raw_key}{match.group('sep')}{quote}{REDACTED}{quote}"


def _redact_text(text: str) -> str:
    redacted = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    redacted = _ENV_LINE_RE.sub(_redact_env_line, redacted)
    redacted = _BEARER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", redacted)
    redacted = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", redacted)
    for pattern in _TOKEN_SHAPE_RES:
        redacted = pattern.sub(REDACTED, redacted)
    return _ASSIGNMENT_RE.sub(_redact_assignment, redacted)


class Redactor:
    """Redacts credentials from text and nested mappings.

    ``extra_patterns`` lets a caller add project-specific secret shapes without
    changing the shared defaults. The class holds no state that could itself leak
    a secret.
    """

    __slots__ = ("_extra_patterns",)

    def __init__(self, *, extra_patterns: Sequence[re.Pattern[str]] = ()) -> None:
        self._extra_patterns: tuple[re.Pattern[str], ...] = tuple(extra_patterns)

    def redact(self, text: str) -> str:
        """Return ``text`` with every recognised secret replaced by ``<redacted>``."""
        redacted = _redact_text(text)
        for pattern in self._extra_patterns:
            redacted = pattern.sub(REDACTED, redacted)
        return redacted

    def redact_mapping(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Return a redacted deep copy of ``data`` (dict/list/scalar aware)."""
        return {str(key): self.redact_any(value, key=str(key)) for key, value in data.items()}

    def redact_any(self, value: Any, *, key: str | None = None) -> Any:
        """Redact one value; when ``key`` is sensitive the value is dropped entirely."""
        if key is not None and is_sensitive_name(key):
            return REDACTED
        if isinstance(value, Mapping):
            return {
                str(item_key): self.redact_any(item_value, key=str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact_any(item) for item in value]
        if isinstance(value, str):
            return self.redact(value)
        return value


_DEFAULT_REDACTOR: Final = Redactor()

# Attributes that the logging module itself owns; never rewritten in place.
_STANDARD_RECORD_ATTRS: Final = frozenset(
    logging.LogRecord("_janus", 0, "", 0, "", (), None).__dict__
) | frozenset({"message", "asctime"})

_MANAGED_HANDLER_ATTR: Final = "_janus_managed_handler"


class RedactingFilter(logging.Filter):
    """Logging filter that renders a record and removes credentials from it."""

    __slots__ = ("_redactor",)

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor: Redactor = redactor if redactor is not None else _DEFAULT_REDACTOR

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # a broken record must not crash logging
            rendered = str(record.msg)
        record.msg = self._redactor.redact(rendered)
        record.args = ()
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_RECORD_ATTRS:
                continue
            if isinstance(value, str):
                record.__dict__[key] = self._redactor.redact(value)
            elif isinstance(value, Mapping):
                record.__dict__[key] = self._redactor.redact_mapping(value)
            elif isinstance(value, (list, tuple)):
                record.__dict__[key] = self._redactor.redact_any(value)
        return True


def redact(text: str) -> str:
    """Redact secrets from a single string."""
    return _DEFAULT_REDACTOR.redact(text)


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Redact secrets from a nested mapping (returns a new object)."""
    return _DEFAULT_REDACTOR.redact_mapping(data)


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        if level < 0:
            raise ValueError(f"log level must be non-negative, got {level}")
        return level
    resolved = logging.getLevelName(level.upper())
    if isinstance(resolved, int):
        return resolved
    raise ValueError(f"unknown log level {level!r}")


def configure_logging(
    level: str | int = "INFO",
    *,
    stream: IO[str] | None = None,
) -> None:
    """Configure the process root logger once, with redaction attached.

    Idempotent: repeated calls update the level instead of stacking handlers.
    Logs go to stderr by default (stdout is reserved for CLI/JSON output).
    """
    numeric_level = _coerce_level(level)
    root = logging.getLogger()
    managed = next(
        (handler for handler in root.handlers if getattr(handler, _MANAGED_HANDLER_ATTR, False)),
        None,
    )
    if managed is None:
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S%z"))
        handler.addFilter(RedactingFilter())
        setattr(handler, _MANAGED_HANDLER_ATTR, True)
        root.addHandler(handler)
    root.setLevel(numeric_level)


def get_logger(name: str) -> logging.Logger:
    """Return the named logger (redaction is applied by the managed handler)."""
    return logging.getLogger(name)
