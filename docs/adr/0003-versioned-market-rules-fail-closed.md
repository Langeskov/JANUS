# ADR-0003 — Versioned, effective-dated market rules that fail closed

* **Status:** Accepted
* **Date:** 2026-11-05
* **Phase:** Phase 0
* **Spec:** §2.2, §2.3, §11 P0-D, §16

## Context

Price limits, tick size, quantity increments, settlement (T+0/T+1), sessions and
fees vary by exchange, board, instrument type, security status, listing stage and
**effective date**. Hard-coding one global percentage would be wrong and would
silently produce invalid simulated orders. The SSE and SZSE 2026 revised trading
rules took effect on **2026-07-06**, so a 2023 description is not current.

## Decision

1. Rules live in versioned, effective-dated **data** (`config/market_rules/`),
   loaded into `TradingRuleSet` → `RuleProfile` → `ResolvedTradingRules`.
2. `MarketRulesEngine.resolve(instrument, trade_date)` selects the rule set
   effective at that instant and the most specific matching profile (symbol ≻
   risk-warning ≻ board/type ≻ wildcard).
3. Resolution is **fail-closed**: no matching profile, no effective rule set,
   ambiguous windows, or a missing fee schedule raise `UnsupportedEntityError`.
   The engine never guesses a default percentage or lot size.
4. Both the 2026-07-06 baseline and a pre-2026 baseline are registered so
   effective-date switching is exercised. Every rule set records source
   references, publication status and notes about delayed provisions.
5. Fee schedules are separately represented components (commission with a
   minimum, stamp duty on sells, transfer fee, handling/regulatory fee) with
   `ESTIMATED`/`VERIFIED` status — never one unexplained percentage.
6. T+0 is modelled only as an **explicit symbol-scoped rule fixture**
   (`510999.SH`); real T+0 product families (cross-border/gold ETFs) are excluded
   from the Phase 0 universe.

## Consequences

* **Positive:** rule changes are reviewable data diffs with sources, not code
  edits scattered across strategies.
* **Positive:** unsupported combinations cannot produce orders (see
  `test_criterion_8_unsupported_rule_combination_fails_closed`).
* **Cost:** the calendar covers a bounded window (2026-07-06 → 2026-12-31);
  outside it the system refuses rather than assuming weekdays.
* **Risk:** an unverified regulatory number could be treated as authoritative.
  Contained by `source_refs`, `ESTIMATED` status and `docs/market_rules/`.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Global constants (one lot size, one limit %) | Wrong across boards/products and invisible to review |
| Rule logic inside strategy code | Makes risk checks unreproducible and unauditable |
| Fetch rules live from exchange websites at runtime | Untrusted input and non-deterministic tests |
