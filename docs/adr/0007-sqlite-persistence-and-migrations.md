# ADR-0007 — SQLite persistence behind repository ports + Alembic

* **Status:** Accepted
* **Date:** 2026-11-05
* **Phase:** Phase 0
* **Spec:** §10, §11 P0-C

## Context

Phase 0 needs durable, transactional storage for instruments, accounts, the
append-only ledger, orders, fills, decisions, snapshots and bars. It is a
single-user development system now, but must be replaceable by PostgreSQL when
hosted on a NAS later. Fabricating a schema ad hoc would make "a fresh checkout
can install, migrate and run" (acceptance criterion 1) untestable.

## Decision

1. Persistence is implemented only in `janus/infrastructure/database/` against the
   repository protocols in `janus/application/ports/repositories.py`.
2. SQLAlchemy 2.0 declarative tables; `Decimal` money round-trips exactly (stored
   as decimal text/NUMERIC, never float); datetimes round-trip timezone-aware.
3. SQLite is configured with `foreign_keys=ON`, WAL journalling, a busy timeout
   and `synchronous=NORMAL`; the parent directory of a file-backed database is
   created automatically so a fresh checkout works.
4. Schema changes go through **Alembic** (`migrations/`), and application startup
   applies `upgrade head` — so a missing migration fails loudly at boot instead of
   producing confusing runtime errors. `python -m
   janus.infrastructure.database.schema_check` reports current vs head for CI.
5. `SqlAlchemyUnitOfWorkFactory` produces a `UnitOfWork` per transaction exposing
   all eleven repositories; `commit()` is the only durability path.
6. Append-only tables carry unique constraints on `(account_id, sequence)` and
   `idempotency_key`, so duplicate delivery is rejected at the storage layer too.

## Consequences

* **Positive:** the domain and services never import SQLAlchemy; a PostgreSQL
  migration touches infrastructure only.
* **Positive:** migration state is machine-checkable, which is what lets acceptance
  criterion 1 be an automated test.
* **Cost:** SQLite is single-writer; fine for the Phase 0 single-user prototype.
* **Risk:** sequence/idempotency uniqueness relies on the schema being migrated.
  Contained by startup migration + schema check.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| JSON files as storage | No transactions, no constraints, no safe concurrent writes |
| ORM models as domain entities | Couples invariants to storage and breaks offline testing |
| No migrations (create_all at boot) | Cannot evolve schema safely or prove a reproducible checkout |
