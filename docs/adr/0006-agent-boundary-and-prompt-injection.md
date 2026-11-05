# ADR-0006 — Agent boundary: proposals only, untrusted external content

* **Status:** Accepted
* **Date:** 2026-11-05
* **Phase:** Phase 0
* **Spec:** §3, §8.3, §9, §12 criteria 9–10

## Context

The research agent reads material that other parties control: web pages, filings,
news, PDFs and market-data metadata. Any of it can contain instructions aimed at
the model. A prompt-level "do not do X" rule is not a security control; an LLM
that can call tools can be steered.

## Decision

1. The agent's **only** output is a strict schema: `AllocationProposal`
   (`janus/domain/strategy/proposal.py`). It is not a broker instruction.
2. Untrusted payloads enter through one function,
   `janus.agents.schemas.parse_proposal_payload`, which enforces:
   * a 32 KiB size cap, nesting-depth cap and `extra="forbid"`;
   * a recursive scan for forbidden keys (tool/policy/risk/capital/ledger/
     credential/secret names) raising `SecurityViolationError`;
   * conversion to the domain value object, which independently checks duplicate
     symbols, weights in `[0,1]` and a total within tolerance.
3. Semantic validation (`validate_proposal`) fails closed on unknown or
   ineligible symbols, unregistered/disabled strategy versions, stale proposals,
   future proposals, missing or unknown data snapshots and prohibited directive
   text.
4. Agent classes expose **no** execution, ledger, risk-approval, policy or
   credential capability. `FORBIDDEN_AGENT_CAPABILITIES` is asserted by tests, and
   `ReadOnlyResearchAgent` only reads market data and quotes untrusted text.
5. The deterministic risk controller is the only approval path. Model confidence
   never influences order size or risk limits.
6. The paper gateway accepts only internally generated `PaperOrder` objects in an
   admissible state; raw text raises `SecurityViolationError`.
7. External text is passed to any future model as *quoted data* with a warning,
   never as privileged instruction.

## Consequences

* **Positive:** prompt injection can at worst produce a rejected/odd proposal —
  not an order, not a policy change, not a ledger write.
* **Positive:** every proposal, valid or rejected, is retained with its
  validation result for audit (spec §10).
* **Cost:** the schema is intentionally rigid; genuinely new proposal fields
  require a reviewed schema + prompt version bump.
* **Risk:** a future tool-using agent could reintroduce capability. Contained by
  ADR-0001 layering and the `tests/security` suite, which asserts the absence of
  those capabilities structurally.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Prompt-only guardrails | Not a boundary; trivially bypassed by injected text |
| LLM-as-risk-approver | Violates §8 (deterministic, independent risk controller) |
| Free-form agent JSON with post-hoc parsing | Ambiguous failures; makes fail-closed impossible |
