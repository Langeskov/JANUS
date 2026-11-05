# ADR-0002 — Paper-only, fixed-capital account with no external flows

* **Status:** Accepted
* **Date:** 2026-11-05
* **Phase:** Phase 0
* **Spec:** §1.2, §5.1, §8, §13

## Context

The first release must be a simulation. Any real-money capability, or any way to
change the simulated principal, would turn a research tool into an execution
system by accident. Agents must be structurally unable to add capital.

## Decision

1. An account is created **once** by an administrative action with a fixed CNY
   principal (default `100000`) and `initial_cash_ratio = 1.0`.
2. `PortfolioAccount.mode` accepts only `PAPER`; `LIVE` raises
   `UnsupportedEntityError` at construction.
3. `allow_external_flows` must be `False` and `initial_cash_ratio` must be `1.0`;
   a configuration that tries otherwise is refused at load time.
4. Cash changes only through immutable ledger entries: fills, fees, corporate
   actions, and explicitly authorised administrative events.
5. Resetting an account creates a **new version** and preserves the old audit
   history; it is an administrative workflow, never an agent capability.
6. The kill switch sets `account.state = PAUSED`, blocks new orders, records the
   trigger, and **never liquidates positions**. Resumption is administrative and
   audited.

## Consequences

* **Positive:** "add capital" and "go live" are impossible code paths, not policy
  promises; they are covered by `test_criterion_2_*`, `test_criterion_10_*` and
  `test_criterion_11_*`.
* **Positive:** acceptance criterion 3 (exact rebuild) has a single opening
  event as its anchor.
* **Cost:** no top-ups, no withdrawals — a Phase 1+ administrative workflow will
  be needed, which must itself be audited.
* **Risk:** a future "reset" implementation could become a de-facto top-up.
  Contained by requiring a new account version and a decision record.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Mutable `cash` column as source of truth | Violates the append-only ledger requirement and makes duplicate delivery unsafe |
| Configurable live mode behind a flag | A flag is not a security boundary; Phase 0 must not ship the code path |
| Soft "pause" that also flattens positions | Spec §8.2 explicitly forbids automatic liquidation |
