# JANUS — Phase 0 (paper trading only)

JANUS is a modular portfolio-management system for Mainland China A-shares and
exchange-traded funds. An LLM-assisted research agent may propose a target
allocation, but a **deterministic** rules engine, risk controller, and paper
execution gateway decide whether anything is allowed to happen.

Phase 0 is the foundation release: domain contracts, a versioned market-rules
baseline, an append-only paper ledger, a deterministic paper execution path, a
read-only agent boundary, and this operational CLI/API. It is a **research and
simulation system**, not a trading system.

> ## ⚠️ Phase 0 is paper-only
>
> * **No brokerage integration.** JANUS never connects to a broker, a bank, or a
>   mobile trading app.
> * **No live trading.** There is no `live` mode, no endpoint, and no CLI
>   command that can place a real order or move real money.
> * **No capital changes.** The paper account is created once with a fixed
>   principal and 100% cash. Deposits, withdrawals, top-ups, and balance edits
>   are rejected by design.
> * **No investment advice.** Every strategy, limit, fee, and fixture in this
>   repository is a development artefact for testing behaviour. Nothing here is
>   personalised financial advice or evidence of an optimal portfolio.

---

## Phase 0 scope

Phase 0 must be able to:

1. create a fixed-capital CNY paper account (100% initial cash);
2. ingest deterministic fixture market data (no network);
3. register effective-dated SSE/SZSE rule sets and fee schedules;
4. validate a structured `AllocationProposal` and record its decision lineage;
5. run a daily-frequency paper cycle that turns an accepted proposal into
   risk-checked paper orders and simulated fills;
6. rebuild the account exactly from its immutable ledger;
7. block new paper orders with a kill switch without liquidating positions;
8. expose the whole thing through a CLI and a localhost-only HTTP API;
9. prove all of the above offline with deterministic fixtures.

### Non-goals for Phase 0

- Real brokerage integration, real-money execution, ADB/mobile automation.
- Margin, leverage, short selling, options, futures, convertible bonds,
  securities lending.
- High-frequency or intraday strategies.
- Unconstrained agent-generated Python, shell commands, SQL, or strategy code.
- Multi-agent swarms or microservice deployment (this is a modular monolith).
- A desktop GUI before the domain and paper ledger pass acceptance tests.
- Authentication/authorisation for the local HTTP API (see
  [Security boundaries](#security-boundaries)).

The governing specification is the Phase 0 project specification; the shared
engineering agreement for contributors is [`docs/CONTRACT.md`](docs/CONTRACT.md).

---

## Architecture

A modular monolith, separated by package boundary, not by deployment.

```text
apps/api/janus_api          FastAPI transport (localhost only)
apps/cli/janus_cli          argparse CLI (`janus ...`)
janus/application/          JanusContainer facade + DTOs, ports, services, workflows
janus/domain/               pure domain: instruments, market_rules, portfolio,
                            ledger, orders, risk, strategy, common
janus/infrastructure/       adapters: config loader, database (SQLAlchemy),
                            market_data, rulesets, paper_execution,
                            observability, llm
config/                     reviewed YAML configuration + versioned market rules
migrations/                 Alembic schema migrations
data/fixtures/              deterministic offline fixtures (bars, calendar, proposals)
tests/                      unit, contract, integration, simulation, security
docs/                       contributor contract, and (later) ADRs / rule notes
```

**Dependency direction** (enforced by review and tests):

```text
apps  ->  application  ->  domain
infrastructure  ->  domain ports / application ports
```

- `janus/domain/**` imports no FastAPI, SQLAlchemy, YAML loader, LLM SDK, broker
  SDK, or data-provider SDK.
- `apps/**` talks to the system **only** through
  `janus.application.container.JanusContainer`; it imports no SQLAlchemy and no
  provider SDK.
- Money is `Decimal` inside the domain and a **decimal string** at every
  transport boundary (JSON never contains monetary floats). Naive datetimes are
  rejected; market time is `Asia/Shanghai`.
- Fail closed: an unknown symbol, rule set, strategy version, stale snapshot, or
  unsupported board is a rejection, never a guess.

---

## Quick start

Prerequisites: Python **3.12** and [`uv`](https://docs.astral.sh/uv/). Everything
below is offline and deterministic; no credentials or network are required.

```powershell
# 1. Create/refresh .venv from the lockfile (includes dev tooling)
uv sync --extra dev

# 2. Apply the SQLite schema
uv run alembic upgrade head

# 3. Health check the assembled system
uv run janus health

# 4. Create the configured paper account (idempotent)
uv run janus init-account

# 5. Load deterministic fixture market data
uv run janus ingest-fixtures

# 6. Run one daily paper cycle
uv run janus run-cycle

# 7. Quality gates
uv run pytest
uv run ruff check .
uv run mypy
```

Optional: start the local HTTP API (see [HTTP API](#http-api)):

```powershell
uv run uvicorn janus_api.app:app --host 127.0.0.1 --port 8000
```

> **Point-in-time note (why `run-cycle` can refuse during the session).** JANUS is
> daily-frequency and point-in-time correct: a daily bar is only *known* after the
> close (`15:10 Asia/Shanghai` in the fixture provider). Run the daily cycle after
> the close — or pass an explicit signal date with
> `uv run janus run-cycle --date 2026-11-05`. If you ask it to trade on a bar whose
> `known_at` is still in the future, the risk controller rejects the order with
> `DATA_QUALITY`. That is the intended behaviour, not a bug.

Configuration defaults live in [`config/`](config). They are conservative
development defaults: review them before relying on any behaviour.

### Documentation

| Document | Contents |
|---|---|
| [`docs/charter.md`](docs/charter.md) | Project charter, scope, boundaries, exit condition |
| [`docs/adr/`](docs/adr) | Architecture decision records (start with the index) |
| [`docs/market_rules/`](docs/market_rules) | Market-rule baseline, sources and assumptions |
| [`docs/CONTRACT.md`](docs/CONTRACT.md) | Engineering contract, layering rules, ownership map |

---

## CLI

Entry point: `janus` (`uv run janus ...`), implemented in
[`apps/cli/janus_cli`](apps/cli/janus_cli) with **argparse only** (no click).
Every subcommand is a thin transport: parse → call the container → print.

### Commands

| Command | Purpose |
|---|---|
| `janus health` | Redacted system health report. |
| `janus init-account` | Create the configured fixed-capital paper account (idempotent). |
| `janus status` | Current paper account state, cash, NAV, positions. |
| `janus ingest-fixtures [--days N]` | Load deterministic fixture bars/instruments. |
| `janus propose --file <json>` | Validate a structured allocation proposal; never executes. |
| `janus run-cycle [--date YYYY-MM-DD]` | Run one daily paper cycle (risk-checked paper orders only). |
| `janus kill-switch --reason <text>` | Pause the account; positions are **not** liquidated. |
| `janus resume --reason <text>` | Resume a paused account (administrative, audited). |
| `janus rebuild` | Rebuild the account from its ledger and compare with stored state. |
| `janus audit [--limit N]` | List recent decision/audit lineage. |
| `janus orders [--limit N]` | List recent paper orders. |

### Global options

`--config-dir PATH`, `--db-url URL`, `--json` may be given **before or after**
the subcommand. `--json` emits machine-readable JSON on stdout; without it you
get a human-readable summary. Diagnostics always go to **stderr** (logs never
pollute stdout JSON).

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (including an accepted proposal). |
| `2` | Invalid usage: unknown command, bad `--date`, bad `--limit`, missing option. |
| `3` | Controlled rejection / validation failure: rejected proposal, malformed proposal file, ledger rebuild mismatch, domain rejection. |
| `4` | Internal or configuration error (unexpected failure; never a bare traceback). |

### Examples

```powershell
uv run janus --json health
uv run janus init-account
uv run janus ingest-fixtures --days 20
uv run janus propose --file data/fixtures/proposals/broad_etf_core_v0_1_0.json
uv run janus run-cycle --date 2026-11-05
uv run janus rebuild --json
uv run janus kill-switch --reason "data integrity failure"
```

The CLI never imports SQLAlchemy or a provider SDK (a contract test asserts
this). When no container is injected, it builds the composition root lazily on
first use.

---

## HTTP API

Implemented in [`apps/api/janus_api`](apps/api/janus_api) with FastAPI. Create it
with `create_app(container=...)` for tests, or use the module-level
`janus_api.app:app` with uvicorn.

```powershell
uv run uvicorn janus_api.app:app --host 127.0.0.1 --port 8000
# OpenAPI docs (local only): http://127.0.0.1:8000/docs
```

> **Phase 0 has no authentication. Bind to `127.0.0.1` only.** Never expose the
> server on a LAN interface, publish the port, or put it behind a public reverse
> proxy. Authentication is a later-phase decision requiring its own security
> review.

### Routes

| Method & path | Purpose |
|---|---|
| `GET /health` | Redacted health report (never contains credentials). |
| `GET /account` | Account state + valuation. |
| `POST /admin/init-account` | Create the paper account (idempotent). |
| `POST /proposals` | Validate a structured proposal; a rejection is `422` with violation codes. |
| `POST /cycles/run` | Run one daily paper cycle. Body `{"trade_date": "YYYY-MM-DD"}` optional. |
| `GET /orders` | Recent paper orders (`?limit=`, `?account_id=`). |
| `GET /audit` | Recent decision lineage (`?limit=`). |
| `POST /admin/kill-switch` | Pause the account (`{"reason": "..."}`); no liquidation. |
| `POST /admin/resume` | Resume a paused account (`{"reason": "..."}`). |
| `POST /admin/rebuild` | Rebuild the account from its ledger and compare. |

### Response conventions and error mapping

- **Money and weights are decimal strings** (`"100000.00"`, `"0.20"`), never
  floats. The contract tests assert no float appears in any response.
- Request models use `extra="forbid"`: unknown fields — including
  `mode: "live"`, `add_capital`, `risk_policy_override`, `execute_order`,
  `api_key`, `strategy_code`, `sql` — are rejected with `422` **before** any
  container call, so they cannot create an order.
- Controlled failures map to 4xx: `422` validation/risk/stale-data rejection,
  `409` state conflict (paused account, invalid transition, idempotency),
  `403` security boundary, `404` unknown entity/route. Only unexpected failures
  return `500` (generic body, no traceback, no internal detail).
- `POST /proposals` records a proposal → `200`; a rejected proposal (risk,
  unknown symbol, stale snapshot, prohibited instruction, ...) → `422` with the
  stable violation codes.

---

## Security boundaries

Phase 0 implements the security model as **permission isolation plus
deterministic validation** — not as a prompt-injection classifier.

1. **Paper only.** No brokerage adapter, no live-mode flag, no order-routing
   endpoint. Every order is a simulated paper order.
2. **The agent proposes; it never executes.** An `AllocationProposal` can only
   be validated/recorded. It becomes orders only through the deterministic
   cycle, risk controller, and paper gateway. Agents cannot approve their own
   risk decision, mutate the ledger, or reach execution code.
3. **No capital or policy editing.** There is no command or endpoint to deposit,
   withdraw, top up, change the risk policy, the universe, market rules, or fee
   schedules. Proposals that contain such instructions are rejected.
4. **No generated code, shell, or SQL.** The request schemas have no field for
   strategy source code, shell commands, SQL, or file paths to execute; unknown
   fields are rejected. Phase 0 never executes agent output as code.
5. **Secrets never enter the repository or logs.** Configuration references
   credentials by **environment-variable name only**; `.env` is git-ignored and
   `.env.example` lists names with empty values. Logs/audit lines pass through a
   redactor that strips `*_KEY`, `*_TOKEN`, `*_SECRET`, `authorization` headers,
   `.env`-style assignments, `Bearer` tokens, and credential-shaped strings.
6. **The LLM is disabled by default.** `janus/infrastructure/llm` ships a
   DeepSeek-compatible adapter shell that refuses to construct unless
   `config/providers.yaml` sets `llm.enabled: true`; the API key is read from the
   configured environment variable and never logged or persisted. No Phase 0
   test makes a network call.
7. **No authentication (yet).** The API is a local operational interface. Bind it
   to `127.0.0.1`; do not expose it.
8. **Fail closed.** Missing/stale/contradictory data, unknown rules, unknown
   strategy versions, or unsupported entities reject rather than guess.

---

## Configuration reference

All behaviour is data-driven from [`config/`](config). Edit YAML, not code; a
missing or contradictory file fails closed with a `ConfigurationError`.

### `config/portfolio.yaml`

The fixed-capital paper account: `account.id`, `mode: paper`, `currency: CNY`,
`initial_capital` (default `100000`), `initial_cash_ratio: 1.0`,
`allow_external_flows: false`, `reset_requires_admin: true`, plus
`account_limits`. Non-paper modes, non-CNY currency, external flows, and
non-100%-cash opening are rejected.

### `config/risk_policy.yaml`

The deterministic risk controller's read-only policy: `execution` flags
(`environment: paper_only`, no short/leverage/derivatives/intraday), `controls`
(fresh data, known rule set, decision reference, human review for policy
changes, pause on data-integrity failure, freshness bounds), `limits`
(per-instrument weight, daily turnover, minimum cash weight, orders/day, gross
exposure, sector weight, optional notional cap), and `drawdown` (pause threshold,
never forced liquidation). These are conservative **development defaults**, not
personalised advice.

### `config/universe.yaml`

The tradable universe: `market.exchanges` (`SSE`, `SZSE`), `market.boards`
(main-board only), `instruments.include` (`A_SHARE`, `DOMESTIC_EQUITY_ETF`),
`instruments.exclude` (BSE, STAR, ChiNext, cross-border/bond/money-market/gold/
commodity/leveraged/inverse ETFs, convertible bonds, options, futures), and
`eligibility` (exclude ST, suspended-for-new-orders, delisted; minimum listing
trading days before a buy).

### `config/providers.yaml`

Provider boundaries: `market_data.primary: fixture` (Phase 0 is offline-only),
`market_data.providers.*` shells for Tushare/AKShare with `enabled: false`, and
`llm` (`enabled: false`, `provider: deepseek_compatible`,
`api_key_env: DEEPSEEK_API_KEY`, `execution_tools_enabled: false`, `model_id`,
`prompt_version`). Turning on a live data or LLM provider is a Phase 1 decision
with permission/cost review.

### `config/market_rules/`

Versioned, effective-dated market rules: `ruleset_cn_2026_07_06.yaml` (current
SSE/SZSE baseline), `ruleset_cn_2023_01_01.yaml` (historical baseline),
`fees_2026.yaml`, `calendar_2026.yaml`, and `codes_switch_2026.yaml`. Every
simulated order references the exact rule-set and fee-schedule version used;
unsupported rule combinations are rejections.

### Environment variables

Only **names** are documented, never values. Copy `.env.example` to a local
`.env` only if a later phase enables a provider; Phase 0 needs none.

| Variable | Used by | Phase 0 |
|---|---|---|
| `TUSHARE_TOKEN` | Tushare data adapter (shell) | disabled |
| `DEEPSEEK_API_KEY` | DeepSeek LLM adapter (shell) | disabled |

---

## Observability and audit

- `janus/infrastructure/observability/logging.py`:
  `configure_logging(level, stream=...)`, `get_logger(name)`, and a `Redactor`
  with `redact(text)` / `redact_mapping(dict)`; a logging filter applies
  redaction to every managed handler. Diagnostics go to stderr.
- `janus/infrastructure/observability/audit.py`:
  `AuditTrail.record(decision_type, outcome, executable=..., **fields)` appends
  redacted JSON lines (optional `path=`) and mirrors them to the audit logger.
  Reserved keys (`recorded_at`, `decision_type`, `outcome`, `executable`) cannot
  be overridden by callers.
- `janus/infrastructure/llm/deepseek_adapter.py`: disabled-by-default
  `LLMProvider` shell (httpx used only if `complete()` is ever called by a future
  phase). Construction refuses when the LLM is disabled; `repr` never contains
  the key.

---

## Testing and quality gates

```powershell
uv run pytest                       # full suite
uv run pytest tests/unit -q         # one directory
uv run pytest tests/contract -q     # CLI + API + ruleset contracts
uv run ruff check .                 # lint
uv run mypy                         # type check janus/ + apps/
```

Conventions: tests are deterministic and offline. Domain/accounting behaviour is
proven with fixtures, not live data. `tests/contract/` pins the CLI/API transport
behaviour against an injected `JanusContainer` fake, so transports are tested
without a database. Keep new monetary values in `Decimal`, never `float`.

---

## Repository layout

```text
JANUS/
├── apps/
│   ├── api/janus_api/          FastAPI app, schemas, error mapping
│   └── cli/janus_cli/          argparse CLI, exit-code mapping
├── janus/
│   ├── domain/                 instruments, market_rules, portfolio, ledger,
│   │                           orders, risk, strategy, common
│   ├── application/            container facade + DTOs, ports, services, workflows
│   ├── agents/                 read-only research agent boundary
│   └── infrastructure/         config, database, market_data, rulesets,
│                               paper_execution, observability, llm
├── config/                     reviewed configuration + versioned market rules
├── data/fixtures/              deterministic offline data (bars, calendar, proposals)
├── migrations/                 Alembic migrations
├── tests/                      unit, contract, integration, simulation, security
├── docs/                       contributor contract and documentation
├── .env.example                environment-variable NAMES only (no values)
├── alembic.ini
├── pyproject.toml
└── uv.lock
```

## Contributing

Read [`docs/CONTRACT.md`](docs/CONTRACT.md) first: it defines the ownership map,
the toolchain, and the non-negotiable engineering rules (fail closed, `Decimal`
money, timezone-aware time, `extra="forbid"` models, layer direction, fully
typed code, stable ids, no secrets or network in Phase 0 tests). Never edit
`pyproject.toml`, `uv.lock`, or another owner's files without coordination.
