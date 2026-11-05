# Architecture decision records

One record per decision that a future maintainer would otherwise have to
re-derive from the code. Records are immutable: a changed decision gets a new
ADR and the old one is marked *Superseded by ADR-NNNN*.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-modular-monolith-dependency-direction.md) | Modular monolith with an inward dependency direction | Accepted |
| [0002](0002-paper-only-fixed-capital-account.md) | Paper-only, fixed-capital account with no external flows | Accepted |
| [0003](0003-versioned-market-rules-fail-closed.md) | Versioned, effective-dated market rules that fail closed | Accepted |
| [0004](0004-append-only-ledger-and-replay.md) | Append-only ledger with deterministic, idempotent replay | Accepted |
| [0005](0005-daily-frequency-fill-model.md) | Daily-frequency fill model (`next_open_with_slippage_v1`) | Accepted |
| [0006](0006-agent-boundary-and-prompt-injection.md) | Agent boundary: proposals only, untrusted external content | Accepted |
| [0007](0007-sqlite-persistence-and-migrations.md) | SQLite persistence behind repository ports + Alembic | Accepted |
| [0008](0008-additional-domain-packages.md) | Additional domain packages (`market_data`) and `tzdata` dependency | Accepted |

New records use [`template.md`](template.md).
