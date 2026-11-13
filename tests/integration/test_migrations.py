"""Alembic migration tests: a fresh SQLite file migrates to the full schema.

Covers Phase 0 P0-C acceptance: ``alembic upgrade head`` works from the repo
root (``JANUS_DB_URL`` overridable), all tables exist, foreign keys are enforced,
and the current revision equals the script head.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from janus.domain.common.errors import ConfigurationError
from janus.infrastructure.database import Database, create_engine_from_url
from janus.infrastructure.database.schema_check import (
    alembic_config,
    assert_schema_current,
    check_schema,
    head_revision,
    main,
)
from janus.infrastructure.database.tables import table_names
from sqlalchemy import Text, inspect
from sqlalchemy.exc import IntegrityError

REQUIRED_TABLES = frozenset(
    {
        "instruments",
        "account_ids",
        "accounts",
        "cash_ledger_entries",
        "position_events",
        "orders",
        "fills",
        "fee_lines",
        "risk_decisions",
        "risk_checks",
        "decision_records",
        "data_snapshots",
        "portfolio_snapshots",
        "portfolio_positions",
        "market_bars",
    }
)

HEAD_REVISION = "0002_data_foundation"


def _url(tmp_path: Path, name: str = "migrated.db") -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def _migrate(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def test_upgrade_head_creates_full_schema(tmp_path: Path) -> None:
    url = _url(tmp_path)
    config = alembic_config(url)
    command.upgrade(config, "head")

    status = check_schema(url)
    assert status.is_current
    assert status.current_revision == HEAD_REVISION
    assert status.current_revision == head_revision(config)

    engine = create_engine_from_url(url)
    try:
        inspector = inspect(engine)
        existing = set(inspector.get_table_names())
        assert existing >= REQUIRED_TABLES
        assert set(table_names()) <= existing
        assert "alembic_version" in existing

        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            assert str(journal_mode).lower() == "wal"
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() > 0
            foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_list('fills')").all()
            assert foreign_keys

        columns = {column["name"]: column for column in inspector.get_columns("accounts")}
        assert isinstance(columns["initial_capital"]["type"], Text)
        assert isinstance(columns["created_at"]["type"], Text)
    finally:
        engine.dispose()


def test_required_uniqueness_and_restrict_foreign_keys(tmp_path: Path) -> None:
    """The P0-C idempotency/sequence guarantees are enforced by the schema itself."""
    url = _url(tmp_path, "constraints.db")
    _migrate(url)
    engine = create_engine_from_url(url)
    try:
        inspector = inspect(engine)

        def unique_columns(table: str) -> set[tuple[str, ...]]:
            return {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table)
            }

        ledger_keys = {
            ("account_id", "sequence"),
            ("account_id", "idempotency_key"),
        }
        assert ledger_keys <= unique_columns("cash_ledger_entries")
        assert ledger_keys <= unique_columns("position_events")
        assert ("idempotency_key",) in unique_columns("fills")

        assert inspector.get_pk_constraint("orders")["constrained_columns"] == ["order_id"]
        assert inspector.get_pk_constraint("fills")["constrained_columns"] == ["fill_id"]
        assert inspector.get_pk_constraint("market_bars")["constrained_columns"] == [
            "symbol",
            "trade_date",
        ]
        assert inspector.get_pk_constraint("accounts")["constrained_columns"] == [
            "account_id",
            "version",
        ]

        fill_foreign_keys = inspector.get_foreign_keys("fills")
        assert any(
            foreign_key["referred_table"] == "orders"
            and (foreign_key.get("options") or {}).get("ondelete") == "RESTRICT"
            for foreign_key in fill_foreign_keys
        )
    finally:
        engine.dispose()


def test_foreign_key_restrict_blocks_orphan_rows(tmp_path: Path) -> None:
    url = _url(tmp_path, "fk.db")
    _migrate(url)
    engine = create_engine_from_url(url)
    try:
        insert_orphan = (
            "INSERT INTO cash_ledger_entries "
            "(entry_id, account_id, sequence, entry_type, amount, trade_date, "
            " occurred_at, source_ref, idempotency_key, description, balance_after) "
            "VALUES ('cash-orphan', 'acct-missing', 1, 'OPENING_CAPITAL', '1.00', "
            "'2026-11-05', '2026-11-05T00:00:00+00:00', 'src', 'orphan-key', '', NULL)"
        )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql(insert_orphan)
    finally:
        engine.dispose()


def test_schema_check_reports_pending_until_migrated(tmp_path: Path) -> None:
    url = _url(tmp_path, "pending.db")
    status = check_schema(url)
    assert status.current_revision is None
    assert not status.is_current
    assert "PENDING MIGRATIONS" in status.describe()

    with pytest.raises(ConfigurationError):
        assert_schema_current(url)

    _migrate(url)
    assert check_schema(url).is_current
    assert assert_schema_current(url).current_revision == HEAD_REVISION


def test_janus_db_url_environment_variable_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _url(tmp_path, "env-override.db")
    monkeypatch.setenv("JANUS_DB_URL", url)

    command.upgrade(alembic_config(), "head")

    assert check_schema(url).is_current
    assert check_schema().database_url == url


def test_downgrade_to_base_drops_the_schema(tmp_path: Path) -> None:
    url = _url(tmp_path, "downgrade.db")
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine_from_url(url)
    try:
        existing = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert not (REQUIRED_TABLES & existing)
    assert check_schema(url).current_revision is None


def test_database_helper_upgrades_to_head(tmp_path: Path) -> None:
    url = _url(tmp_path, "helper.db")
    database = Database(url)
    try:
        database.upgrade_to_head()
        assert check_schema(url).is_current
        with database.engine.connect() as connection:
            tables = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).scalars()
            assert set(tables) >= REQUIRED_TABLES
    finally:
        database.dispose()


def test_schema_check_cli_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CI can gate on the CLI: non-zero while migrations are pending, then zero."""
    url = _url(tmp_path, "cli.db")
    monkeypatch.setenv("JANUS_DB_URL", url)

    assert main(["--database-url", url]) == 1
    assert "PENDING MIGRATIONS" in capsys.readouterr().out

    _migrate(url)

    assert main(["--database-url", url]) == 0
    output = capsys.readouterr().out
    assert "up to date" in output
    assert HEAD_REVISION in output
