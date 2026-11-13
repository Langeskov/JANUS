# Data foundation (PR-001)

The data foundation makes every fact JANUS uses **explainable, immutable and
point-in-time correct**. It separates four layers so that "what we received",
"when we were allowed to know it" and "what a strategy may use at a historical
cutoff" can never be conflated. The temporal reasoning decision is recorded in
[ADR-0009](adr/0009-temporal-data-model-and-point-in-time-semantics.md); the
physical schema is documented in [`database_schema.md`](database_schema.md).

> **Status.** This document describes the contracts and services implemented in
> PR-001 §4–§5. Live provider integrations are **not** implemented: Tushare and
> AKShare remain disabled shells with empty capability declarations, and all
> tests run offline. Everything below is enforced by code in
> `janus/domain/data_foundation/`, `janus/application/{ports,dto,services}/`, and
> `janus/infrastructure/market_data/`.

## 1. Architecture: four layers plus two registries

```text
provider / fixture adapter
        │  normalised IngestFact values (provider-neutral)
        ▼
┌────────────────────────────────────────────────────────────────────┐
│ 1. RAW ARCHIVE          RawPayload                                  │
│    immutable bytes, SHA-256 content hash, credential screening,     │
│    request scope, retrieval/ingestion timestamps                    │
├────────────────────────────────────────────────────────────────────┤
│ 2. PROVENANCE           DataProvenance + ProvenanceTimestamps       │
│    event/published/retrieved/ingested/observed times, availability  │
│    basis + precision, derived strategy_usable_at, policy version,   │
│    payload hash, deterministic provenance id                        │
├────────────────────────────────────────────────────────────────────┤
│ 3. NORMALISED REVISIONS DataRevision + DataValue + Invalidation     │
│    append-only, content-addressed revisions of one logical fact;    │
│    revision_sequence, supersedes link, effective windows, values    │
├────────────────────────────────────────────────────────────────────┤
│ 4. POINT-IN-TIME QUERIES LoadedRevision / select_latest_per_key     │
│    eligibility at an as-of cutoff, observation mode, invalidation,  │
│    deterministic tie-breaking, filters                              │
└────────────────────────────────────────────────────────────────────┘
```

Two supporting registries make the layers accountable:

* **data sources** (`DataSource`, `DataSourceKind`) — every provenance record
  names a registered source; an unregistered source is a hard ingestion
  rejection.
* **provider capabilities** (`ProviderCapabilities`) — declared, append-only
  support per dataset capability; an undeclared capability is an explicit
  `UnsupportedEntityError`, never an empty result.

Dependency direction is `infrastructure → application ports → domain`
(`janus/domain/data_foundation/**` imports no SQLAlchemy, HTTP client, provider
SDK or YAML loader). Repositories are append-only ports: there is no update or
delete method for raw payloads, provenance, revisions, invalidations, ingestion
reports or capability declarations.

## 2. Layer 1 — raw archive (`janus/domain/data_foundation/raw.py`)

`RawPayload` preserves exactly what the provider returned, once:

| Field | Meaning |
|---|---|
| `payload_id` | `raw-<first 32 hex of the content hash>`: content-addressed, so identical bytes map to the same row (natural deduplication). |
| `content_hash` | Lower-case hex SHA-256 of `payload_text` (UTF-8, no newline translation); revalidated on construction. |
| `byte_size` | UTF-8 byte length; must match `payload_text`; hard cap 8 MiB per payload. |
| `media_type` | Default `application/json`. |
| `source_id` / `dataset` | Registered source and logical dataset name. |
| `request_scope` | Non-secret request parameters (symbol, date range, ...). Credential-like keys (`token`, `secret`, `key`, `password`, `cookie`, `auth`) are rejected; keys/values are bounded and sorted. |
| `retrieved_at` / `ingested_at` | Aware timestamps; `ingested_at >= retrieved_at`. |
| `payload_text` | The archived payload itself. |
| `redaction_findings` | Must be empty for an archived payload; findings refuse the archive instead. |

**Credential screening is a hard gate.** `find_credential_material` detects
authorization headers, bearer tokens, API-key assignments, cookie headers, private
key blocks and password assignments; `RawPayload.archive()` calls
`require_clean_payload()` and raises `ValidationError` rather than persisting a
payload it suspects contains credentials. Redaction happens **before** archiving;
the archive never stores a credential "for later cleaning".

## 3. Layer 2 — provenance (`janus/domain/data_foundation/{provenance,availability}.py`)

`ProvenanceTimestamps` keeps six distinct concepts apart (see ADR-0009 for the full
rationale and invariants):

`event_at`, `published_at`, `retrieved_at`, `ingested_at`,
`system_first_observed_at`, and (on `DataProvenance`) `public_available_at` /
`strategy_usable_at`.

Key rules enforced by model validators:

* every supplied timestamp must be timezone-aware (`ensure_aware`);
* `retrieved_at <= ingested_at`;
* `system_first_observed_at <= retrieved_at` (a bulk import observes *now*, not in
  the past);
* `published_at >= event_at`;
* `public_available_at >= published_at`;
* `strategy_usable_at >= public_available_at`;
* precision `UNKNOWN` ⇒ no `public_available_at` and basis `UNKNOWN`;
* unknown strategy availability must record an `UNKNOWN` derivation basis.

`DataProvenance.build()` derives availability via
`derive_strategy_usable_at()` and then computes a **deterministic**
`provenance_id` from source, dataset, source record, source revision, payload
hash, policy version, availability evidence and `ingested_at`. Provenance
identity is therefore **per ingestion attempt**: re-retrieving identical bytes
records a new, auditable attempt instead of colliding with the previous one.
The normalized revision identity deliberately excludes `ingested_at`, so a
repeated attempt still maps to the same revision (no duplicate fact) — this is
the "raw payload / ingestion attempt / normalized revision" separation required
by PR-001 §4.2.

Availability evidence is classified, not just stored:

| `AvailabilityBasis` | `is_verified` | Meaning |
|---|---|---|
| `SOURCE_TIMESTAMP` | yes | The source gave an exact public release timestamp. |
| `SOURCE_PUBLICATION_DATE` | yes | The source gave a calendar date only. |
| `OFFICIAL_RELEASE_SCHEDULE` | yes | An official, effective-dated schedule fixes the time. |
| `PROVIDER_TIMESTAMP` | yes | The vendor's own timestamp (trusted only as far as the vendor contract). |
| `CONSERVATIVE_POLICY` | no | JANUS derived a deliberately late lower bound. |
| `UNKNOWN` | no | Nothing establishes public availability. |

`AvailabilityPrecision` is `INSTANT`/`MINUTE` (exact), `DATE` (needs the
date-only policy) or `UNKNOWN` (no availability). An exact datetime whose
precision is `DATE` is **downgraded** to the day boundary: sub-day digits supplied
with date-precision evidence are never trusted.

`AvailabilityPolicy` is versioned (`policy_version`, default
`p1a-availability-v1`) and copied to every provenance row as
`knowledge_policy_version`. Its `date_only_mode` is
`CONSERVATIVE_NEXT_CALENDAR_DAY` by default:

* **`CONSERVATIVE_NEXT_CALENDAR_DAY`** — `strategy_usable_at` = next calendar day
  at 00:00 `Asia/Shanghai` (`next_market_day_start`), derivation basis
  `CONSERVATIVE_POLICY`. A fact published on date `D` cannot be proven public
  before `D` ended; no trading calendar is used or needed.
* **`STRICT_UNKNOWN`** — nothing is inferred: `strategy_usable_at` is `None` and
  the fact is excluded from strategy-visible queries.

`DataProvenance` also exposes `has_known_availability`, `is_available_at(at)`
(inclusive boundary; unknown availability is **never** available),
`is_system_observed_at(at)` and `to_point_in_time()` (refuses to bridge an
unknown-availability fact).

## 4. Layer 3 — normalised revisions (`janus/domain/data_foundation/revisions.py`)

A `DataRevision` is one immutable revision of one logical fact. The ingestion
service allocates `revision_sequence` per `logical_key`; the ordering
`(revision_sequence, revision_id)` is total and deterministic. `revision_id` is
content-addressed from the logical key, source revision marker, payload hash,
canonical value, business period and provenance id, so replaying the same source
revision is a duplicate, never a new revision.

`DataValue` stores the exact lexical value plus its kind (`DECIMAL`, `INTEGER`,
`TEXT`, `BOOLEAN`, `DATE`, `DATETIME`) and optional unit/precision digits. Money is
never converted to `float`; decimal facts keep their exact digits.

A `DataRevision` may carry `period_start`/`period_end` (business period) and
`effective_from`/`effective_to` (validity window; start-inclusive,
end-exclusive). `supersedes_revision_id` links a correction to the revision it
replaces and may not reference itself. Revisions are frozen pydantic models.

`RevisionInvalidation` is an append-only statement that a revision must no longer
be selected from `effective_at` onwards (with reason, policy version and optional
decision id). It never mutates or deletes the revision; it is the tool for
"this data is wrong and no replacement exists".

## 5. Layer 4 — point-in-time queries (`janus/domain/data_foundation/query.py`)

`LoadedRevision` pairs a revision with its authorising provenance. At a cutoff
`as_of` a revision is eligible only when:

1. provenance `strategy_usable_at is not None` and `<= as_of`;
2. under `SYSTEM_OBSERVED_ONLY`, `system_first_observed_at <= as_of`;
3. it is not invalidated as of `as_of` (invalidations are never retroactive);
4. `RevisionQueryFilters` match: source, subject symbol, logical-key prefix,
   period-end range, and business-time coverage.

`select_latest_per_logical_key` selects the greatest
`(strategy_usable_at, revision_sequence, revision_id)` per logical key and returns
results sorted by logical key; `revision_ordering_key` is the single shared
ordering function. Storage only supplies candidates (`DataRevisionQuery`); the
temporal rules stay in the domain so they cannot drift per storage engine.

### Observation modes

| Mode | A fact is visible when | Use |
|---|---|---|
| `AVAILABILITY_ONLY` (default) | The knowledge policy says the information was public (`strategy_usable_at <= as_of`), even if JANUS imported it later. | Standard research/backtest convention. |
| `SYSTEM_OBSERVED_ONLY` | The above **and** `system_first_observed_at <= as_of`. | Prove that a backfilled historical import does not manufacture historical observations. |

### Application composition

`janus/application/services/data_foundation_facade.py` exposes
`DataFoundation.from_uow_factory(uow_factory=..., clock=..., policy=...)`: a thin
helper that wires the five data-foundation services (data sources, provider
capabilities, ingestion, point-in-time queries, provenance resolution) against one
unit-of-work factory. It contains no business rules; callers may construct the
individual services directly instead.

## 6. Ingestion pipeline (`janus/application/services/data_ingestion_service.py`)

A provider or fixture adapter normalises its payload into a provider-neutral
`IngestBatch` of `IngestFact` objects (`janus/application/dto/data_foundation.py`).
`DataFoundationIngestionService.ingest(batch)` then runs, inside **one unit of
work / transaction**:

1. **Validate the batch** — `dataset` non-empty; `retrieved_at` aware and not in
   the future relative to the run start.
2. **Deterministic order** — facts are sorted by `(source_record_id,
   logical_key, canonical value)` so identical batches produce identical reports.
3. **Source identity** — `data_sources.exists(batch.source_id)`; an unregistered
   source rejects every row (`UNKNOWN_SOURCE_IDENTITY`, `CRITICAL`).
4. **Capability gate** — when `batch.provider_id` and
   `batch.required_capability` are set, the provider's **latest** declaration must
   support it; otherwise every row is rejected with `UNSUPPORTED_CAPABILITY`
   (`CRITICAL`). Deep failures never look like empty success.
5. **Raw payload archive** — empty payload → `EMPTY_PAYLOAD`; credential material
   → `CREDENTIAL_MATERIAL_DETECTED` (refused, never archived); malformed JSON
   (when `media_type` ends in `json`) → `MALFORMED_PAYLOAD`; otherwise the payload
   is hashed and appended idempotently. The run's
   `system_first_observed_at` is derived from the payload's `retrieved_at`
   (reusing the earlier retrieval if the identical payload already existed) —
   never from a publication time.
6. **Per fact: provenance + revision** — timestamp/precision issues are rejected
   (`INVALID_TIMESTAMP_PRECISION`); duplicate rows within the batch are counted
   (`DUPLICATE_RECORD`); the same `(logical_key, source_revision)` with a
   different value is a conflict (`CONFLICTING_REVISIONS`); an already-stored
   `(source_revision, payload_hash)` is a duplicate; a *known* availability is
   counted accepted, an unknown one is stored but counted `incomplete`
   (`UNKNOWN_AVAILABILITY`); a newly ingested revision that becomes available
   before an earlier one raises `OUT_OF_ORDER_INGESTION` (warning).
7. **Reconciled report** — counters must account for every received row
   (`received == accepted + incomplete + rejected + duplicate`), the status is
   derived (never supplied) and the `IngestionReport` is appended.

The service never raises for *data* problems: it returns an `IngestionOutcome`
(report, archived payload, created revisions, provenance). Programmer errors and
contract violations still raise `ValidationError`. Atomicity comes from the unit
of work: a failure before `commit()` leaves no partial payload/provenance/revision
behind.

## 7. Quality report contract (`janus/domain/data_foundation/quality.py`)

An ingestion run always produces a frozen `IngestionReport` with reconciled
counters and a **derived** `QualityState`:

| State | Derivation (first match wins) |
|---|---|
| `REJECTED` | `provider_failure` is true, or any issue has `CRITICAL` severity. |
| `WARNING` | At least one row was rejected. |
| `INCOMPLETE` | Everything was stored, but at least one fact has unknown strategy availability. |
| `WARNING` | Duplicates or `INFO`/`WARNING` issues only. |
| `ACCEPTED` | `received_rows > 0` and every received row was accepted with known availability and no issues. |
| `WARNING` | `received_rows == 0` (an empty run is not a silent success). |

The `IngestionReport` model validates that `finished_at >= started_at`, that the
counters reconcile exactly, and that the reported status equals the derived
status; a contradiction is a `ValidationError`. Issue codes are a closed,
provider-independent vocabulary (`QualityIssueCode`) with intrinsic severity
(`severity_of`); free-form text is never parsed to decide severity.

Current emitters in the ingestion service (the remaining codes are reserved for
paths that do not exist yet — adapter failure handling, a dataset registry, and a
payload-hash cross-check — and are documented as reserved, not as behaviour):

| Issue code | Severity | Emitted by current ingestion service |
|---|---|---|
| `MISSING_REQUIRED_FIELD` | ERROR | yes |
| `INVALID_FIELD_TYPE` | ERROR | no — reserved |
| `INVALID_TIMESTAMP_PRECISION` | WARNING | yes |
| `DUPLICATE_RECORD` | INFO | yes |
| `INVALID_PAYLOAD_HASH` | CRITICAL | no — reserved (hash validation raises `ValidationError` directly) |
| `UNKNOWN_SOURCE_IDENTITY` | CRITICAL | yes |
| `UNAVAILABLE_PUBLICATION_TIME` | WARNING | yes |
| `UNKNOWN_AVAILABILITY` | WARNING | yes |
| `CONFLICTING_REVISIONS` | ERROR | yes |
| `INCONSISTENT_PROVENANCE_REFERENCE` | ERROR | no — reserved (provenance resolver raises a `DataQualityError`) |
| `CREDENTIAL_MATERIAL_DETECTED` | CRITICAL | yes |
| `EMPTY_PAYLOAD` | ERROR | yes |
| `MALFORMED_PAYLOAD` | CRITICAL | yes |
| `UNSUPPORTED_DATASET` | CRITICAL | no — reserved (no dataset registry yet) |
| `UNSUPPORTED_CAPABILITY` | CRITICAL | yes |
| `PROVIDER_FAILURE` | CRITICAL | no — reserved (the report flag exists; no live-provider wrapper sets it) |
| `OUT_OF_ORDER_INGESTION` | WARNING | yes |

## 8. Provider capability contract

`janus/domain/data_foundation/capabilities.py`:

* `DataSource` — registered origin of data: lower-case `source_id`, `kind`
  (`MARKET_DATA_PROVIDER`, `EXCHANGE_DISCLOSURE`, `LOCAL_FIXTURE`,
  `MANUAL_ENTRY`, `DERIVED`), display name, aware `registered_at`, notes.
* `DataCapability` — the closed dataset vocabulary: `DAILY_BARS`,
  `HISTORICAL_MINUTES`, `REALTIME_QUOTES`, `FINANCIAL_REPORTS`,
  `CORPORATE_ACTIONS`, `ANNOUNCEMENTS`.
* `ProviderCapabilities` — one immutable declaration: `provider_id`,
  `supported: frozenset[DataCapability]`, aware `declared_at`,
  `declaration_reason`, `notes`, `dataset_versions`. A declaration with an empty
  supported set is valid and meaningful ("registered, supplies nothing yet").
  * `supports(capability)` — membership;
  * `require(capability)` — raises `UnsupportedEntityError` with provider,
    capability and supported set;
  * `unsupported()` — vocabulary minus `supported`, sorted;
  * `declaration_id` — deterministic content-addressed id for dedupe/audit.
* `ProviderCapabilityChange` + `capability_change(previous, current)` —
  deterministic diff: `added`/`removed` sorted by capability value, optional
  `previous_declared_at` (so the first declaration is representable), and a
  deterministic `change_id`.

`ProviderCapabilityService` (`janus/application/services/provider_capability_service.py`)
is the application-facing registry: `declare()` appends a declaration (identical
declarations are not duplicated), `latest()`, `require()` (unknown provider or
undeclared capability → `UnsupportedEntityError`), `history()` and `changes()`
(consecutive diffs, newest first).

### Infrastructure declarations (this PR)

| Adapter | Declaration | Notes |
|---|---|---|
| `FixtureProvider` | `{DAILY_BARS}` | Synthetic daily bars generated from `(symbol, trade_date)`; no network, no clock. `declared_at` defaults to the fixed `PHASE0_DECLARATION_AT` and can be injected. |
| `TushareProvider` (disabled shell) | `∅` | Phase 1 only; daily data needs Tushare points, minute/news/announcements need separate permissions. Never claims a capability it cannot supply. |
| `AKShareProvider` (disabled shell) | `∅` | Phase 1 research-only cross-check; public-source data with commercial-use caveats. Never claims a capability it cannot supply. |

The disabled shells expose `capabilities()` as a **classmethod**: they cannot be
constructed while disabled (construction still raises `UnsupportedEntityError`),
yet their empty declaration must remain auditable and registerable.

`require_capability(capabilities, capability)` in
`janus/infrastructure/market_data/capability_gate.py` delegates to
`ProviderCapabilities.require`; adapters call it before serving data so an
unsupported dataset fails closed. `PHASE0_DECLARATION_AT` is a fixed declaration
instant (not a wall-clock reading) so declaration ids are reproducible.

## 9. Revision and invalidation workflow

1. Adapter normalises a provider response into an `IngestBatch` (raw payload text
   plus `IngestFact`s with event/published/public-availability metadata).
2. `DataFoundationIngestionService.ingest()` archives the payload, builds
   provenance, allocates the next revision sequence and appends the new revision.
   A correction to an existing fact becomes **revision N+1** of the same
   `logical_key`; earlier revisions stay untouched.
3. `PointInTimeQueryService.get_as_of(logical_key, as_of)` returns the latest
   eligible revision at the cutoff (default `AVAILABILITY_ONLY`), or `None` when
   nothing was permissible then. `query_as_of(dataset, as_of)` does the same for
   every logical key in a dataset.
4. When data is proven wrong and no replacement exists,
   `PointInTimeQueryService.invalidate(revision_id=..., reason=...,
   effective_at=...)` appends an invalidation. From `effective_at` onwards queries
   ignore the revision; historical cutoffs before `effective_at` still see it, and
   the revision remains in history.
5. `ProvenanceResolver.resolve/resolve_for_revision` re-verifies that a revision's
   provenance exists, its source is registered, its payload is archived and the
   hashes agree; any dangling reference is a `DataQualityError`/`NotFoundError`,
   never a silent pass.

## 10. Unsupported temporal cases and fail-closed behaviour

These cases are deliberately **not** guessed:

| Situation | Behaviour |
|---|---|
| Availability basis or precision unknown | `strategy_usable_at = None`; stored for audit, never strategy-visible. |
| Date-only publication, `CONSERVATIVE_NEXT_CALENDAR_DAY` | Usable from next calendar day 00:00 `Asia/Shanghai`; derivation basis `CONSERVATIVE_POLICY` (**not** verified availability). |
| Date-only publication, `STRICT_UNKNOWN` | Unknown availability; excluded from queries. |
| Date-only evidence supplied as an exact datetime | Downgraded to the day boundary; sub-day digits ignored. |
| Naive datetime anywhere in provenance/query | `ValidationError` (no local-time assumptions). |
| Public availability before publication, availability before public availability, retrieval after ingestion, observation after retrieval | `ValidationError`. |
| Unknown/unregistered data source | Ingestion rejects all rows (`UNKNOWN_SOURCE_IDENTITY`, `CRITICAL`). |
| Provider capability undeclared/unknown | `UnsupportedEntityError` (gate/service) or `UNSUPPORTED_CAPABILITY` rejection (ingestion). |
| Credential material in a raw payload | `ValidationError`/`CREDENTIAL_MATERIAL_DETECTED`; payload refused. |
| Payload larger than 8 MiB | `ValidationError`; refused. |
| Relative ordering of historical publication vs. system observation | Represented by two timestamps; never collapsed. `SYSTEM_OBSERVED_ONLY` enforces system observation where it matters. |
| Historical date-only availability before the covered Phase 0 calendar window | Calendar-day arithmetic only; the market calendar is not consulted, so no session boundary is invented. |
| Revision invalidated with no replacement | Query excludes it from `effective_at`; history is preserved. |

## 11. What is not implemented yet (honest gaps)

* Live provider integrations: Tushare/AKShare remain disabled shells; no network
  call is made in Phase 0/PR-001 tests.
* No dataset registry: `UNSUPPORTED_DATASET` is defined vocabulary but nothing
  rejects an unknown dataset name yet.
* No live-provider failure wrapper: `IngestionReport.provider_failure` is
  supported by the model (and forces `REJECTED`), but the current ingestion
  service never sets it.
* No CLI/API surface for point-in-time queries or ingestion yet; the services are
  importable application-layer APIs.
* Physical persistence is provided by the SQLAlchemy repositories and Alembic
  migration `0002_data_foundation` (see [`database_schema.md`](database_schema.md));
  the domain/application contracts above are storage-engine independent.
