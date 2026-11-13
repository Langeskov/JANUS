# ADR-0009 — Temporal data model and point-in-time semantics

* **Status:** Accepted
* **Date:** 2026-11-13
* **Phase:** Phase 1 foundation (PR-001, data foundation)
* **Spec:** §7.3 (point-in-time correctness), §10 (audit/observability); PR-001 §4

## Context

Phase 0 proved the paper-trading loop with deterministic fixtures, where a single
`PointInTime` carrier (`event_time`, `published_at`, `retrieved_at`, `known_at`)
was enough. Real providers and historical research break that simplification:

1. **Different "when"s are not interchangeable.** When an event happened, when a
   source claims it was published, when JANUS fetched it, when JANUS stored it and
   when a strategy is allowed to use it are different facts. Collapsing them into
   one `known_at` is exactly how look-ahead bias enters a backtest: using a fact
   before it was public, or pretending a fact imported today was known years ago.
2. **Historical bulk imports are modern observations.** Downloading 2019 filings
   in 2026 does not mean the system "knew" them in 2019. Back-dating an observation
   timestamp manufactures a historical run that could never have happened.
3. **Publication precision is often a calendar date only.** Filings, reports and
   exchange notices frequently expose a date, not an instant. Inventing midnight,
   market open or close from a date is fabrication. Using the Phase 0 trading
   calendar to infer historical session boundaries would be worse: that calendar
   covers only 2026-07-06..2026-12-31, so applying it to arbitrary history would
   silently guess.
4. **Providers revise data.** Restatements, corrections and vendor revisions mean
   the same logical fact changes over time. Overwriting destroys both the audit
   trail and the ability to answer "what did we believe on date D?".
5. **Unknown availability must not look available.** A fact whose public
   availability cannot be established must be stored (for audit) yet excluded from
   strategy-visible queries — never treated as available "by default".

The constraints are non-negotiable: paper-only, fail-closed, deterministic and
offline-testable (Phase 0 spec §1.3, §9, §12; ADR-0001).

## Decision

### 1. Six distinct time concepts, enforced by separate fields

`janus/domain/data_foundation/provenance.py` (`ProvenanceTimestamps`,
`DataProvenance`) and `janus/domain/data_foundation/availability.py` keep the
following distinct. Their ordering is validated by model validators, not by
convention:

| Field | Meaning | Enforced invariant |
|---|---|---|
| `event_at` | Business time the fact describes (e.g. bar close, quarter end). | Required, timezone-aware. |
| `published_at` | What the **source** claims about the public release time. May be absent. | If present: aware, `published_at >= event_at`. |
| `retrieved_at` | When JANUS fetched the response from the source. | Aware; `retrieved_at <= ingested_at`; the ingestion service also rejects a `retrieved_at` in the future relative to the run's start. |
| `ingested_at` | When JANUS persisted the record. | Aware; never earlier than `retrieved_at`. |
| `system_first_observed_at` | When JANUS first saw this information. | Aware; `system_first_observed_at <= retrieved_at`; a historical bulk import therefore has a **modern** observation time. Never back-dated. |
| `public_available_at` (provenance) | JANUS's estimate/verification of when the information was publicly accessible, at the precision the source actually supplied. | Paired with `availability_basis` + `availability_precision`; must not precede `published_at`; must be absent when precision is `UNKNOWN`. |
| `strategy_usable_at` (provenance) | Earliest instant the knowledge policy permits a strategy query to see the fact. | Derived by `derive_strategy_usable_at`; `None` means unknown; must not precede `public_available_at`; a known value must record its derivation basis. |

The legacy Phase 0 `PointInTime.known_at` remains for existing models; the
provenance bridge `DataProvenance.to_point_in_time()` maps `strategy_usable_at`
into it and **refuses** to bridge a fact whose availability is unknown.

`availability_basis` records the *kind of evidence* (source timestamp, source
publication date, official release schedule, provider timestamp, conservative
policy, unknown) and `AvailabilityBasis.is_verified` is true only for positive
evidence categories. A policy-derived lower bound is therefore visibly **not**
verified availability. `AvailabilityPolicy.policy_version` is copied onto every
provenance row (`knowledge_policy_version`) so an old decision can be reconstructed
after the policy changes.

### 2. Revisions are append-only

`janus/domain/data_foundation/revisions.py`:

* a *logical fact* has a stable `logical_key`; each correction/restatement is a
  **new** `DataRevision`;
* `revision_sequence` is allocated per logical key by the ingestion service
  (`next_revision_sequence`); `(revision_sequence, revision_id)` is the total,
  deterministic ordering within a key;
* `revision_id` is content-addressed (`logical_key`, `source_revision`,
  `payload_hash`, value, period) so re-ingesting the same fact is a duplicate,
  not a phantom revision. It deliberately **excludes** `provenance_id`, because
  provenance identity is per ingestion attempt (`ingested_at`): an attempt must
  be auditable without spawning a second revision of the same fact;
* `supersedes_revision_id` links a correction to its predecessor; it can never
  point at itself;
* `RevisionInvalidation` is a separate append-only record with `effective_at`:
  queries stop selecting the revision from that instant, but the revision row is
  never updated or deleted. It is for "the data is proven wrong and no
  replacement exists";
* the repository ports (`janus/application/ports/data_foundation.py`) expose only
  append/get/list operations for raw payloads, provenance and revisions — there is
  no update or delete method to misuse.

### 3. Historical bulk imports never back-date observation

`system_first_observed_at` is a real system timestamp. The ingestion service
(`janus/application/services/data_ingestion_service.py`) derives it from the
payload's `retrieved_at` (reusing the earlier retrieval time when an identical
payload was already archived), never from `published_at`/`event_at`.

Two observation modes make the consequences explicit
(`janus/domain/data_foundation/query.py`):

* `ObservationMode.AVAILABILITY_ONLY` (default) — a historical fact may be used
  once the knowledge policy says it was public, even if JANUS imported it later.
  This is the standard backtest convention.
* `ObservationMode.SYSTEM_OBSERVED_ONLY` — additionally requires
  `system_first_observed_at <= as_of`. A historical bulk import is invisible to
  historical cutoffs, which is the no-look-ahead diagnostic mode.

### 4. Date-only publication: a conservative lower bound or nothing

`AvailabilityPrecision.DATE` is handled by `AvailabilityPolicy.date_only_mode`:

* `CONSERVATIVE_NEXT_CALENDAR_DAY` (**default**) — `strategy_usable_at` is the
  **next calendar day's 00:00 Asia/Shanghai** (`next_market_day_start`). A fact
  published on date `D` cannot be shown to have been public before `D` ended, so
  `D + 1` 00:00 is a provable lower bound. It may delay availability by up to a
  day; that is accepted as conservative.
* `STRICT_UNKNOWN` — nothing is inferred: `strategy_usable_at is None`,
  `availability_basis = UNKNOWN`. The fact is stored for audit but is never
  strategy-visible.

A date-only value that arrives as an exact datetime is **downgraded** to the day
boundary; the extra sub-day digits are never trusted. No trading calendar is
consulted: the Phase 0 calendar covers only 2026-07-06..2026-12-31, so using it
for arbitrary history would fabricate session boundaries. Calendar-day arithmetic
needs no market calendar at all.

When `strategy_usable_at` comes from this policy, the derivation basis is
`CONSERVATIVE_POLICY`, so downstream code and documentation can distinguish a
policy lower bound from verified availability.

### 5. Point-in-time eligibility is explicit and deterministic

`LoadedRevision.is_eligible` + `select_latest_per_logical_key` define a revision
as visible at `as_of` only when **all** hold:

1. `provenance.strategy_usable_at is not None` **and** `strategy_usable_at <= as_of`
   (boundary inclusive; unknown availability is never visible);
2. under `SYSTEM_OBSERVED_ONLY`, `system_first_observed_at <= as_of`;
3. the revision is not invalidated as of `as_of` (invalidations apply from
   `effective_at`, never retroactively);
4. the caller's `RevisionQueryFilters` match (source, subject, key prefix,
   period-end range, business-time window).

Selection takes the greatest `(strategy_usable_at, revision_sequence, revision_id)`
per logical key and returns results sorted by `logical_key`, so every process and
every storage engine returns the same answer. Storage supplies candidates only
(`DataRevisionQuery`); the eligibility and tie-breaking rules live in the domain
and are applied by `PointInTimeQueryService`.

### 6. Fail closed

The data foundation refuses to guess rather than returning a plausible default:

* unknown precision/basis or missing `public_available_at` → availability
  `None`, excluded from strategy-visible queries;
* naive datetimes at any boundary → `ValidationError` (`ensure_aware`);
* `public_available_at` before `published_at`, `strategy_usable_at` before
  `public_available_at`, `retrieved_at` after `ingested_at`, observation after
  retrieval → `ValidationError`;
* credential material in a raw payload → refused before archiving
  (`find_credential_material` / `require_clean_payload`);
* a provider whose capability is undeclared or unknown → `UnsupportedEntityError`
  (`ProviderCapabilities.require`, `require_capability`,
  `ProviderCapabilityService.require`), never an empty success;
* ingestion counters that do not reconcile, or a reported status that contradicts
  the derived status → `ValidationError` (never a "successful" partial run that
  silently lost rows).

## Consequences

* **Positive:** backtests cannot see a fact before the chosen policy permits it;
  `SYSTEM_OBSERVED_ONLY` makes backfilled history provably invisible to historical
  cutoffs.
* **Positive:** every fact resolves to an archived payload, a provenance record
  and a registered source; revisions and invalidations preserve history instead of
  overwriting it.
* **Positive:** date-only evidence has an honest, documented lower bound
  (`CONSERVATIVE_POLICY`) or is excluded (`STRICT_UNKNOWN`); nothing is described
  as verified availability when it is not.
* **Cost / accepted limitation:** one extra set of timestamps, a knowledge-policy
  version and append-only revision bookkeeping on every dataset. Date-only facts
  may be usable up to a day later than an exact timestamp would allow. Unknown
  availability removes facts from strategy queries entirely.
* **Risk — caller-supplied timestamps:** `retrieved_at`/`published_at` come from
  adapters. Contained by model ordering invariants, the ingestion service's
  future-retrieval check, and the provenance resolver's hash/source verification.
  A wrong but internally consistent timestamp cannot be detected from content
  alone; adapters must be tested per provider contract.
* **Risk — policy drift:** changing `AvailabilityPolicy` changes derived
  availability. Contained by copying `policy_version` onto provenance and by
  storing the derived `strategy_usable_at` with the fact.
* **Tests that prove the decision:**
  `tests/unit/test_data_provenance.py`,
  `tests/unit/test_point_in_time_semantics.py`,
  `tests/unit/test_data_foundation_contracts.py`,
  `tests/unit/test_provider_capabilities_infra.py`,
  `tests/integration/test_data_foundation_ingestion.py`,
  `tests/integration/test_data_foundation_persistence.py` and
  `tests/integration/test_data_foundation_migration.py`.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Keep one `known_at` instant for everything | Conflates event time, publication, retrieval and policy availability; enables look-ahead and destroys auditability |
| Use `published_at` as `strategy_usable_at` | Ignores retrieval/verification and date-only precision; a publication claim provides no proof about accessibility |
| Assume date-only publication means market open/close of that day | Fabricated sub-day precision; the Phase 0 calendar does not cover arbitrary history |
| Treat unknown availability as available | Violates fail-closed; manufactures information that may not have been public |
| Update revisions in place / delete superseded rows | Destroys replayability and point-in-time history; violates the append-only ledger philosophy (ADR-0004) |
| Back-date `system_first_observed_at` to publication time for bulk imports | Creates fake historical observations; defeats `SYSTEM_OBSERVED_ONLY` |
