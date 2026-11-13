# Database schema

JANUS persists its aggregates in **SQLite** through SQLAlchemy 2.0 declarative
tables (`janus/infrastructure/database/tables.py`) and applies
**Alembic** migrations (`migrations/versions/`). The schema is deliberately
ledger-shaped: append-only history, exact decimal text and foreign keys with
`ON DELETE RESTRICT`.

> **Source of truth.** This document was written from and checked against
> `janus/infrastructure/database/tables.py` and
> `migrations/versions/0002_data_foundation_schema.py` in this working tree. If
> the code changes, the code wins: update this document or report the mismatch.

## Storage conventions

The domain value semantics are preserved by custom column types
(`janus/infrastructure/database/columns.py`):

| Type | Physical storage | Guarantee |
|---|---|---|
| `MoneyText` | `TEXT` | Exact CNY `Money.amount` decimal digits; never `NUMERIC`/`REAL`, so cents cannot be lost to floats. |
| `DecimalText` | `TEXT` | Exact `Decimal` digits (no exponent notation) round-tripped via `format(value, "f")` / `Decimal(str(...))`. |
| `UtcDateTime` | `TEXT` | UTC ISO-8601 with microseconds; naive datetimes are rejected on write and values always load back timezone-aware. |
| `enum_column(...)` | `TEXT` | Persists enum **values** (not names), validated on load; no native enum types. |
| `JsonText` | `TEXT` | Deterministic JSON (`sort_keys`, compact, fail-closed serialiser); enums/Decimal/date handled explicitly. |

Additional conventions:

* One `MetaData` with a naming convention (`ix_`, `uq_`, `fk_`, `pk_`,
  `ck_`), so constraint names are stable across migrations.
* Primary keys are caller-supplied stable ids (`TEXT`); nothing is auto-numbered
  except the append-only `sequence` counters and child-row ordinals.
* Foreign keys use `ON DELETE RESTRICT`: append-only rows cannot be orphaned or
  cascade-deleted.
* SQLite foreign-key enforcement is enabled per connection by the engine
  (`PRAGMA foreign_keys=ON`, plus `journal_mode=WAL`, `busy_timeout=5000`,
  `synchronous=NORMAL` — `janus/infrastructure/database/engine.py`).
* In-memory URLs share one `StaticPool` so every session sees the same database.

## Migration chain

```text
0001_initial  ──►  0002_data_foundation  (head)
```

| Revision | File | What it does |
|---|---|---|
| `0001_initial` | `migrations/versions/0001_initial_schema.py` | Creates the schema from the live `Base.metadata` (`create_all`). Because the metadata includes the PR-001 tables, a **fresh** database already has all 23 tables after this step. |
| `0002_data_foundation` | `migrations/versions/0002_data_foundation_schema.py` | Idempotent, inspector-guarded delta: creates any of the 8 data-foundation tables that are missing, creates their indexes, and adds `risk_checks.verdict` only when absent. On a pre-PR-001 Phase 0 database stamped `0001_initial`, this is the step that adds them. Downgrade drops only the 8 tables and the `verdict` column; P0 tables are never touched. |

`Base.metadata` currently declares **23 tables**: 15 Phase 0 tables + 8
data-foundation tables (verified with `table_names()`, which returns the sorted
table list).

## Phase 0 tables (15)

| Table | Primary key | Key constraints / indexes | Purpose |
|---|---|---|---|
| `instruments` | `symbol` | — | Security master: exchange, board, type, status, listing/delisting dates, lot quantity, ETF flag. |
| `account_ids` | `account_id` | — | Stable account identity so versioned accounts can be referenced by `ON DELETE RESTRICT` FKs. |
| `accounts` | `(account_id, version)` | FK `account_id` → `account_ids` (`RESTRICT`) | Immutable account versions: mode, currency, initial capital/ratio, state, pause reason/time. |
| `cash_ledger_entries` | `entry_id` | FK `account_id`; `UNIQUE(account_id, sequence)`; `UNIQUE(account_id, idempotency_key)` | Append-only cash events with amount, trade date, source ref, balance after. |
| `position_events` | `event_id` | FK `account_id`; `UNIQUE(account_id, sequence)`; `UNIQUE(account_id, idempotency_key)`; `ix(symbol)` | Append-only position events with quantity/cost delta, availability date, realized P&L. |
| `orders` | `order_id` | FK `account_id`; `ix(account_id, status)`; `ix(account_id, target_trade_date)` | Paper-order lifecycle with rule-set/fee-schedule/fill-model/decision/snapshot references. |
| `fills` | `fill_id` | FK `order_id`, FK `account_id`; `UNIQUE(idempotency_key)`; `ix(order_id)`; `ix(account_id, trade_date)` | Immutable fills with price/notional/slippage and nested fee-breakdown header. |
| `fee_lines` | `(fill_id, line_index)` | FK `fill_id` (`RESTRICT`) | Itemised fee components of one fill (component, basis, rate, amount). |
| `risk_decisions` | `risk_decision_id` | `ix(order_intent_id)` | Immutable risk-decision header (outcome, policy version, rule-set/snapshot refs). `account_id` intentionally has **no** FK so fail-closed lineage for unknown accounts is retained. |
| `risk_checks` | `(risk_decision_id, check_index)` | FK `risk_decision_id` (`RESTRICT`) | One check per risk rule: `rule_id`, `verdict`, `passed`, message, measured/threshold. |
| `decision_records` | `decision_id` | `ix(created_at)` | Immutable audit lineage: proposal/strategy/model versions, inputs JSON, thesis/evidence, outcome, executable flag. |
| `data_snapshots` | `snapshot_id` | `ix(created_at)` | Immutable data-provenance bundle (provider, symbols, bar dates/count, content hash, quality). |
| `portfolio_snapshots` | `snapshot_id` | FK `account_id`; `ix(account_id, as_of)` | Immutable valuation header (cash, NAV, realized/unrealized P&L, staleness). |
| `portfolio_positions` | `(snapshot_id, position_index)` | FK `snapshot_id` (`RESTRICT`) | Valued position inside a snapshot. |
| `market_bars` | `(symbol, trade_date)` | `ix(trade_date)` | Daily OHLCV with adjustment/quality/source and the Phase 0 point-in-time fields (`event_time`, `published_at`, `retrieved_at`, `known_at`, effective window). |

`risk_checks.verdict` (enum `RiskVerdict`, `NOT NULL`, `server_default "PASS"`)
is part of the PR-001 delta: existing Phase 0 rows remain readable because the
default backfills them; the migration adds the column only when it is absent.

## Data-foundation tables (8, PR-001)

### 1. `data_sources`

Registered origins of data (providers, exchanges, fixtures, manual/derived sets).
Every provenance record must reference one.

| Column | Type | Notes |
|---|---|---|
| `source_id` | TEXT **PK** | Lower-case identifier (domain validation). |
| `kind` | enum `DataSourceKind` | `MARKET_DATA_PROVIDER`, `EXCHANGE_DISCLOSURE`, `LOCAL_FIXTURE`, `MANUAL_ENTRY`, `DERIVED`. |
| `display_name` | TEXT | Human-readable name. |
| `registered_at` | UTC datetime | Timezone-aware registration time. |
| `notes` | TEXT | Free-form notes. |

### 2. `raw_payloads`

Content-addressed archive of provider responses (PR-001 §4.2).

| Column | Type | Notes |
|---|---|---|
| `payload_id` | TEXT **PK** | `raw-<first 32 hex of content hash>`; content-addressed. |
| `content_hash` | TEXT(64) **UNIQUE** | Lower-case SHA-256 of the payload text; identical bytes cannot create a second archive entry. |
| `byte_size` | INTEGER | UTF-8 byte length (matches payload text; model caps at 8 MiB). |
| `media_type` | TEXT | e.g. `application/json`. |
| `source_id` | TEXT **FK** → `data_sources.source_id` (`RESTRICT`) | Registered origin. |
| `dataset` | TEXT | Logical dataset name. |
| `request_scope` | JSON TEXT | Non-secret request parameters; credential-like keys rejected by the domain model. |
| `retrieved_at`, `ingested_at` | UTC datetime | `ingested_at >= retrieved_at` (domain invariant). |
| `payload_text` | TEXT | The archived payload itself. |

Index: `ix_raw_payloads_source_dataset (source_id, dataset)`.

### 3. `data_provenance`

Immutable provenance for one ingested source record (PR-001 §4.1).

| Column | Type | Notes |
|---|---|---|
| `provenance_id` | TEXT **PK** | Deterministic (content-addressed) id from source/dataset/record/revision/hash/policy/availability evidence. |
| `source_id` | TEXT **FK** → `data_sources.source_id` (`RESTRICT`) | Registered source. |
| `dataset`, `source_record_id`, `source_revision` | TEXT | Source identity; not empty (model validation). |
| `event_at` | UTC datetime | Business event time. |
| `published_at` | UTC datetime **NULL** | Source-claimed publication; must not precede `event_at`. |
| `retrieved_at` | UTC datetime | Fetch time; must not follow `ingested_at`. |
| `ingested_at` | UTC datetime | Persistence time. |
| `system_first_observed_at` | UTC datetime | When JANUS first saw it; never back-dated, never after `retrieved_at`. |
| `public_available_at` | UTC datetime **NULL** | Estimate/verification of public accessibility; absent when precision is `UNKNOWN`. |
| `strategy_usable_at` | UTC datetime **NULL** | Earliest strategy-visible instant; `NULL` means unknown availability (never visible). |
| `availability_basis` | enum `AvailabilityBasis` | Evidence class (source timestamp/date, official schedule, provider timestamp, conservative policy, unknown). |
| `availability_precision` | enum `AvailabilityPrecision` | `INSTANT`, `MINUTE`, `DATE`, `UNKNOWN`. |
| `strategy_usable_at_derivation` | enum `AvailabilityBasis` | Evidence that justified `strategy_usable_at`; must be non-`UNKNOWN` when the value is known. |
| `knowledge_policy_version` | TEXT | Version of the availability policy applied. |
| `payload_hash` | TEXT(64) | SHA-256 of the archived raw payload this record was normalised from. |

Constraints/indexes:

* `UNIQUE uq_data_provenance_identity (source_id, dataset, source_record_id, source_revision, payload_hash, ingested_at)` — one row per **ingestion attempt**. Provenance identity includes `ingested_at`, so re-retrieving identical bytes records a new, auditable attempt; the normalized revision identity deliberately excludes it, so a repeated attempt still maps to the same revision (no duplicate fact);
* `ix_data_provenance_dataset_usable (dataset, strategy_usable_at)` — the point-in-time candidate lookup;
* `ix_data_provenance_payload_hash (payload_hash)` — raw-payload resolution.

### 4. `data_revisions`

Append-only revisions of a logical fact (PR-001 §4.3).

| Column | Type | Notes |
|---|---|---|
| `revision_id` | TEXT **PK** | Content-addressed id (logical key, source revision, payload hash, canonical value, period). Deliberately **excludes** `provenance_id`, so a repeated ingestion attempt maps to the same revision. |
| `logical_key` | TEXT | Stable identity of the fact (e.g. question/period/instrument). |
| `dataset` | TEXT | Logical dataset. |
| `source_id` | TEXT **FK** → `data_sources.source_id` (`RESTRICT`) | Registered source. |
| `subject_symbol` | TEXT **NULL** | Instrument symbol when the fact is instrument-scoped. |
| `revision_sequence` | INTEGER | Per-logical-key sequence; allocated by the ingestion service; never reused. |
| `value_kind` | enum `ValueKind` | `DECIMAL`, `INTEGER`, `TEXT`, `BOOLEAN`, `DATE`, `DATETIME`. |
| `value_text` | TEXT | Canonical lexical value (exact digits for decimals). |
| `value_unit` | TEXT **NULL** | Unit, if any. |
| `value_precision_digits` | INTEGER **NULL** | Declared decimal precision, if any. |
| `period_start`, `period_end` | DATE **NULL** | Business period the fact describes (inclusive end). |
| `effective_from`, `effective_to` | UTC datetime **NULL** | Applicability window of the fact (start-inclusive, end-exclusive). |
| `provenance_id` | TEXT **FK** → `data_provenance.provenance_id` (`RESTRICT`) | Authorising provenance. |
| `payload_hash` | TEXT(64) | Raw payload hash (cross-checked with provenance by the resolver). |
| `source_revision` | TEXT | Provider revision marker. |
| `supersedes_revision_id` | TEXT **NULL** | Previous revision this one corrects (self-reference rejected by the model). |
| `ingested_at` | UTC datetime | Persistence time. |

Constraints/indexes:

* `UNIQUE uq_data_revisions_logical_sequence (logical_key, revision_sequence)` — sequence slots are never reused;
* `UNIQUE uq_data_revisions_identity (logical_key, source_revision, payload_hash)` — re-ingesting the same source revision cannot create a phantom revision;
* `ix_data_revisions_dataset_key (dataset, logical_key)`;
* `ix_data_revisions_dataset_symbol (dataset, subject_symbol)`;
* `ix_data_revisions_provenance (provenance_id)`.

### 5. `data_revision_invalidations`

Append-only statement that a revision must not be selected from an instant
onwards (PR-001 §4.3). The revision row itself is never modified or deleted.

| Column | Type | Notes |
|---|---|---|
| `invalidation_id` | TEXT **PK** | Deterministic id from revision, effective time, reason and policy. |
| `revision_id` | TEXT **FK** → `data_revisions.revision_id` (`RESTRICT`) | Revision being invalidated. |
| `reason` | TEXT | Human-readable reason. |
| `effective_at` | UTC datetime | Queries at/after this instant must exclude the revision; earlier cutoffs still see it. |
| `recorded_at` | UTC datetime | When the invalidation was recorded. |
| `policy_version` | TEXT | Availability/invalidation policy version. |
| `decision_id` | TEXT **NULL** | Optional decision-lineage reference. |

Index: `ix_data_revision_invalidations_revision_effective (revision_id, effective_at)`.

### 6. `ingestion_runs`

Append-only per-run ingestion report header (PR-001 §4.3 / quality contract).

| Column | Type | Notes |
|---|---|---|
| `ingestion_id` | TEXT **PK** | One id per ingestion attempt (`new_id("ing")`; not content-addressed). |
| `dataset` | TEXT | Logical dataset. |
| `source_id` | TEXT (no FK) | Source of the batch. Deliberately **not** a foreign key: an ingestion run is an append-only audit log that must be able to record a rejected attempt naming an unregistered source (`UNKNOWN_SOURCE_IDENTITY`). Referential integrity for accepted data is enforced by the `raw_payloads`/`data_provenance` FKs. |
| `started_at`, `finished_at` | UTC datetime | `finished_at >= started_at`. |
| `received_rows`, `accepted_rows`, `incomplete_rows`, `rejected_rows`, `duplicate_rows` | INTEGER | Must reconcile: `received = accepted + incomplete + rejected + duplicate`. |
| `status` | enum `QualityState` | Derived from counters/issues: `ACCEPTED`, `WARNING`, `INCOMPLETE`, `REJECTED`. |
| `provider_failure` | BOOLEAN | Provider-level failure flag; forces `REJECTED`. |
| `failure_detail` | TEXT | Failure description (empty when none). |
| `payload_hash` | TEXT(64) **NULL** | Hash of the archived payload consumed by the run, when one was archived. |

Index: `ix_ingestion_runs_dataset_started (dataset, started_at)`.

### 7. `ingestion_issues`

Structured data-quality findings attached to an ingestion run (child of
`ingestion_runs`).

| Column | Type | Notes |
|---|---|---|
| `issue_id` | TEXT **PK** | Deterministic: `issue-<sha256(ingestion_id, ordinal, code, logical_key)[:32]>`. |
| `ingestion_id` | TEXT **FK** → `ingestion_runs.ingestion_id` (`RESTRICT`) | Owning run. |
| `ordinal` | INTEGER | Position within the run; deterministic order. |
| `code` | enum `QualityIssueCode` | Closed issue vocabulary (17 codes). |
| `message` | TEXT | Human-readable explanation. |
| `logical_key` | TEXT **NULL** | Affected logical fact, when applicable. |
| `field` | TEXT **NULL** | Affected field name, when applicable. |

Constraint: `UNIQUE uq_ingestion_issues_ingestion_ordinal (ingestion_id, ordinal)`.

### 8. `provider_capabilities`

Append-only provider capability declarations (PR-001 §5 / ADR-0009 companion).

| Column | Type | Notes |
|---|---|---|
| `declaration_id` | TEXT **PK** | `ProviderCapabilities.declaration_id` (deterministic content-addressed id). |
| `provider_id` | TEXT | Provider identifier (e.g. `fixture`, `tushare`). |
| `supported` | JSON TEXT | Sorted JSON list of `DataCapability` values (may be empty). |
| `dataset_versions` | JSON TEXT | Dataset → version mapping (sorted by the domain model). |
| `declared_at` | UTC datetime | Aware declaration time. |
| `declaration_reason` | TEXT | Why the declaration exists. |
| `notes` | TEXT | Free-form, bounded notes. |

Index: `ix_provider_capabilities_provider_declared (provider_id, declared_at)`.

## Foreign-key and integrity summary

```text
data_sources ◄── raw_payloads
      ▲              (source_id, RESTRICT)
      ├────────── data_provenance ──► data_revisions ──► data_revision_invalidations
      │              (provenance_id, RESTRICT)              (revision_id, RESTRICT)
      └────────── (no FK) ingestion_runs ──► ingestion_issues
                                                     (ingestion_id, RESTRICT)
provider_capabilities     (no FK: declarations may precede a registered source)

A rejected ingestion attempt may name an unregistered source, so
`ingestion_runs.source_id` is intentionally not a foreign key.

account_ids ◄── accounts / cash_ledger_entries / position_events / orders /
                fills / portfolio_snapshots
orders ◄── fills ◄── fee_lines
risk_decisions ◄── risk_checks
portfolio_snapshots ◄── portfolio_positions
```

Append-only guarantees:

* The repository ports expose **append/get/list** for raw payloads, provenance,
  revisions, invalidations, ingestion reports and capability declarations;
  there is no update or delete method for those aggregates.
* `ON DELETE RESTRICT` prevents deleting a parent that still has children.
* Revisions correct history by appending a new row (sequence +1), and
  invalidations are separate rows; historical rows are never rewritten.

## Assumptions and limitations

* SQLite has no exact decimal type, so money/decimals are stored as decimal
  TEXT. Exactness is guaranteed, but database-level numeric aggregation is not
  available (aggregation happens in Python with `Decimal`).
* Enum columns are `TEXT` with validation on load (no database-level enum
  constraint; `create_constraint=False`), so validity is enforced by the domain
  model and mapper, not by SQLite.
* `ingestion_runs` uses a generated ingestion id rather than a content hash: two
  byte-identical ingestion attempts are two audit events.
* The 8 data-foundation tables were added by PR-001; the migration is idempotent
  so both a fresh `0001_initial → 0002_data_foundation` run and an upgrade of an
  existing Phase 0 database converge on the same schema.
