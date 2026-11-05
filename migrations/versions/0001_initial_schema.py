"""Initial JANUS Phase 0 schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-05

Creates the complete Phase 0 schema from the declarative metadata in
``janus.infrastructure.database.tables``. Deriving the DDL from the same
``MetaData`` the application uses guarantees that a migrated database and a
``Base.metadata.create_all`` database can never drift apart.

Tables: instruments, account_ids, accounts, cash_ledger_entries,
position_events, orders, fills, fee_lines, risk_decisions, risk_checks,
decision_records, data_snapshots, portfolio_snapshots, portfolio_positions,
market_bars.
"""

from __future__ import annotations

from alembic import op

from janus.infrastructure.database.tables import Base

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create every Phase 0 table, index and constraint."""
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """Drop every Phase 0 table (destructive; use on disposable databases only)."""
    Base.metadata.drop_all(bind=op.get_bind())
