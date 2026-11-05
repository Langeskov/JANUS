# ADR-0004 — Append-only ledger with deterministic, idempotent replay

* **Status:** Accepted
* **Date:** 2026-11-05
* **Phase:** Phase 0
* **Spec:** §5.1, §5.2, §12 criteria 3 and 4

## Context

The account state must be auditable and reproducible. If a mutable `cash` column
were the only source of truth, an accounting bug would be indistinguishable from
a data-entry correction, and a re-delivered execution event could double-count
cash or quantity.

## Decision

1. Cash and holdings are **derived** by replaying two append-only streams:
   `CashLedgerEntry` (signed CNY deltas) and `PositionEvent` (signed quantity
   deltas with lot metadata).
2. Every entry carries an `idempotency_key` and a per-account monotonic
   `sequence`. Duplicate delivery is **harmless**: `_dedupe` collapses identical
   re-deliveries *before* sequence validation, and reusing a key with different
   content raises `IdempotencyConflictError`.
3. Replay is pure: `rebuild_account(opening_capital, cash_entries,
   position_events)` needs no clock, rules or database, because each acquisition
   event stores its own `available_date` and `unit_cost`. Realised P&L is summed
   from deduplicated sell events.
4. Position lots are consumed FIFO by `(available_date, acquired_trade_date,
   lot_id)`. Availability follows the resolved settlement rule captured on the
   event, so T+1/T+0 replay identically years later.
5. Cached `balance_after` values are optional and **verified** by replay; a
   mismatch is an `InvariantViolation`.
6. Cash may never go negative in a CNY cash account. Debits that would dip below
   zero fail the replay.
7. Writes happen inside one `UnitOfWork` transaction: parent order row, cash
   entries, position event and fill are committed together.

## Consequences

* **Positive:** acceptance criteria 3 and 4 are enforced by construction and
  proved by `tests/integration/test_ledger_rebuild.py` and
  `tests/integration/test_phase0_acceptance.py`.
* **Positive:** a stored `PortfolioSnapshot` is only a cache; divergence is
  detectable and `janus rebuild` reports it.
* **Cost:** replay cost grows with history; snapshots mitigate this. A
  PostgreSQL materialised view is the Phase 1+ escape hatch.
* **Risk:** sequence gaps now fail a rebuild. That is intended (incomplete
  history must not silently reconcile) and is surfaced as `DataQualityError`.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Mutable balance columns | Not auditable; duplicate delivery corrupts state |
| Event sourcing through an external broker (Kafka) | Vastly out of Phase 0 scope |
| Recomputing lots from a calendar at replay time | Replay would change when rules/calendar change — not reproducible |
