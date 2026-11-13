# JANUS Phase 0 — working contract for parallel contributors

This file is the shared agreement for everyone working in this repository during
Phase 0. Read it before writing code.

Repo root: `D:\Test_area\market\JANUS`
Target runtime: **Python 3.12** (managed by `uv`).
Governing spec: `JANUS_Phase0_Spec.md` (Phase 0 = fixed-capital **paper trading only**).

---

## 1. Toolchain and commands

```powershell
uv sync --locked             # create/refresh .venv incl. dev tooling (Lead does this)
uv run pytest -q             # ALL tests
uv run pytest tests/unit -q  # one directory
uv run ruff check .          # lint
uv run ruff format .         # format (optional, keep diffs small)
uv run mypy                  # type check janus/ + apps/
```

Never edit `pyproject.toml`, `uv.lock`, or add dependencies. If something is
missing, report it to the Lead instead.

## 2. Non-negotiable engineering rules

1. **Fail closed.** Unknown symbol / unknown rule-set / unknown strategy version /
   unsupported board / stale data / missing reference ⇒ *reject*, never guess.
   Use the error types in `janus.domain.common.errors`.
2. **Money is `Decimal`.** Never `float` in domain or accounting code. Use
   `janus.domain.common.money.Money` / `as_decimal` / `quantize_cny`.
3. **Time is timezone-aware.** Shanghai market tz is
   `janus.domain.common.time.MARKET_TZ`. Naive datetimes are rejected.
4. **Pydantic models are `frozen=True, extra="forbid"`** for value objects and
   records. Use `model_config = ConfigDict(frozen=True, extra="forbid")`.
5. **Layer direction**: `apps -> application -> domain`, and
   `infrastructure -> domain ports`. `janus.domain.*` must not import FastAPI,
   SQLAlchemy, YAML loaders, LLM SDKs, provider SDKs, or `apps.*`.
6. **Fully typed.** `mypy` runs with `disallow_untyped_defs=true` over `janus/`
   and `apps/`. Annotate every function/method (tests are exempt).
7. **Stable ids only.** New identifiers come from `janus.domain.common.ids.new_id`.
8. **No secrets, no network in Phase 0 tests.** Everything deterministic, offline.

## 3. Domain foundation API (owned by the Lead — do not modify)

### `janus.domain.common`

| Module | Key exports |
|---|---|
| `errors` | `JanusError`, `ErrorCode`, `ValidationError`, `InvariantViolation`, `NotFoundError`, `UnsupportedEntityError`, `DataQualityError`, `StaleDataError`, `RiskRejection`, `InvalidTransitionError`, `IdempotencyConflictError`, `SecurityViolationError`, `ConfigurationError`, `AccountPausedError` |
| `enums` | `Exchange`, `Board`, `InstrumentType`, `SecurityStatus`, `SettlementRule`, `SessionType`, `TradingDayType`, `OrderSide`, `OrderType`, `OrderValidity`, `OrderStatus`, `RiskOutcome`, `AccountState`, `DataQualityStatus`, `AdjustmentMode`, `QuoteSource`, `FeeComponent` |
| `money` | `Money`, `CNY`, `CASH_SYMBOL`, `ZERO_CNY`, `as_decimal`, `quantize_cny`, `bps_to_rate` |
| `time` | `MARKET_TZ`, `PointInTime`, `utc_now`, `market_now`, `to_utc`, `to_market_tz`, `market_date`, `parse_iso`, `ensure_aware` |
| `ids` | `new_id(prefix)`, `ensure_id(value, field)` |

### `janus.domain.instruments`

```python
Instrument(symbol, name, exchange, board, instrument_type, status, listing_date, ...)
Instrument.cash() -> Instrument
validate_symbol("600519.SH") -> str ; make_symbol(code, Exchange) -> str ; exchange_of(sym)
UniversePolicy(...) ; EligibilityPolicy(policy).evaluate(instrument, as_of, listed_trading_days=..., suspended=...) -> EligibilityResult
```

### `janus.domain.market_rules` (models + calendar owned by Lead)

```python
TradingSession(session_type, start_time, end_time)
TradingDay(trade_date, day_type, sessions)
TradingCalendar(days)  # .is_trading_day, .ensure_trading_day, .sessions_on,
# .next_trading_day, .previous_trading_day, .trading_days_between,
# .count_trading_days, .settlement_available_date, .session_at(datetime)
RuleProfile(...)  # match dims + settlement_rule, tick_size, price_limit_pct,
# buy_quantity_increment, min_buy_quantity, allowed_order_types,
# allowed_validities, fee_schedule_id
FeeRate(component, basis, rate, applicable_sides, min_amount, max_amount)
FeeSchedule(fee_schedule_id, version, status, effective_from, effective_to, lines)
FeeBreakdown(fee_schedule_id, fee_schedule_version, side, notional, lines)
TradingRuleSet(rule_set_id, version, effective_from, ..., default_sessions, profiles, fee_schedules)
ResolvedTradingRules(...)  # .compute_price_limits(prev_close, is_risk_warning=...),
# .allows(order_type, validity), .session_at(time)
```

### `janus.domain.portfolio`

`PortfolioAccount` (`create_paper_account`), `PositionView`, `Position`,
`PortfolioSnapshot` (`empty`), `TargetWeight`, `TargetAllocation`.

### `janus.domain.ledger`

`CashLedgerEntry`, `PositionEvent`, `RemainingLot`, `OpenLot` and replay in
`janus.domain.ledger.replay`: `replay_cash`, `replay_positions`,
`rebuild_account`, `available_quantity_on`, `sellable_lots_on`.

### `janus.domain.orders`

`OrderIntent`, `PaperOrder`, `ExecutionFill`, `FILL_MODEL_VERSION`,
`janus.domain.orders.state_machine`: `ALLOWED_TRANSITIONS`, `can_transition`,
`assert_transition`, `transition`, `apply_fill`, `GATEWAY_ADMITTED`.

### `janus.domain.risk`

`RiskPolicy`, `ExecutionPolicy`, `RiskControls`, `RiskLimits`, `DrawdownPolicy`,
`RiskRuleId`, `RiskCheck` (`RiskCheck.ok` / `RiskCheck.failed`), `RiskDecision`.

### `janus.domain.strategy`

`StrategyDefinition`, `StrategyRegistry`, `ParameterBound`, `RegimeClass`,
`AllocationProposal`, `ProposalViolation`, `ProposalValidationResult`,
`validate_proposal(...)`, `find_prohibited_content(text)`.

## 4. Ownership map (write scopes)

| Owner | May write |
|---|---|
| **Lead** | `pyproject.toml`, `uv.lock`, `janus/domain/common/**`, `janus/domain/instruments/**`, `janus/domain/market_rules/models.py`, `janus/domain/market_rules/calendar.py`, `janus/domain/portfolio/**`, `janus/domain/ledger/**`, `janus/domain/orders/**`, `janus/domain/risk/models.py`, `janus/domain/strategy/models.py`, `janus/domain/strategy/proposal.py`, `janus/application/ports/**`, `janus/application/services/{accounting,portfolio,execution}_service.py`, `janus/application/workflows/**`, `janus/infrastructure/paper_execution/**`, `janus/infrastructure/market_data/**`, `tests/simulation/**`, `tests/integration/test_phase0_acceptance.py`, `docs/CONTRACT.md`, `README.md` |
| **market-rules-engine** | `janus/domain/market_rules/{engine,fees}.py`, `janus/domain/market_rules/__init__.py`, `janus/infrastructure/rulesets/**`, `config/market_rules/**`, `data/fixtures/calendar/**`, `tests/unit/test_market_rules*.py`, `tests/contract/test_ruleset_config*.py`, `docs/market_rules/**` |
| **persistence** | `janus/infrastructure/database/**`, `migrations/**`, `tests/integration/test_persistence*.py`, `tests/integration/test_ledger_rebuild*.py` |
| **risk-agent-boundary** | `janus/domain/risk/controller.py`, `janus/application/services/risk_service.py`, `janus/agents/**`, `tests/security/**`, `tests/unit/test_risk*.py`, `tests/unit/test_proposal*.py` |
| **cli-api** | `apps/cli/**`, `apps/api/**`, `janus/infrastructure/observability/**`, `janus/infrastructure/llm/**`, `tests/contract/test_cli*.py`, `tests/contract/test_api*.py` |

Never edit a file outside your scope. If you believe a shared file must change,
message the Lead with the exact patch you need.

## 5. Reporting back

When your scope is complete, report to the Lead:
1. files created/changed;
2. public API you expose (exact signatures);
3. test command + results (`uv run pytest <your tests>`) and counts;
4. `uv run ruff check <your scope>` and `uv run mypy` status for your files;
5. anything the Lead must wire up, and any assumption you made that the spec does
   not settle.
