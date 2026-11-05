# JANUS database migrations

Schema source of truth: `janus/infrastructure/database/tables.py`.
Migrations are applied with Alembic from the repository root:

```powershell
uv run alembic upgrade head      # apply all migrations
uv run alembic current           # show the current revision
uv run alembic downgrade base    # drop the schema (disposable databases only)
uv run python -m janus.infrastructure.database.schema_check   # CI check
```

The target database is resolved from the `JANUS_DB_URL` environment variable,
falling back to `sqlalchemy.url` in `alembic.ini` (a local SQLite file).

Rule: never hand-edit a migrated database. Add a new revision instead. Because
the initial revision is generated from the declarative metadata, changing
`tables.py` requires a new revision whose `upgrade()` performs the same change.
