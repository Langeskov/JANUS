"""Data-foundation schema (PR-001).

Revision ID: 0002_data_foundation
Revises: 0001_initial
Create Date: 2026-11-13

This revision is deliberately **idempotent and inspector-guarded**:

* ``0001_initial`` builds its DDL from the live ``Base.metadata``. On a fresh
  database that means the eight data-foundation tables (and the
  ``risk_checks.verdict`` column) already exist by the time this revision runs,
  so every create/add below is conditional on the current schema.
* On an existing Phase 0 database (stamped ``0001_initial`` but created before
  this revision) the eight tables are missing and ``risk_checks`` has no
  ``verdict`` column; both are added here.

Nothing in this revision touches an existing Phase 0 table other than adding
``risk_checks.verdict``. The downgrade drops only the objects introduced here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from janus.domain.risk.models import RiskVerdict
from janus.infrastructure.database.columns import enum_column
from janus.infrastructure.database.tables import Base

# revision identifiers, used by Alembic.
revision: str = "0002_data_foundation"
down_revision: str | None = "0001_initial"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

NEW_TABLES: tuple[str, ...] = (
    "data_sources",
    "raw_payloads",
    "data_provenance",
    "data_revisions",
    "data_revision_invalidations",
    "ingestion_runs",
    "ingestion_issues",
    "provider_capabilities",
)
"""Creation order respects the foreign keys between the new tables."""

VERDICT_COLUMN = "verdict"
RISK_CHECKS_TABLE = "risk_checks"


def _existing_tables(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def _existing_columns(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    """Create the data-foundation objects that are not already present."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _existing_tables(inspector)

    for table_name in NEW_TABLES:
        table = Base.metadata.tables[table_name]
        if table_name not in existing:
            table.create(bind=bind, checkfirst=True)
        # ``Table.create`` may skip index DDL in some SQLAlchemy versions; create
        # the indexes explicitly so both fresh and P0-stamped paths converge.
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            index.create(bind=bind, checkfirst=True)

    inspector = sa.inspect(bind)
    if RISK_CHECKS_TABLE in _existing_tables(inspector):
        if VERDICT_COLUMN not in _existing_columns(inspector, RISK_CHECKS_TABLE):
            op.add_column(
                RISK_CHECKS_TABLE,
                sa.Column(
                    VERDICT_COLUMN,
                    enum_column(RiskVerdict),
                    nullable=False,
                    server_default="PASS",
                ),
            )


def downgrade() -> None:
    """Drop only the objects introduced by this revision."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _existing_tables(inspector)

    for table_name in reversed(NEW_TABLES):
        if table_name in existing:
            Base.metadata.tables[table_name].drop(bind=bind, checkfirst=True)

    inspector = sa.inspect(bind)
    if RISK_CHECKS_TABLE in _existing_tables(inspector) and (
        VERDICT_COLUMN in _existing_columns(inspector, RISK_CHECKS_TABLE)
    ):
        with op.batch_alter_table(RISK_CHECKS_TABLE) as batch_op:
            batch_op.drop_column(VERDICT_COLUMN)
