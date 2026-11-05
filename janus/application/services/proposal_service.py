"""Allocation-proposal validation and decision-lineage recording (spec s3.3, s10, P0-G).

``ProposalService`` is the application-level gate between an agent and the rest
of the system:

``validate``
    Runs the deterministic, fail-closed domain validation
    (:func:`janus.domain.strategy.proposal.validate_proposal`) against trusted
    facts (known/eligible symbols, the closed strategy registry, the evaluation
    clock) and optionally against the persisted data-snapshot repository.
    It returns a :class:`ProposalValidationResult`; a rejection is a normal
    outcome, never an exception.

``record``
    Persists one immutable :class:`~janus.domain.strategy.decision.DecisionRecord`
    holding the *complete* lineage: the proposal JSON, validation result,
    strategy/model/prompt versions, data-snapshot reference, rule-set references
    and account. A proposal record is never ``executable`` -- only a
    risk-approved ``PaperOrder`` is.

Constructor injection keeps every dependency explicit: a unit-of-work factory,
the closed :class:`StrategyRegistry`, and the :class:`Clock`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final

from janus.application.ports.clock import Clock
from janus.application.ports.repositories import UnitOfWorkFactory
from janus.domain.common.ids import ensure_id, new_id
from janus.domain.common.time import ensure_aware
from janus.domain.strategy.decision import DecisionRecord, DecisionType
from janus.domain.strategy.models import StrategyRegistry
from janus.domain.strategy.proposal import (
    AllocationProposal,
    ProposalValidationResult,
    ProposalViolation,
    validate_proposal,
)

__all__ = ["ProposalService"]

_DEFAULT_MAX_PROPOSAL_AGE: Final[timedelta] = timedelta(hours=24)
"""A proposal older than this (relative to the evaluation time) is stale."""

_FUTURE_TOLERANCE: Final[timedelta] = timedelta(minutes=5)
"""Small clock-skew allowance before a proposal/snapshot counts as "future"."""


class ProposalService:
    """Validate allocation proposals and persist their decision lineage."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        registry: StrategyRegistry,
        clock: Clock,
        max_proposal_age: timedelta = _DEFAULT_MAX_PROPOSAL_AGE,
        required_data_snapshot: bool = True,
        verify_snapshot: bool = True,
        known_evidence_refs: Sequence[str] | None = None,
    ) -> None:
        self._uow_factory: UnitOfWorkFactory = uow_factory
        self._registry: StrategyRegistry = registry
        self._clock: Clock = clock
        self._max_proposal_age: timedelta = max_proposal_age
        self._required_data_snapshot: bool = required_data_snapshot
        self._verify_snapshot: bool = verify_snapshot
        self._known_evidence_refs: Sequence[str] | None = known_evidence_refs

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(
        self,
        proposal: AllocationProposal,
        *,
        account_id: str,
        known_symbols: Mapping[str, object] | Sequence[str],
        eligible_symbols: Sequence[str] | None,
        evaluation_time: datetime,
        known_evidence_refs: Sequence[str] | None = None,
        rule_set_id: str | None = None,
        rule_set_version: str | None = None,
    ) -> ProposalValidationResult:
        """Validate ``proposal`` against trusted facts.

        ``account_id`` is validated as a well-formed id for lineage safety; the
        account's trading state is enforced independently by the risk controller.
        ``rule_set_id`` / ``rule_set_version`` are accepted for audit symmetry and
        are recorded by :meth:`record`, not used for validation.
        """
        ensure_id(account_id, "account_id")
        evaluation_time = ensure_aware(evaluation_time, "evaluation_time")
        result = validate_proposal(
            proposal,
            known_symbols=known_symbols,
            eligible_symbols=eligible_symbols,
            registry=self._registry,
            evaluation_time=evaluation_time,
            max_proposal_age=self._max_proposal_age,
            known_evidence_refs=(
                known_evidence_refs
                if known_evidence_refs is not None
                else self._known_evidence_refs
            ),
            required_data_snapshot=self._required_data_snapshot,
        )
        if not self._verify_snapshot or proposal.data_snapshot_id is None:
            return result
        snapshot_violations, snapshot_messages = self._check_snapshot(
            proposal.data_snapshot_id, evaluation_time
        )
        return _merge(result, snapshot_violations, snapshot_messages)

    def _check_snapshot(
        self, snapshot_id: str, evaluation_time: datetime
    ) -> tuple[list[ProposalViolation], list[str]]:
        violations: list[ProposalViolation] = []
        messages: list[str] = []
        with self._uow_factory() as uow:
            snapshot = uow.data_snapshots.get(snapshot_id)
        if snapshot is None:
            violations.append(ProposalViolation.MISSING_DATA_SNAPSHOT)
            messages.append(f"data snapshot {snapshot_id!r} does not exist")
            return violations, messages
        if not snapshot.is_usable():
            violations.append(ProposalViolation.STALE_DATA)
            messages.append(
                f"data snapshot {snapshot_id!r} is not usable (quality={snapshot.quality.value})"
            )
        if snapshot.as_of > evaluation_time + _FUTURE_TOLERANCE:
            violations.append(ProposalViolation.STALE_DATA)
            messages.append(f"data snapshot {snapshot_id!r} is dated in the future")
        elif evaluation_time - snapshot.as_of > self._max_proposal_age:
            violations.append(ProposalViolation.STALE_DATA)
            messages.append(f"data snapshot {snapshot_id!r} is older than the freshness bound")
        return violations, messages

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record(
        self,
        proposal: AllocationProposal,
        validation: ProposalValidationResult,
        *,
        account_id: str,
        rule_set_id: str | None = None,
        rule_set_version: str | None = None,
    ) -> DecisionRecord:
        """Persist the immutable decision record and return it.

        The record is written even for a rejected proposal: the spec requires the
        full structured proposal plus its validation result to be retained for
        audit and replay (spec s10).
        """
        ensure_id(account_id, "account_id")
        now = self._clock.now()
        outcome = "ACCEPTED" if validation.valid else "REJECTED"
        failure_codes = ",".join(validation.reason_codes)
        record = DecisionRecord(
            decision_id=new_id("decision"),
            decision_type=DecisionType.ALLOCATION_PROPOSAL,
            created_at=now,
            as_of=proposal.as_of,
            account_id=account_id,
            proposal_id=proposal.proposal_id,
            strategy_id=proposal.strategy_id,
            strategy_version=proposal.strategy_version,
            model_id=proposal.model_id,
            prompt_version=proposal.prompt_version,
            data_snapshot_id=proposal.data_snapshot_id,
            rule_set_id=rule_set_id,
            rule_set_version=rule_set_version,
            inputs={
                "proposal": proposal.model_dump(mode="json"),
                "validation": validation.model_dump(mode="json"),
                "account_id": account_id,
                "rule_set_id": rule_set_id,
                "rule_set_version": rule_set_version,
            },
            thesis=proposal.thesis,
            evidence_refs=proposal.evidence_refs,
            uncertainties=proposal.uncertainties,
            outcome=f"{outcome}: {failure_codes}" if failure_codes else outcome,
            # A proposal can never execute by itself; only a risk-approved paper
            # order can reach the paper gateway (spec s6.4, s9.10).
            executable=False,
        )
        with self._uow_factory() as uow:
            uow.decisions.append(record)
            uow.commit()
        return record

    def submit(
        self,
        proposal: AllocationProposal,
        *,
        account_id: str,
        known_symbols: Mapping[str, object] | Sequence[str],
        eligible_symbols: Sequence[str] | None,
        evaluation_time: datetime,
        known_evidence_refs: Sequence[str] | None = None,
        rule_set_id: str | None = None,
        rule_set_version: str | None = None,
    ) -> tuple[ProposalValidationResult, DecisionRecord]:
        """Validate then always record; convenience wrapper for the container."""
        validation = self.validate(
            proposal,
            account_id=account_id,
            known_symbols=known_symbols,
            eligible_symbols=eligible_symbols,
            evaluation_time=evaluation_time,
            known_evidence_refs=known_evidence_refs,
            rule_set_id=rule_set_id,
            rule_set_version=rule_set_version,
        )
        record = self.record(
            proposal,
            validation,
            account_id=account_id,
            rule_set_id=rule_set_id,
            rule_set_version=rule_set_version,
        )
        return validation, record


def _merge(
    result: ProposalValidationResult,
    extra_violations: Sequence[ProposalViolation],
    extra_messages: Sequence[str],
) -> ProposalValidationResult:
    """Union of a domain validation result and service-level snapshot findings."""
    if not extra_violations:
        return result
    violations = tuple(dict.fromkeys((*result.violations, *extra_violations)))
    messages = (*result.messages, *extra_messages)
    return ProposalValidationResult.rejected(violations, messages)
