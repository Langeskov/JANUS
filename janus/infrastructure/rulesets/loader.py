"""Strict YAML loading for versioned market-rules configuration.

Everything the market-rules engine needs is *data*: rule-sets, fee schedules and
the trading calendar live in ``config/market_rules/`` and are loaded here by the
infrastructure layer so that ``janus.domain`` never imports a YAML library
(CONTRACT.md s2.5).

Configuration is fail-closed: unknown keys, missing required fields, schema
drift, duplicate ids, gaps in calendar coverage and fee schedules that do not
cover the rule-set window all raise
:class:`~janus.domain.common.errors.ConfigurationError` naming the file and the
offending field. Nothing is silently defaulted.

Document formats (one document per file)::

    # ruleset_*.yaml
    rule_set:
      rule_set_id: CN-SSE-SZSE-2026.1
      ...
      fee_schedule_ids: [CN-EQUITY-DEFAULT, CN-ETF-DEFAULT]

    # fees_*.yaml
    fee_schedules:
      - fee_schedule_id: CN-EQUITY-DEFAULT
        ...

    # calendar_*.yaml
    calendar:
      calendar_id: CN-2026-H2
      coverage_from: 2026-09-01
      coverage_to: 2026-12-31
      default_sessions: [...]
      days:
        - {date: 2026-09-01, day_type: TRADING, source: PRC_WEEKDAY_RULE}
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from janus.domain.common.enums import TradingDayType
from janus.domain.common.errors import ConfigurationError
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.models import (
    FeeSchedule,
    TradingDay,
    TradingRuleSet,
    TradingSession,
)
from pydantic import ValidationError as PydanticValidationError

RULESET_FILE_GLOB = "ruleset_*.yaml"
FEES_FILE_GLOB = "fees_*.yaml"
CALENDAR_FILE_GLOB = "calendar_*.yaml"

__all__ = [
    "CALENDAR_FILE_GLOB",
    "FEES_FILE_GLOB",
    "RULESET_FILE_GLOB",
    "load_calendar",
    "load_fee_schedules",
    "load_rule_sets",
]


# ---------------------------------------------------------------------------
# Generic document helpers
# ---------------------------------------------------------------------------


def _read_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigurationError("configuration file not found", file=str(path)) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            "configuration file is not valid YAML", file=str(path), error=str(exc)
        ) from exc
    if raw is None:
        raise ConfigurationError("configuration file is empty", file=str(path))
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration document must be a mapping", file=str(path))
    return raw


def _require_mapping(payload: Any, *, file: Path, field: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ConfigurationError(f"'{field}' must be a mapping", file=str(file), field=field)
    return payload


def _require_list(payload: Any, *, file: Path, field: str) -> list[Any]:
    if not isinstance(payload, list):
        raise ConfigurationError(f"'{field}' must be a list", file=str(file), field=field)
    return payload


def _reject_unknown_keys(
    payload: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    file: Path,
    where: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConfigurationError(
            f"unknown configuration key(s) under '{where}': {', '.join(unknown)}",
            file=str(file),
            field=f"{where}.{unknown[0]}",
            unknown_keys=unknown,
        )


def _as_configuration_error(
    exc: PydanticValidationError, *, file: Path, where: str
) -> ConfigurationError:
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return ConfigurationError(f"invalid {where} configuration", file=str(file), field=where)
    details: list[dict[str, str]] = []
    for error in errors:
        location = ".".join(str(part) for part in error["loc"])
        details.append(
            {
                "field": f"{where}.{location}" if location else where,
                "message": error["msg"],
            }
        )
    first = details[0]
    return ConfigurationError(
        f"invalid {where} configuration at {first['field']}: {first['message']}",
        file=str(file),
        field=first["field"],
        errors=details,
    )


def _resolve_files(path: Path | str, pattern: str) -> tuple[Path, ...]:
    """Expand a file-or-directory argument to the matching configuration files."""
    candidate = Path(path)
    if candidate.is_dir():
        files = tuple(sorted(candidate.glob(pattern)))
        if not files:
            raise ConfigurationError(
                f"no '{pattern}' configuration files found", directory=str(candidate)
            )
        return files
    if candidate.is_file():
        return (candidate,)
    raise ConfigurationError("configuration path does not exist", path=str(candidate))


# ---------------------------------------------------------------------------
# Fee schedules
# ---------------------------------------------------------------------------


def _build_fee_schedule(payload: Any, *, file: Path) -> FeeSchedule:
    mapping = _require_mapping(payload, file=file, field="fee_schedules[]")
    _reject_unknown_keys(
        mapping, frozenset(FeeSchedule.model_fields), file=file, where="fee_schedules[]"
    )
    try:
        return FeeSchedule.model_validate(dict(mapping))
    except PydanticValidationError as exc:
        raise _as_configuration_error(exc, file=file, where="fee_schedules[]") from exc


def load_fee_schedules(path: Path | str) -> tuple[FeeSchedule, ...]:
    """Load every effective-dated fee schedule from a file or a config directory."""
    by_id: dict[str, FeeSchedule] = {}
    for file in _resolve_files(path, FEES_FILE_GLOB):
        document = _read_document(file)
        _reject_unknown_keys(document, frozenset({"fee_schedules"}), file=file, where="<root>")
        if "fee_schedules" not in document:
            raise ConfigurationError(
                "missing required field 'fee_schedules'",
                file=str(file),
                field="fee_schedules",
            )
        for item in _require_list(document["fee_schedules"], file=file, field="fee_schedules"):
            schedule = _build_fee_schedule(item, file=file)
            if schedule.fee_schedule_id in by_id:
                raise ConfigurationError(
                    "duplicate fee schedule id",
                    file=str(file),
                    field="fee_schedules[].fee_schedule_id",
                    fee_schedule_id=schedule.fee_schedule_id,
                )
            by_id[schedule.fee_schedule_id] = schedule
    return tuple(by_id.values())


# ---------------------------------------------------------------------------
# Trading rule-sets
# ---------------------------------------------------------------------------


def _build_rule_set(
    payload: Any, *, file: Path, fee_schedules: Mapping[str, FeeSchedule]
) -> TradingRuleSet:
    mapping = _require_mapping(payload, file=file, field="rule_set")
    allowed = frozenset(
        (set(TradingRuleSet.model_fields) - {"fee_schedules"}) | {"fee_schedule_ids"}
    )
    _reject_unknown_keys(mapping, allowed, file=file, where="rule_set")
    if "fee_schedule_ids" not in mapping:
        raise ConfigurationError(
            "missing required field 'fee_schedule_ids'",
            file=str(file),
            field="rule_set.fee_schedule_ids",
        )
    raw_ids = _require_list(
        mapping["fee_schedule_ids"], file=file, field="rule_set.fee_schedule_ids"
    )
    if not raw_ids:
        raise ConfigurationError(
            "a rule set must reference at least one fee schedule",
            file=str(file),
            field="rule_set.fee_schedule_ids",
        )
    referenced: list[FeeSchedule] = []
    for raw_id in raw_ids:
        if not isinstance(raw_id, str):
            raise ConfigurationError(
                "fee_schedule_ids entries must be strings",
                file=str(file),
                field="rule_set.fee_schedule_ids",
            )
        schedule = fee_schedules.get(raw_id)
        if schedule is None:
            raise ConfigurationError(
                "rule set references an unknown fee schedule",
                file=str(file),
                field="rule_set.fee_schedule_ids",
                fee_schedule_id=raw_id,
            )
        referenced.append(schedule)
    data = {key: value for key, value in mapping.items() if key != "fee_schedule_ids"}
    data["fee_schedules"] = referenced
    try:
        return TradingRuleSet.model_validate(data)
    except PydanticValidationError as exc:
        raise _as_configuration_error(exc, file=file, where="rule_set") from exc


def _validate_fee_coverage(
    rule_set: TradingRuleSet, fee_schedules: Mapping[str, FeeSchedule], *, file: Path
) -> None:
    """Every profile's fee schedule must cover the rule-set's effective window."""
    for profile in rule_set.profiles:
        schedule = fee_schedules.get(profile.fee_schedule_id)
        if schedule is None:  # pragma: no cover - validated while building
            raise ConfigurationError(
                "rule profile references an unknown fee schedule",
                file=str(file),
                field="rule_set.profiles[].fee_schedule_id",
                fee_schedule_id=profile.fee_schedule_id,
            )
        if not schedule.is_effective_at(rule_set.effective_from):
            raise ConfigurationError(
                "fee schedule is not effective at the rule-set effective_from",
                file=str(file),
                field="rule_set.profiles[].fee_schedule_id",
                fee_schedule_id=profile.fee_schedule_id,
                rule_set_effective_from=rule_set.effective_from.isoformat(),
            )
        if rule_set.effective_to is not None and (
            schedule.effective_to is not None and schedule.effective_to < rule_set.effective_to
        ):
            raise ConfigurationError(
                "fee schedule ends before the rule-set effective_to",
                file=str(file),
                field="rule_set.profiles[].fee_schedule_id",
                fee_schedule_id=profile.fee_schedule_id,
                rule_set_effective_to=rule_set.effective_to.isoformat(),
            )


def load_rule_sets(
    path: Path | str,
    *,
    fee_schedules: Sequence[FeeSchedule] | None = None,
) -> tuple[TradingRuleSet, ...]:
    """Load every versioned trading rule-set from a file or a config directory.

    When ``fee_schedules`` is omitted and ``path`` is a directory, the directory's
    ``fees_*.yaml`` documents are loaded automatically. Loading a single rule-set
    file requires the fee schedules to be supplied explicitly.
    """
    candidate = Path(path)
    if fee_schedules is None:
        if candidate.is_dir():
            schedules = load_fee_schedules(candidate)
        else:
            raise ConfigurationError(
                "fee_schedules are required when loading a single rule-set file",
                file=str(candidate),
            )
    else:
        schedules = tuple(fee_schedules)
    fee_map: dict[str, FeeSchedule] = {}
    for schedule in schedules:
        if schedule.fee_schedule_id in fee_map:
            raise ConfigurationError(
                "duplicate fee schedule id", fee_schedule_id=schedule.fee_schedule_id
            )
        fee_map[schedule.fee_schedule_id] = schedule
    rule_sets: list[TradingRuleSet] = []
    seen: dict[tuple[str, str], Path] = {}
    for file in _resolve_files(path, RULESET_FILE_GLOB):
        document = _read_document(file)
        _reject_unknown_keys(document, frozenset({"rule_set"}), file=file, where="<root>")
        if "rule_set" not in document:
            raise ConfigurationError(
                "missing required field 'rule_set'", file=str(file), field="rule_set"
            )
        rule_set = _build_rule_set(document["rule_set"], file=file, fee_schedules=fee_map)
        key = (rule_set.rule_set_id, rule_set.version)
        if key in seen:
            raise ConfigurationError(
                "duplicate rule-set registration",
                file=str(file),
                field="rule_set.rule_set_id",
                rule_set_id=rule_set.rule_set_id,
                version=rule_set.version,
                first_file=str(seen[key]),
            )
        seen[key] = file
        _validate_fee_coverage(rule_set, fee_map, file=file)
        rule_sets.append(rule_set)
    return tuple(rule_sets)


# ---------------------------------------------------------------------------
# Trading calendar
# ---------------------------------------------------------------------------

_CALENDAR_KEYS = frozenset(
    {
        "calendar_id",
        "version",
        "coverage_from",
        "coverage_to",
        "source_refs",
        "notes",
        "default_sessions",
        "days",
    }
)
_CALENDAR_DAY_KEYS = frozenset({"date", "day_type", "source"})
_SESSION_KEYS = frozenset(TradingSession.model_fields)
_DAY_TYPES = frozenset(member.value for member in TradingDayType)


def _parse_date(value: Any, *, file: Path, field: str) -> date:
    if isinstance(value, datetime):  # pragma: no cover - defensive YAML shape
        raise ConfigurationError("expected a date, got a datetime", file=str(file), field=field)
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ConfigurationError(
                "invalid ISO date", file=str(file), field=field, value=value
            ) from exc
    raise ConfigurationError("expected an ISO date", file=str(file), field=field, value=repr(value))


def _build_sessions(payload: Any, *, file: Path, field: str) -> tuple[TradingSession, ...]:
    sessions: list[TradingSession] = []
    for item in _require_list(payload, file=file, field=field):
        mapping = _require_mapping(item, file=file, field=field)
        _reject_unknown_keys(mapping, _SESSION_KEYS, file=file, where=field)
        try:
            sessions.append(TradingSession.model_validate(dict(mapping)))
        except PydanticValidationError as exc:
            raise _as_configuration_error(exc, file=file, where=field) from exc
    return tuple(sessions)


def _build_calendar(payload: Mapping[str, Any], *, file: Path) -> TradingCalendar:
    _reject_unknown_keys(payload, _CALENDAR_KEYS, file=file, where="calendar")
    for required in ("calendar_id", "version", "coverage_from", "coverage_to", "days"):
        if required not in payload:
            raise ConfigurationError(
                "missing required field", file=str(file), field=f"calendar.{required}"
            )
    calendar_id = payload["calendar_id"]
    if not isinstance(calendar_id, str) or not calendar_id.strip():
        raise ConfigurationError(
            "calendar_id must be a non-empty string",
            file=str(file),
            field="calendar.calendar_id",
        )
    coverage_from = _parse_date(payload["coverage_from"], file=file, field="calendar.coverage_from")
    coverage_to = _parse_date(payload["coverage_to"], file=file, field="calendar.coverage_to")
    if coverage_to < coverage_from:
        raise ConfigurationError(
            "calendar coverage_to must not precede coverage_from",
            file=str(file),
            field="calendar.coverage_to",
        )
    if "default_sessions" not in payload:
        raise ConfigurationError(
            "missing required field 'calendar.default_sessions'",
            file=str(file),
            field="calendar.default_sessions",
        )
    default_sessions = _build_sessions(
        payload["default_sessions"], file=file, field="calendar.default_sessions"
    )
    if not default_sessions:
        raise ConfigurationError(
            "calendar.default_sessions must not be empty",
            file=str(file),
            field="calendar.default_sessions",
        )
    days: list[TradingDay] = []
    seen: set[date] = set()
    for index, item in enumerate(_require_list(payload["days"], file=file, field="calendar.days")):
        where = f"calendar.days[{index}]"
        mapping = _require_mapping(item, file=file, field=where)
        _reject_unknown_keys(mapping, _CALENDAR_DAY_KEYS, file=file, where=where)
        if "date" not in mapping or "day_type" not in mapping:
            raise ConfigurationError(
                "each calendar day requires 'date' and 'day_type'",
                file=str(file),
                field=where,
            )
        day = _parse_date(mapping["date"], file=file, field=f"{where}.date")
        if day in seen:
            raise ConfigurationError(
                "duplicate calendar day",
                file=str(file),
                field=f"{where}.date",
                day=day.isoformat(),
            )
        seen.add(day)
        raw_type = mapping["day_type"]
        if not isinstance(raw_type, str) or raw_type not in _DAY_TYPES:
            raise ConfigurationError(
                "invalid trading-day type",
                file=str(file),
                field=f"{where}.day_type",
                value=repr(raw_type),
                allowed=sorted(_DAY_TYPES),
            )
        day_type = TradingDayType(raw_type)
        is_weekend = day.weekday() >= 5
        if is_weekend and day_type is not TradingDayType.WEEKEND:
            raise ConfigurationError(
                "a Saturday/Sunday entry must be declared WEEKEND "
                "(exchanges do not trade on weekends, including make-up working days)",
                file=str(file),
                field=f"{where}.day_type",
                day=day.isoformat(),
            )
        if not is_weekend and day_type is TradingDayType.WEEKEND:
            raise ConfigurationError(
                "a weekday entry cannot be declared WEEKEND",
                file=str(file),
                field=f"{where}.day_type",
                day=day.isoformat(),
            )
        days.append(
            TradingDay(
                trade_date=day,
                day_type=day_type,
                sessions=default_sessions if day_type is TradingDayType.TRADING else (),
            )
        )
    expected = {
        coverage_from + timedelta(days=offset)
        for offset in range((coverage_to - coverage_from).days + 1)
    }
    missing = sorted(expected - seen)
    extra = sorted(seen - expected)
    if missing or extra:
        raise ConfigurationError(
            "calendar days must cover every date between coverage_from and coverage_to",
            file=str(file),
            field="calendar.days",
            missing_days=[day.isoformat() for day in missing],
            extra_days=[day.isoformat() for day in extra],
        )
    return TradingCalendar(days, name=calendar_id)


def load_calendar(path: Path | str) -> TradingCalendar:
    """Load a trading calendar from a ``calendar_*.yaml`` document.

    JSON fixture files under ``data/fixtures/calendar/`` are accepted too, because
    JSON is a subset of YAML.
    """
    candidate = Path(path)
    if candidate.is_dir():
        files = _resolve_files(candidate, CALENDAR_FILE_GLOB)
        if len(files) > 1:
            raise ConfigurationError(
                "multiple calendar files found; a registry loads exactly one",
                directory=str(candidate),
                files=[file.name for file in files],
            )
        file = files[0]
    elif candidate.is_file():
        file = candidate
    else:
        raise ConfigurationError("calendar file not found", path=str(candidate))
    document = _read_document(file)
    _reject_unknown_keys(document, frozenset({"calendar"}), file=file, where="<root>")
    if "calendar" not in document:
        raise ConfigurationError(
            "missing required field 'calendar'", file=str(file), field="calendar"
        )
    payload = _require_mapping(document["calendar"], file=file, field="calendar")
    return _build_calendar(payload, file=file)
