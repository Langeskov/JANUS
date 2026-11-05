# JANUS Phase 0 — Project Charter

**Version:** 0.1.0 (Phase 0)
**Market:** Mainland China A-shares and domestic exchange-listed equity ETFs
**Mode:** Fixed-capital **paper trading only**

## 1. Purpose

JANUS is a modular portfolio-management system in which an LLM-assisted research
agent evaluates the market and proposes a target allocation. A deterministic
portfolio engine, rule engine, risk controller and paper-trading gateway enforce
every financial and operational constraint.

Phase 0 is a **research and simulation** release. It never connects to a real
brokerage account, submits real orders, moves funds, or controls a mobile
brokerage app.

## 2. Scope of Phase 0

| In scope | Out of scope (Phase 0 non-goals) |
|---|---|
| Fixed-capital CNY paper account (default CNY 100,000) | Real brokerage integration or real-money execution |
| Versioned SSE/SZSE market-rule registry (2026-07-06 baseline) | ADB / mobile-UI automation |
| Append-only cash + position ledger with exact replay | Margin, leverage, short selling, options, futures, convertible bonds |
| Deterministic risk controller and kill switch | High-frequency / intraday strategies |
| Structured `AllocationProposal` boundary + stub agent | Agent-generated Python, shell, SQL or strategy code |
| Daily-frequency versioned fill model + fees | Multi-agent swarm or microservices |
| CLI and HTTP health/operations surface | Desktop UI before the ledger passes acceptance tests |

## 3. Selected decisions

| Item | Decision |
|---|---|
| Backend | Python 3.12 modular monolith (`apps → application → domain`) |
| Persistence | SQLite via SQLAlchemy/Alembic, repository boundary allows PostgreSQL later |
| Data provider | Deterministic `FixtureProvider`; Tushare/AKShare are disabled Phase 1 shells |
| LLM | DeepSeek-compatible adapter behind a provider port; disabled in Phase 0 |
| Execution | Autonomous **paper** orders only, daily frequency |
| UI | CLI/API first; desktop console later |

## 4. Architectural boundaries

```
apps/            CLI and FastAPI transport (no domain logic)
  ↓
janus/application/   services, workflows, ports (interfaces)
  ↓
janus/domain/        framework-free models, rules, invariants
  ↑
janus/infrastructure/ adapters implementing application/domain ports
```

* `janus.domain` imports no FastAPI, SQLAlchemy, YAML, LLM SDK, broker SDK or
  provider SDK.
* Infrastructure implements ports declared at the application/domain boundary.
* All financial values are `Decimal`; all timestamps are timezone-aware.
* The system **fails closed**: unknown symbol, rule-set, strategy version,
  unsupported board or stale data is a rejection, never a guess.

## 5. Security posture

* Research agents receive read-only data and may emit only a strict, size-limited
  `AllocationProposal`.
* Agents cannot: read credentials or `.env`, execute shell/Python/SQL, modify
  policies/universe/rules/fees, write the ledger or fills, approve their own risk,
  enable live trading, or add capital.
* All external material (web, filings, news, PDFs) is untrusted **data**, never
  instructions. Prompt-injection classifiers are a signal, not a boundary;
  permission isolation and deterministic validation are the boundary.
* Secrets live outside the repository, are referenced by environment-variable
  name only, and are redacted from logs.

## 6. Phase 0 exit condition

JANUS can create a fixed-capital paper account, ingest deterministic fixture
data, validate a structured allocation proposal, reject invalid/stale/unsupported
proposals, and preserve a fully auditable account/decision history — without any
connection to a real brokerage and without granting the agent execution
privileges.

Acceptance criteria and their tests: `tests/integration/test_phase0_acceptance.py`
(one test group per criterion in spec §12).

## 7. Explicitly deferred decisions

Target-universe construction, exact paper capital overrides, paid data provider
and budget, exact commission/slippage calibration, whether Phase 1 includes
fundamentals, strategy registry/rebalance cadence, ChiNext/STAR/BSE/T+0/after-hours
support, and hosting topology (local PC vs NAS).

**Live trading is never a natural consequence of completing Phase 0–5.** It
requires separate design, security and broker/API review plus explicit
authorization.
