# ADR-0005 — Daily-frequency fill model (`next_open_with_slippage_v1`)

* **Status:** Accepted
* **Date:** 2026-11-05
* **Phase:** Phase 0
* **Spec:** §6.1, §6.2, §12 criterion 7

## Context

The first simulator uses daily OHLCV data. Daily bars do **not** reveal intraday
order sequencing, queue priority, or limit-order-book behaviour, so any fill
model is a conservative approximation. It must nevertheless be deterministic,
versioned, and impossible to confuse with a realistic execution simulator.

## Decision

1. One versioned model: `next_open_with_slippage_v1`
   (`janus/infrastructure/paper_execution/fill_model.py`).
2. Reference price is the **next valid session's open**; adverse slippage of
   `5 bps` is applied (buy pays up, sell receives less), rounded against the
   trader to the instrument's tick size.
3. The model refuses to fill when:
   * the order was not submitted (`RISK_APPROVED`/`SUBMITTED_PAPER` only);
   * no bar exists, volume is zero, or the bar is not unadjusted / not quality-OK;
   * the bar date precedes `earliest_execution_date`;
   * the bar date is `<= signal_bar_date` (a day-T signal can never fill on day T);
   * the board is locked at the relevant limit (limit-up blocks buys, limit-down
     blocks sells);
   * the fill price breaches the order's limit price or the daily price limits;
   * the participation-capped quantity rounds below one increment.
4. Participation is capped at a configurable fraction of reported daily volume
   (`10%`) and rounded down to the quantity increment, so partial fills are a
   first-class outcome.
5. Every fill records `fill_model_version`, `slippage_bps`, rule-set/fee-schedule
   versions, the decision, the data snapshot and a deterministic idempotency key.
6. Fees are **not** part of the fill model: they are computed by the versioned,
   effective-dated `FeeSchedule` and stored as an itemised `FeeBreakdown`.

## Consequences

* **Positive:** the "signal bar cannot be the fill bar" rule (acceptance criterion
  7) is testable at both the risk and fill layers.
* **Positive:** pessimistic assumptions make Phase 0 results an upper bound on
  plausible performance rather than an optimistic one.
* **Cost:** no intraday realism; queue position and locked-board fills are
  deliberately modelled as *no fill*.
* **Risk:** a future change to slippage/caps could silently alter historical
  comparisons. Contained by the version string on every fill.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Fill at the signal day's close | Look-ahead bias; forbidden by spec §6.1 |
| Fill whenever `low <= limit <= high` | Assumes intraday ordering daily data cannot prove |
| Market-order model at VWAP | Requires data Phase 0 does not have; implied precision is false |
