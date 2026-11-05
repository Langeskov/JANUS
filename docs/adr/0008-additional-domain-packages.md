# ADR-0008 — Additional domain packages and the `tzdata` dependency

* **Status:** Accepted
* **Date:** 2026-11-05
* **Phase:** Phase 0
* **Spec:** §4.3 (indicative layout), §7.3

## Context

The spec's indicative layout lists `domain/{instruments, market_rules, portfolio,
ledger, orders, risk, strategy}` and `infrastructure/market_data`. Two practical
needs are not covered by that list:

1. `MarketBar`, `DataSnapshot`, `CorporateAction`, `AdjustmentFactor` and the
   data-quality report are **domain** entities (they appear in the spec's own core
   domain model, §5) and must be usable by the domain, application and ports
   layers without importing an infrastructure package.
2. On Windows, CPython ships **no IANA time database**, and `ZoneInfo("Asia/Shanghai")`
   is resolved at import time by the market-calendar code. In a clean `uv` virtual
   environment this raises `ZoneInfoNotFoundError` and every test collection fails.

## Decision

1. Add `janus/domain/market_data/` for market-data domain models and pure quality
   checks. `janus/infrastructure/market_data/` remains the adapter layer
   (`FixtureProvider`, Tushare/AKShare shells).
2. Add `tzdata` as a **runtime** dependency in `pyproject.toml` so `ZoneInfo`
   works on every platform, including a fresh Windows checkout.
3. Keep `DecisionRecord` and `DecisionType` in `janus/domain/strategy/` (decision
   lineage is produced by the proposal/rebalance boundary), and place the
   repository/unit-of-work protocols in `janus/application/ports/`.

The rest of the §4.3 layout is followed as written.

## Consequences

* **Positive:** the core domain model is complete inside the domain layer; ports
  can reference snapshot/bar types without an infrastructure import, preserving
  the ADR-0001 dependency rule.
* **Positive:** a fresh `uv sync` on Windows produces a working test suite — this
  was a real, observed failure before the dependency was added.
* **Cost:** one small extra dependency (~1 MB) and one extra domain package to keep
  in mind when reading the layout section.
* **Risk:** drift between documented layout and reality. Contained by this ADR and
  `docs/CONTRACT.md`.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Put market-data models under `infrastructure/market_data` | Forces the domain and ports to import infrastructure — violates ADR-0001 |
| Require the OS tz database | Breaks the fresh-checkout acceptance criterion on Windows |
| Hard-code UTC+8 instead of `zoneinfo` | Loses DST-correct general handling and hides the real dependency |
