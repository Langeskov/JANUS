"""PR-001 migration tests: fresh database and an existing Phase 0 database.

The critical property under test is that ``0002_data_foundation`` is
inspector-guarded and idempotent:

* on a **fresh** database, ``0001_initial`` (metadata-driven) has already created
  the eight new tables and the ``risk_checks.verdict`` column, so ``0002`` must
  decide to do nothing and still reach head;
* on an **existing Phase 0 database** (stamped ``0001_initial``, no data
  foundation objects, ``risk_checks`` without ``verdict``) the same revision must
  create the eight tables and add the column without touching Phase 0 data.

A ledger-replay test then proves that the migrated database still rebuilds the
paper account exactly from its persisted history.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from janus.domain.common.money import Money
from janus.domain.common.time import MARKET_TZ
from janus.domain.data_foundation.capabilities import DataSource, DataSourceKind
from janus.domain.data_foundation.quality import (
    IngestionReport,
    QualityIssue,
    QualityIssueCode,
)
from janus.domain.ledger.models import LedgerEntryType
from janus.domain.ledger.replay import rebuild_account
from janus.domain.risk.models import RiskVerdict
from janus.infrastructure.database import (
    Database,
    SqlAlchemyUnitOfWorkFactory,
    create_engine_from_url,
)
from janus.infrastructure.database.schema_check import (
    alembic_config,
    check_schema,
    head_revision,
)
from janus.infrastructure.database.tables import Base, table_names
from sqlalchemy import inspect
from tests.integration import scenario

HEAD_REVISION = "0002_data_foundation"
BASE_P0_REVISION = "0001_initial"

P0_TABLES = (
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
)

NEW_TABLES = (
    "data_sources",
    "raw_payloads",
    "data_provenance",
    "data_revisions",
    "data_revision_invalidations",
    "ingestion_runs",
    "ingestion_issues",
    "provider_capabilities",
)


def _url(tmp_path: Path, name: str = "migrated.db") -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def _migrate(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def _table_names(url: str) -> set[str]:
    engine = create_engine_from_url(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _column_names(url: str, table_name: str) -> set[str]:
    engine = create_engine_from_url(url)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table_name)}
    finally:
        engine.dispose()


def _build_phase0_database(url: str) -> None:
    """Create a genuine pre-PR-001 database: 15 tables, no verdict, legacy rows."""
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("SQLite >= 3.35 is required to simulate the pre-PR-001 schema")
    engine = create_engine_from_url(url)
    try:
        p0_table_objects = [Base.metadata.tables[name] for name in P0_TABLES]
        Base.metadata.create_all(bind=engine, tables=p0_table_objects)
        with engine.begin() as connection:
            # The live metadata already carries the new column; remove it so this
            # database really has the Phase 0 shape.
            connection.exec_driver_sql("ALTER TABLE risk_checks DROP COLUMN verdict")
            # A real Phase 0 database may already contain risk lineage rows.
            connection.exec_driver_sql(
                "INSERT INTO risk_decisions "
                "(risk_decision_id, order_intent_id, account_id, outcome, "
                " policy_version, decided_at, rule_set_id, rule_set_version, "
                " data_snapshot_id, summary) "
                "VALUES ('risk-legacy', 'intent-legacy', 'acct-legacy', 'PASS', "
                "'0.1.0', '2026-11-05T00:00:00.000000+00:00', NULL, NULL, NULL, '')"
            )
            connection.exec_driver_sql(
                "INSERT INTO risk_checks "
                "(risk_decision_id, check_index, rule_id, passed, message, "
                " measured, threshold) "
                "VALUES ('risk-legacy', 0, 'PRICE_TICK', 1, '', NULL, NULL)"
            )
    finally:
        engine.dispose()


def test_fresh_database_upgrades_to_data_foundation_head(tmp_path: Path) -> None:
    url = _url(tmp_path, "fresh.db")
    config = alembic_config(url)
    command.upgrade(config, "head")

    status = check_schema(url)
    assert status.current_revision == HEAD_REVISION
    assert status.current_revision == head_revision(config)

    engine = create_engine_from_url(url)
    try:
        inspector = inspect(engine)
        existing = set(inspector.get_table_names())
        assert set(NEW_TABLES) <= existing
        assert set(P0_TABLES) <= existing
        assert set(table_names()) <= existing
        assert "verdict" in {column["name"] for column in inspector.get_columns("risk_checks")}

        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            assert str(journal_mode).lower() == "wal"

        invalidation_fks = inspector.get_foreign_keys("data_revision_invalidations")
        assert any(
            foreign_key["referred_table"] == "data_revisions"
            and (foreign_key.get("options") or {}).get("ondelete") == "RESTRICT"
            for foreign_key in invalidation_fks
        )
        provenance_fks = inspector.get_foreign_keys("data_provenance")
        assert any(
            foreign_key["referred_table"] == "data_sources"
            and (foreign_key.get("options") or {}).get("ondelete") == "RESTRICT"
            for foreign_key in provenance_fks
        )
        # The ingestion audit log must be able to name an unregistered source, so
        # it deliberately has no foreign key on source_id.
        assert not inspector.get_foreign_keys("ingestion_runs")
    finally:
        engine.dispose()


def test_downgrade_removes_only_data_foundation_objects(tmp_path: Path) -> None:
    url = _url(tmp_path, "downgrade.db")
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, BASE_P0_REVISION)

    remaining = _table_names(url)
    assert not (set(NEW_TABLES) & remaining)
    assert set(P0_TABLES) <= remaining
    assert "verdict" not in _column_names(url, "risk_checks")
    assert check_schema(url).current_revision == BASE_P0_REVISION

    # Idempotent re-upgrade: the data-foundation objects come back.
    command.upgrade(config, "head")
    assert check_schema(url).current_revision == HEAD_REVISION
    assert set(NEW_TABLES) <= _table_names(url)
    assert "verdict" in _column_names(url, "risk_checks")


def test_existing_phase0_database_gains_data_foundation(
    tmp_path: Path,
) -> None:
    url = _url(tmp_path, "p0.db")
    _build_phase0_database(url)
    assert not (set(NEW_TABLES) & _table_names(url))
    assert "verdict" not in _column_names(url, "risk_checks")

    config = alembic_config(url)
    command.stamp(config, BASE_P0_REVISION)
    command.upgrade(config, "head")

    assert check_schema(url).current_revision == HEAD_REVISION
    remaining = _table_names(url)
    assert set(NEW_TABLES) <= remaining
    assert set(P0_TABLES) <= remaining
    engine = create_engine_from_url(url)
    try:
        verdict_columns = {
            column["name"]: column for column in inspect(engine).get_columns("risk_checks")
        }
    finally:
        engine.dispose()
    assert "verdict" in verdict_columns

    database = Database(url)
    try:
        factory = SqlAlchemyUnitOfWorkFactory(database)
        # The added column defaults existing Phase 0 rows to PASS and reads back.
        with factory() as uow:
            legacy = uow.risk_decisions.get("risk-legacy")
        assert legacy is not None
        assert legacy.checks[0].verdict is RiskVerdict.PASS

        # The newly created data-foundation tables are immediately usable.
        source = DataSource(
            source_id="migrated_source",
            kind=DataSourceKind.LOCAL_FIXTURE,
            display_name="Migrated Fixture",
            registered_at=datetime(2026, 11, 13, 9, 0, tzinfo=MARKET_TZ),
            notes="created after the 0002 migration",
        )
        with factory() as uow:
            assert uow.data_sources.register(source) is True
            uow.commit()
        with factory() as uow:
            assert uow.data_sources.get(source.source_id) == source

        # A rejected attempt naming an unregistered source must be persistable:
        # ingestion_runs.source_id has no foreign key by design.
        unknown_report = IngestionReport.build(
            ingestion_id="ing-unknown-source",
            dataset="financial_facts",
            source_id="never_registered_source",
            started_at=datetime(2026, 11, 13, 9, 0, tzinfo=MARKET_TZ),
            finished_at=datetime(2026, 11, 13, 9, 5, tzinfo=MARKET_TZ),
            received_rows=1,
            accepted_rows=0,
            incomplete_rows=0,
            rejected_rows=1,
            duplicate_rows=0,
            issues=(
                QualityIssue(
                    code=QualityIssueCode.UNKNOWN_SOURCE_IDENTITY,
                    message="data source is not registered",
                    field="source_id",
                ),
            ),
        )
        with factory() as uow:
            assert uow.ingestion_runs.append(unknown_report) is True
            uow.commit()
        with factory() as uow:
            assert uow.ingestion_runs.get("ing-unknown-source") == unknown_report
    finally:
        database.dispose()

    engine = create_engine_from_url(url)
    try:
        with engine.connect() as connection:
            column_info = {
                row[1]: row
                for row in connection.exec_driver_sql("PRAGMA table_info('risk_checks')")
            }
            assert column_info["verdict"][3] == 1  # NOT NULL
            assert "PASS" in str(column_info["verdict"][4])  # server default
            legacy_verdict = connection.exec_driver_sql(
                "SELECT verdict FROM risk_checks WHERE risk_decision_id = 'risk-legacy'"
            ).scalar_one()
            assert legacy_verdict == "PASS"
    finally:
        engine.dispose()


def test_phase0_ledger_replay_still_matches_after_migration(
    tmp_path: Path,
) -> None:
    url = _url(tmp_path, "ledger.db")
    _build_phase0_database(url)
    config = alembic_config(url)
    command.stamp(config, BASE_P0_REVISION)
    command.upgrade(config, "head")

    account = scenario.make_account()
    opening = scenario.make_opening_entry(account)
    settlement = scenario.make_cash_entry(
        scenario.ACCOUNT_ID,
        sequence=2,
        amount=Money(amount=Decimal("-10000.00")),
        entry_type=LedgerEntryType.FILL_SETTLEMENT,
        idempotency_key="fill-1:settlement",
        source_ref="fill-1",
        balance_after=Money(amount=Decimal("90000.00")),
    )
    acquisition = scenario.make_position_acquisition(
        scenario.ACCOUNT_ID,
        sequence=1,
        symbol=scenario.SYMBOL,
        quantity=100,
        unit_cost=Decimal("100.00"),
        idempotency_key="fill-1:position",
        source_ref="fill-1",
    )
    stored_position = scenario.make_position(
        quantity=100,
        available_quantity=100,
        cost_basis="10000.00",
        market_price="100.00",
    )
    snapshot = scenario.make_portfolio_snapshot(
        cash="90000.00",
        position=stored_position,
        realized_pnl="0.00",
    )

    database = Database(url)
    try:
        factory = SqlAlchemyUnitOfWorkFactory(database)
        with factory() as uow:
            uow.accounts.save(account)
            assert uow.cash_ledger.append(opening) is True
            assert uow.cash_ledger.append(settlement) is True
            assert uow.position_ledger.append(acquisition) is True
            uow.portfolio_snapshots.save(snapshot)
            uow.commit()
        with factory() as uow:
            entries = uow.cash_ledger.list_for_account(scenario.ACCOUNT_ID)
            events = uow.position_ledger.list_for_account(scenario.ACCOUNT_ID)
            stored = uow.portfolio_snapshots.latest_for_account(scenario.ACCOUNT_ID)
    finally:
        database.dispose()

    assert stored is not None
    rebuilt = rebuild_account(
        opening_capital=account.initial_capital,
        cash_entries=entries,
        position_events=events,
        as_of=scenario.NEXT_TRADE_DATE,
    )
    assert rebuilt.cash == stored.cash == Money(amount=Decimal("90000.00"))
    rebuilt_position = rebuilt.positions[scenario.SYMBOL]
    stored_symbol_position = stored.position(scenario.SYMBOL)
    assert stored_symbol_position is not None
    assert rebuilt_position.quantity == stored_symbol_position.quantity == 100
    assert rebuilt_position.available_quantity == 100
    assert (
        rebuilt_position.cost_basis
        == stored_symbol_position.cost_basis
        == Money(amount=Decimal("10000.00"))
    )
    assert rebuilt.cash_entry_count == 2
    assert rebuilt.position_event_count == 1
