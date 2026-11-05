# ADR-0001 — Modular monolith with an inward dependency direction

* **Status:** Accepted
* **Date:** 2026-11-05
* **Phase:** Phase 0
* **Spec:** §4 (system architecture), §14

## Context

Phase 0 must deliver a coherent domain, paper ledger, rule engine, risk
controller and agent boundary within one development cycle. Microservices or a
multi-agent swarm would multiply operational surface before a single workflow is
testable (spec §1.3). At the same time the financial logic must be identifiable
and reviewable independently of transport and storage, so it must not be
entangled with FastAPI, SQLAlchemy or a provider SDK.

## Decision

Build a **modular monolith** separated by Python package and domain boundary:

```
apps/          → janus/application/  → janus/domain/
janus/infrastructure/ → implements ports declared in application/domain
```

Enforced rules:

1. `janus.domain.*` may import only the standard library and `pydantic`.
2. `janus.application.*` may import `janus.domain.*` and declares all I/O
   interfaces as `typing.Protocol` ports in `janus/application/ports/`.
3. `janus.infrastructure.*` implements those ports. It is the only layer allowed
   to import SQLAlchemy, Alembic, YAML loaders or provider SDKs.
4. `apps/*` (CLI, FastAPI) talk to the system through
   `janus.application.container.JanusContainer` only — never a repository or a
   session.
5. Composition happens in exactly one place:
   `janus.infrastructure.app_container.build_container`.

## Consequences

* **Positive:** the domain, rules and ledger are unit-testable without a database;
  storage and transport are replaceable; one process to run and debug.
* **Positive:** the agent boundary is a package boundary, which makes "the agent
  cannot execute" a structural property rather than a convention.
* **Cost:** a single deployable; no independent scaling. Acceptable for Phase 0
  and explicitly the point of the decision.
* **Risk:** layer violations creep in via convenience imports. Contained by the
  `tests/security` boundary tests and by keeping composition in one module.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Microservices per component | Operational burden before the core workflow is proven; spec §1.3 non-goal |
| Single flat package | Loses the enforceable agent/risk isolation that is a Phase 0 acceptance criterion |
| Framework-first (FastAPI models as domain) | Couples financial invariants to transport and makes offline simulation awkward |
