# Market-rules 2026 baseline (Phase 0, P0-D)

Status: **Phase 0 engineering configuration baseline — not a verified legal/regulatory
transcription.** This document records what was registered, where every number came
from, what is deliberately *not* modelled, and how to change a rule-set safely.

Governing spec: `JANUS_Phase0_Spec.md` §2 (market scope and rules), §2.3 (2026 rule
baseline), §6.3 (fees), §11 P0-D, §12 (acceptance criteria), §16 (sources).

## 1. What is registered

| File | Content |
|---|---|
| `config/market_rules/ruleset_cn_2026_07_06.yaml` | `CN-SSE-SZSE-2026.1` (v`2026.1`, **ACTIVE**, effective `2026-07-06T00:00:00+08:00`) |
| `config/market_rules/ruleset_cn_2023_01_01.yaml` | `CN-SSE-SZSE-2023.1` (v`2023.1`, **SUPERSEDED**, effective `2023-01-01` → `2026-07-06`) — replay baseline so effective-date selection is exercised |
| `config/market_rules/fees_2026.yaml` | `CN-EQUITY-DEFAULT`, `CN-ETF-DEFAULT`, both `ESTIMATED`, effective `2023-01-01` → open |
| `config/market_rules/calendar_2026.yaml` | Deterministic calendar `CN-2026-H2`, coverage `2026-07-06 … 2026-12-31` |
| `config/market_rules/codes_switch_2026.yaml` | Documentation-only record of the 2026-07-06 switch (not loaded by code) |
| `data/fixtures/calendar/cn_calendar_2026.json` | JSON mirror of the calendar for fixtures/tests |

Load path: `janus.infrastructure.rulesets.RuleSetRegistry.from_default_config()`
(or `JANUS_MARKET_RULES_DIR` / an explicit `config_dir`).

The registry constructs `MarketRulesEngine` eagerly, so duplicate rule-sets,
overlapping effective windows, a missing `ACTIVE` rule-set, a missing fee schedule,
or a fee schedule that does not cover the rule-set window fail at load time.

## 2. Rule-set profile grid (2026 baseline)

Every profile is effective-dated by its rule-set and captures `exchange`, `board`,
`instrument_type`, `risk_warning_only` and optional explicit `symbols`. The engine
selects the most specific match and **fails closed on a tie**.

| profile_id | Match | Settlement | Tick | Limit | Buy increment / min | Fees |
|---|---|---|---|---|---|---|
| `SSE_MAIN-A_SHARE` | SSE / SSE_MAIN / A_SHARE | T+1 | 0.01 | 10 % | 100 / 100 | `CN-EQUITY-DEFAULT` |
| `SZSE_MAIN-A_SHARE` | SZSE / SZSE_MAIN / A_SHARE | T+1 | 0.01 | 10 % | 100 / 100 | `CN-EQUITY-DEFAULT` |
| `SSE_MAIN-A_SHARE-RISK-WARNING` | SSE / SSE_MAIN / A_SHARE / ST or *ST | T+1 | 0.01 | **5 % (configured baseline)** | 100 / 100 | `CN-EQUITY-DEFAULT` |
| `SZSE_MAIN-A_SHARE-RISK-WARNING` | SZSE / SZSE_MAIN / A_SHARE / ST or *ST | T+1 | 0.01 | **5 % (configured baseline)** | 100 / 100 | `CN-EQUITY-DEFAULT` |
| `SSE_MAIN-EQUITY-ETF` | SSE / SSE_MAIN / DOMESTIC_EQUITY_ETF | T+1 | 0.001 | 10 % | 100 / 100 | `CN-ETF-DEFAULT` |
| `SZSE_MAIN-EQUITY-ETF` | SZSE / SZSE_MAIN / DOMESTIC_EQUITY_ETF | T+1 | 0.001 | 10 % | 100 / 100 | `CN-ETF-DEFAULT` |
| `SSE_MAIN-EQUITY-ETF-T0-FIXTURE` | symbol `510999.SH` only | **T+0** | 0.001 | 10 % | 100 / 100 | `CN-ETF-DEFAULT` |
| `SSE_MAIN-A_SHARE-NEW-LISTING` | SSE / SSE_MAIN / A_SHARE, stage-only | T+1 | 0.01 | **none** (`NEW_LISTING_NO_LIMIT_STAGE`) | 100 / 100 | `CN-EQUITY-DEFAULT` |
| `SZSE_MAIN-A_SHARE-NEW-LISTING` | SZSE / SZSE_MAIN / A_SHARE, stage-only | T+1 | 0.01 | **none** (`NEW_LISTING_NO_LIMIT_STAGE`) | 100 / 100 | `CN-EQUITY-DEFAULT` |

All profiles declare: sells may dispose of odd residual lots
(`sell_quantity_increment: 1`, `odd_lot_sell_must_be_whole_position: true`),
eligible order types `LIMIT` only, validity `DAY` only. Market orders are not
simulated because Phase 0 has no order book.

Explicit sessions per trading day (declared identically in the rule-set and the
calendar):

| Session | Window (Asia/Shanghai) |
|---|---|
| `PRE_OPEN_AUCTION` | 09:15–09:25 |
| `CONTINUOUS` | 09:30–11:30 |
| `CONTINUOUS` | 13:00–14:57 |
| `CLOSING_AUCTION` | 14:57–15:00 |

Execution (`allows_execution`) is limited to continuous and closing-auction
sessions; pre-open auction is represented but not an execution window.

### Market rules that were **not** invented

* The effective date `2026-07-06` and the existence of the SSE/SZSE 2026 revision
  come from spec §2.3 / §16 (SSE/SZSE notices, published 2026-04-24).
* The main-board A-share/equity-ETF grids (10 % limit, 0.01 / 0.001 tick, 100-share
  buy lots, T+1) are the Phase 0 conservative baseline. They are **not** claimed to
  be a complete transcription of the 2026 rule text.
* The main-board risk-warning limit of **5 %** is the **configured baseline**, not a
  verified post-2026 value: the spec states the 2026 revision *changed* the
  main-board risk-warning percentage, but the final value is not settled here. Both
  main boards are configured symmetrically as an explicit assumption. **Verify
  against §16 sources before Phase 1.**
* The 2023 baseline is an honest replay placeholder: it reuses the same Phase 0
  grid and is explicitly labelled as *not* a verified reconstruction of the 2023
  rule text.

## 3. Fee schedules (spec §6.3)

All rates are configuration data with a `source`/`note` on every line. `ESTIMATED`
means: conservative simulation defaults, **not** a personal brokerage quote.

| Component | Equity (`CN-EQUITY-DEFAULT`) | ETF (`CN-ETF-DEFAULT`) |
|---|---|---|
| `BROKER_COMMISSION` (NOTIONAL) | 0.00025 both sides, min CNY 5.00 | 0.00025 both sides, min CNY 5.00 |
| `STAMP_DUTY` (NOTIONAL) | 0.0005 **SELL only** | not modelled (none) |
| `TRANSFER_FEE` (NOTIONAL) | 0.00001 both sides | not modelled |
| `EXCHANGE_HANDLING_FEE` (NOTIONAL) | 0.0000341 both sides | 0.0000341 both sides |
| `SLIPPAGE` | **must not appear** — it is a fill-model concern (§6.2) | same |

Calculator behaviour (`janus.domain.market_rules.fees.compute_fee_breakdown`):

* one line per configured component, `Decimal` only, each amount quantized
  half-up to CNY 0.01;
* when the commission floor binds, the rate-derived amount is kept as
  `BROKER_COMMISSION` and the top-up to the floor is emitted as a separate
  `MIN_COMMISSION` line (total = `max(rate × notional, minimum)`);
* sells are charged sell-only components only (stamp duty never appears on buys);
* a schedule containing a `SLIPPAGE` component raises `ConfigurationError`.

Example (contract-tested): 1 000 shares at CNY 10.00 → buy fees CNY 5.44, sell fees
CNY 10.44; ETF buy 1 000 units at CNY 3.500 → CNY 5.12.

## 4. Calendar assumptions

`CN-2026-H2` covers `2026-07-06 … 2026-12-31` with **every date listed** (179
entries), so a date inside coverage can never fall through a gap. Session data is
explicit per trading day.

Coverage deliberately starts at the `2026-07-06` rule-set boundary:

* any trade date selectable under the **ACTIVE** `CN-SSE-SZSE-2026.1` rule-set is
  also calendar-covered (mixing an ACTIVE rule-set with an uncovered trade date
  fails closed);
* the window contains **83 trading days** up to the Phase 0 reference date
  (`2026-11-05`), so the configured 60-trading-day seasoned-listing check can be
  evaluated from coverage alone. Trade dates before `2026-07-06` are covered by the
  SUPERSEDED replay rule-set but **not** by the calendar; Phase 0 has no verified
  historical calendar, so replay of older dates fails closed.

Assumptions (marked per entry via `source`):

* ordinary weekdays → `PRC_WEEKDAY_TRADING_RULE`;
* Saturday/Sunday → `WEEKEND_RULE` (exchanges are closed on weekends, including
  make-up working days; weekend trading is not modelled);
* assumed holiday closures → `PRC_HOLIDAY_ASSUMED`:
  * `2026-09-25` (Mid-Autumn),
  * `2026-10-01 … 2026-10-07` (National Day golden week, weekday dates).

No PRC statutory holiday falls in July/August, so those weekdays are ordinary
trading days under the weekday rule. **The 2026 official PRC/exchange holiday
schedule has not been verified for this build.** The calendar is deterministic test
data; it must be replaced by an official exchange calendar before any Phase 1 work
that depends on exact settlement/availability dates. Tests that need exact
behaviour use explicit deterministic calendars or this documented fixture, never
"today's date".

## 5. Deliberately not modelled (fail closed instead)

These are known gaps; the engine/registry refuses rather than guessing:

* **After-hours fixed-price session (15:05–15:30)** — not declared anywhere. An
  order that requires it is unsupported, since `session_at` returns `None`.
* **Closing-price/auction mechanics** — only the 14:57–15:00 window is represented;
  no auction order book, no closing-price computation.
* **Intraday behaviour, queue position, locked limit-up/limit-down fill
  plausibility** — fill-model concerns (§6.2), not rules.
* **Universe/eligibility** (ST exclusion for new buys, suspension, seasoning) —
  `EligibilityPolicy`/risk layer, not the rules engine.
* **New-listing stage window length** — the engine cannot derive "N trading days
  since listing" from the rules layer. A caller that *knows* the stage declares
  `resolve(..., new_listing_stage=True)`; otherwise numeric limits apply. Do not
  guess the window from `listing_date`.
* **T+0 products** — real T+0 ETFs are outside the Phase 0 universe. The single
  symbol-scoped T+0 profile for `510999.SH` ("JANUS T+0 ETF FIXTURE") is a
  **test-only rule fixture** and does not represent a real product. `510999.SH` is
  not part of the tradable universe.
* **ChiNext, STAR, BSE, cross-border/bond/gold/money-market/leveraged ETFs,
  convertible bonds, options, futures** — no rule profiles are registered; the
  engine rejects them with `UnsupportedEntityError`.
* **T+0/T+1 product-level nuance for excluded ETF families** — not registered until
  those products are in scope.

## 6. Fail-closed contract

| Situation | Result |
|---|---|
| No rule-set effective at the requested instant | `UnsupportedEntityError` |
| Unknown/unsupported board, instrument type or instrument stage | `UnsupportedEntityError` |
| No matching profile, or equal-specificity tie | `UnsupportedEntityError` |
| Profile without a numeric price limit and without an explicit no-limit reason | `UnsupportedEntityError` |
| Risk-warning security with no declared risk-warning limit | `UnsupportedEntityError` |
| Fee schedule missing from the rule-set / not effective at the trade date | `UnsupportedEntityError` |
| Resolved rules referencing an unregistered rule-set/version | `UnsupportedEntityError` |
| Cash line (`CASH.CNY`) | `UnsupportedEntityError` |
| Non-listed/delisted instrument on the trade date | `UnsupportedEntityError` |
| Duplicate rule-set, overlapping windows, no `ACTIVE` rule-set | `ConfigurationError` |
| Unknown YAML key, missing required field, unknown fee reference, calendar gap, weekend marked trading | `ConfigurationError` with file + field |
| Naive datetime anywhere | `ValidationError` |

## 7. How to update a rule-set (procedure)

1. Capture the official source(s) and publication/effective dates first (spec §16).
2. Add a **new** rule-set document (new `rule_set_id`/`version`, correct
   `effective_from`) instead of editing a historical one; set the old one's
   `status` to `SUPERSEDED` and its `effective_to` to the new `effective_from`.
3. Keep effective windows **half-open** (`effective_to` exclusive) and
   non-overlapping — the engine rejects overlaps at construction.
4. Update/add fee schedules with `status` (`ESTIMATED`/`VERIFIED`), `source_refs`,
   and per-line `source`/`note`. Prefer a new schedule id/version when a rate
   changes.
5. Record the switch in a `codes_switch_*.yaml` document and update this file.
6. Add or adjust tests under `tests/unit/test_market_rules_*.py` and
   `tests/contract/test_ruleset_config*.py`; never weaken a fail-closed test.
7. Verify:
   `uv run pytest tests/unit/test_market_rules_*.py tests/contract/test_ruleset_config*.py -q`
   and `uv run ruff check janus/domain/market_rules janus/infrastructure/rulesets`
   and `uv run mypy janus/domain/market_rules janus/infrastructure/rulesets`.
8. Bump the spec/ADR record if a regulatory fact was confirmed or corrected.

## 8. Open items before Phase 1

1. Verify the 2026-07-06 rule text (limit percentages, tick/lot grids, delayed
   provisions, risk-warning percentage) against the §16 sources.
2. Verify the official 2026 PRC/exchange holiday calendar and replace the assumed
   entries.
3. Replace `ESTIMATED` fee schedules with verified broker/exchange rates and mark
   them `VERIFIED`; confirm ETF fee treatment and commission minimums.
4. Decide whether the after-hours fixed-price session, T+0 ETF families and
   listing-stage windows become modelled sessions/rules.
5. Reconstruct the pre-2026-07-06 baseline from verified historical sources if
   historical-rule research is required.
