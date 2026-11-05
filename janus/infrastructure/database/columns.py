"""SQLAlchemy column types that preserve JANUS domain value semantics.

Storage policy (Phase 0 spec s5.1, s10; acceptance criteria 3 and 4):

* **Money and arbitrary decimals are stored as TEXT.** SQLite has no exact
  decimal type: a ``NUMERIC`` column would be coerced through IEEE-754 floats
  and silently lose cents. ``DecimalText``/``MoneyText`` serialise the exact
  Decimal digits and rebuild the exact Decimal on load.
* **Datetimes are stored as UTC ISO-8601 TEXT** with an explicit offset. Naive
  datetimes are rejected on write (the domain forbids them), and values always
  load back timezone-aware.
* **Enums are stored as their ``.value`` strings**, not their Python names, so
  the persisted vocabulary matches the domain/API vocabulary exactly.
* **Structured audit payloads are stored as JSON TEXT** with a fail-closed
  serialiser (unknown types raise instead of being silently coerced).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from janus.domain.common.money import Money
from janus.domain.common.time import to_utc
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

__all__ = [
    "DecimalText",
    "JsonText",
    "MoneyText",
    "UtcDateTime",
    "enum_column",
]


class DecimalText(TypeDecorator[Decimal]):
    """Exact Decimal <-> TEXT. Never route money through SQLite NUMERIC/REAL."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        """Serialise a Decimal without exponent notation (``1E+2`` -> ``100``)."""
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(f"DecimalText expects Decimal, got {type(value).__name__}")
        return format(value, "f")

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        """Rebuild the exact Decimal from its stored decimal-string form."""
        if value is None:
            return None
        return Decimal(str(value))


class MoneyText(TypeDecorator[Money]):
    """Exact CNY :class:`~janus.domain.common.money.Money` <-> TEXT."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Money | None, dialect: Dialect) -> str | None:
        """Store only the amount; Phase 0 money is always CNY."""
        if value is None:
            return None
        if not isinstance(value, Money):
            raise TypeError(f"MoneyText expects Money, got {type(value).__name__}")
        return format(value.amount, "f")

    def process_result_value(self, value: Any, dialect: Dialect) -> Money | None:
        """Rebuild a quantised CNY Money value."""
        if value is None:
            return None
        return Money(amount=Decimal(str(value)))


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime <-> UTC ISO-8601 TEXT.

    Values are normalised to UTC before writing (the domain's comparison
    convention). The returned datetime is always timezone-aware; equality with
    the original instant therefore always holds.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        """Fail closed on naive datetimes (``to_utc`` raises ``ValidationError``)."""
        if value is None:
            return None
        return to_utc(value).isoformat(timespec="microseconds")

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        """Parse the stored ISO-8601 text back into an aware UTC datetime."""
        if value is None:
            return None
        return datetime.fromisoformat(str(value)).astimezone(UTC)


class JsonText(TypeDecorator[Any]):
    """JSON TEXT column with a deterministic, fail-closed serialiser."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        return json.loads(str(value))


def _json_default(value: Any) -> Any:
    """Serialise the domain scalar types an audit payload may legitimately hold."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON-serialisable")


def enum_column(enum_type: type[StrEnum]) -> SAEnum:
    """A TEXT enum column that persists enum *values* and validates on load."""
    return SAEnum(
        enum_type,
        native_enum=False,
        length=64,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
        create_constraint=False,
    )
