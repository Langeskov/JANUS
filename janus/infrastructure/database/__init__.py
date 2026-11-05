"""SQLAlchemy persistence adapter for the JANUS repository ports.

Public surface the application composition root uses::

    from janus.infrastructure.database import Database, SqlAlchemyUnitOfWorkFactory

    database = Database(database_url)          # or Database.from_url(...)
    database.upgrade_to_head()                 # Alembic migration path
    uow_factory = SqlAlchemyUnitOfWorkFactory(database)

    with uow_factory() as uow:
        account = uow.accounts.get(account_id)
        uow.commit()

Tests/bootstrap may use ``database.create_all()`` instead of migrations.
Migration-state reporting for CI lives in
:mod:`janus.infrastructure.database.schema_check`.
"""

from janus.infrastructure.database.engine import (
    DEFAULT_DATABASE_URL,
    Database,
    create_engine_from_url,
    is_memory_url,
)
from janus.infrastructure.database.unit_of_work import (
    SqlAlchemyUnitOfWork,
    SqlAlchemyUnitOfWorkFactory,
)

__all__ = [
    "DEFAULT_DATABASE_URL",
    "Database",
    "SqlAlchemyUnitOfWork",
    "SqlAlchemyUnitOfWorkFactory",
    "create_engine_from_url",
    "is_memory_url",
]
