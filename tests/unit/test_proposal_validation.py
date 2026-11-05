"""Proposal-schema, proposal-validation and decision-lineage tests (spec s3.3, s10).

The strict agent boundary turns malformed/oversized/dangerous payloads into
``SecurityViolationError``; semantic problems (unknown symbol, unregistered
strategy, stale data) become a ``ProposalValidationResult`` with stable violation
codes. Either way, nothing becomes executable.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from janus.agents import FixtureAgent, load_fixture_payload, parse_proposal_payload
from janus.application.ports.clock import FixedClock
from janus.application.services.proposal_service import ProposalService
from janus.domain.common.enums import DataQualityStatus, QuoteSource
from janus.domain.common.errors import SecurityViolationError, ValidationError
from janus.domain.common.time import MARKET_TZ
from janus.domain.market_data.models import DataSnapshot
from janus.domain.strategy.decision import DecisionRecord, DecisionType
from janus.domain.strategy.models import StrategyDefinition, StrategyRegistry
from janus.domain.strategy.proposal import (
    AllocationProposal,
    ProposalValidationResult,
    ProposalViolation,
)

FIXTURES = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "proposals"
ACCOUNT_ID = "janus-paper-cny-001"
SYMBOL = "510300.SH"
NOW = datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ)
T_DATE = date(2026, 11, 5)
SNAPSHOT_ID = "dsnap-fixture-0001"
RULE_SET_ID = "SSE-SZSE-2026"
RULE_SET_VERSION = "2026.07.06"


# ---------------------------------------------------------------------------
# Fake persistence for ProposalService
# ---------------------------------------------------------------------------
class _DataSnapshots:
    def __init__(self, snapshot: DataSnapshot | None) -> None:
        self._snapshot = snapshot

    def get(self, snapshot_id: str) -> DataSnapshot | None:
        if self._snapshot is not None and self._snapshot.snapshot_id == snapshot_id:
            return self._snapshot
        return None

    def save(self, snapshot: DataSnapshot) -> None:
        self._snapshot = snapshot


class _Decisions:
    def __init__(self) -> None:
        self.records: list[DecisionRecord] = []

    def append(self, record: DecisionRecord) -> None:
        self.records.append(record)


class FakeUnitOfWork:
    def __init__(self, snapshot: DataSnapshot | None) -> None:
        self.data_snapshots = _DataSnapshots(snapshot)
        self.decisions = _Decisions()
        self.commits = 0

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


class FakeUoWFactory:
    def __init__(self, uow: FakeUnitOfWork) -> None:
        self._uow = uow

    def __call__(self) -> FakeUnitOfWork:
        return self._uow


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _snapshot(
    *,
    quality: DataQualityStatus = DataQualityStatus.OK,
    as_of: datetime | None = None,
) -> DataSnapshot:
    return DataSnapshot(
        snapshot_id=SNAPSHOT_ID,
        created_at=NOW,
        as_of=as_of or (NOW - timedelta(minutes=5)),
        provider=QuoteSource.FIXTURE,
        symbols=(SYMBOL,),
        bar_dates=(T_DATE,),
        bar_count=1,
        content_hash="sha256:fixture",
        quality=quality,
    )


def _registry() -> StrategyRegistry:
    return StrategyRegistry(
        (
            StrategyDefinition(
                strategy_id="broad_etf_core",
                version="0.1.0",
                name="Broad ETF core",
                description="Fixture baseline strategy used only by tests.",
            ),
        )
    )


def _uow(snapshot: DataSnapshot | None = None) -> FakeUnitOfWork:
    return FakeUnitOfWork(_snapshot() if snapshot is None else snapshot)


def _service(uow: FakeUnitOfWork) -> ProposalService:
    return ProposalService(
        uow_factory=FakeUoWFactory(uow),
        registry=_registry(),
        clock=FixedClock(NOW),
    )


def _fixture_payload(name: str) -> dict[str, object]:
    return load_fixture_payload(FIXTURES / name)


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload = _fixture_payload("broad_etf_core_v0_1_0.json")
    payload.update(overrides)
    return payload


def _proposal_from_valid(**overrides: object) -> AllocationProposal:
    return parse_proposal_payload(_valid_payload(**overrides))


def _validate(
    proposal: AllocationProposal,
    *,
    known_symbols: list[str] | None = None,
    eligible_symbols: list[str] | None = None,
    evaluation_time: datetime = NOW,
    known_evidence_refs: list[str] | None = None,
) -> ProposalValidationResult:
    uow = _uow()
    return _service(uow).validate(
        proposal,
        account_id=ACCOUNT_ID,
        known_symbols=known_symbols or [SYMBOL],
        eligible_symbols=eligible_symbols if eligible_symbols is not None else [SYMBOL],
        evaluation_time=evaluation_time,
        known_evidence_refs=known_evidence_refs,
    )


# ---------------------------------------------------------------------------
# Strict schema boundary (SecurityViolationError)
# ---------------------------------------------------------------------------
def test_valid_fixture_parses_and_preserves_lineage_fields() -> None:
    proposal = FixtureAgent(FIXTURES / "broad_etf_core_v0_1_0.json").propose()
    assert proposal.strategy_id == "broad_etf_core"
    assert proposal.strategy_version == "0.1.0"
    assert proposal.symbols == (SYMBOL, "CASH.CNY")
    assert proposal.cash_weight == Decimal("0.80")
    assert proposal.data_snapshot_id == SNAPSHOT_ID
    assert proposal.model_id == "fixture://no-llm"
    assert proposal.prompt_version == "research_agent_v0_1_0"


def test_mapping_and_json_text_parse_identically() -> None:
    payload = _valid_payload()
    from_mapping = parse_proposal_payload(payload)
    from_text = parse_proposal_payload(json.dumps(payload))
    assert from_mapping.symbols == from_text.symbols
    assert from_mapping.strategy_id == from_text.strategy_id


def test_duplicate_symbol_payload_is_rejected() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_fixture_payload("duplicate_symbol.json"))


def test_weight_out_of_range_payload_is_rejected() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_fixture_payload("out_of_range_weight.json"))


def test_weight_sum_mismatch_payload_is_rejected() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_fixture_payload("weight_sum_mismatch.json"))


def test_prohibited_tool_policy_credential_fields_are_security_violations() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_fixture_payload("extra_key_tool_call.json"))


def test_unknown_json_keys_are_rejected() -> None:
    payload = _valid_payload(unknown_key={"unexpected": True})
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(payload)


def test_malformed_ticker_format_is_rejected() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_fixture_payload("malformed_symbol.json"))


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_valid_payload(as_of="2026-11-05T15:30:00"))


def test_oversized_mapping_payload_is_rejected() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(_valid_payload(thesis="x" * 40_000))


def test_oversized_json_text_payload_is_rejected() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(json.dumps(_valid_payload(thesis="x" * 40_000)))


def test_malformed_json_text_is_rejected() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload("{not-json")


def test_non_object_payload_is_rejected() -> None:
    with pytest.raises(SecurityViolationError):
        parse_proposal_payload(json.dumps([1, 2, 3]))


def test_missing_agent_fixture_path_is_rejected() -> None:
    with pytest.raises(ValidationError):
        FixtureAgent(FIXTURES / "does-not-exist.json")


# ---------------------------------------------------------------------------
# Semantic validation (ProposalValidationResult)
# ---------------------------------------------------------------------------
def test_unknown_symbol_is_rejected() -> None:
    proposal = _proposal_from_valid()
    result = _validate(proposal, known_symbols=["600519.SH"])
    assert not result.valid
    assert ProposalViolation.UNKNOWN_SYMBOL in result.violations


def test_ineligible_instrument_is_rejected() -> None:
    proposal = _proposal_from_valid()
    result = _validate(proposal, eligible_symbols=[])
    assert not result.valid
    assert ProposalViolation.INELIGIBLE_INSTRUMENT in result.violations


def test_unregistered_strategy_version_is_rejected() -> None:
    proposal = parse_proposal_payload(_fixture_payload("unknown_strategy_version.json"))
    result = _validate(proposal)
    assert not result.valid
    assert ProposalViolation.UNKNOWN_STRATEGY in result.violations


def test_unregistered_strategy_id_is_rejected() -> None:
    proposal = _proposal_from_valid(strategy_id="invented_strategy")
    result = _validate(proposal)
    assert not result.valid
    assert ProposalViolation.UNKNOWN_STRATEGY in result.violations


def test_stale_proposal_is_rejected() -> None:
    proposal = parse_proposal_payload(_fixture_payload("stale_proposal_v0_1_0.json"))
    result = _validate(proposal)
    assert not result.valid
    assert ProposalViolation.STALE_DATA in result.violations


def test_future_proposal_is_rejected() -> None:
    proposal = _proposal_from_valid(as_of="2027-01-01T15:30:00+08:00")
    result = _validate(proposal)
    assert not result.valid
    assert ProposalViolation.FUTURE_PROPOSAL in result.violations


def test_missing_data_snapshot_is_rejected() -> None:
    proposal = parse_proposal_payload(_fixture_payload("missing_snapshot.json"))
    result = _validate(proposal)
    assert not result.valid
    assert ProposalViolation.MISSING_DATA_SNAPSHOT in result.violations


def test_snapshot_missing_from_repository_is_rejected() -> None:
    proposal = _proposal_from_valid()
    uow = FakeUnitOfWork(None)
    result = _service(uow).validate(
        proposal,
        account_id=ACCOUNT_ID,
        known_symbols=[SYMBOL],
        eligible_symbols=[SYMBOL],
        evaluation_time=NOW,
    )
    assert not result.valid
    assert ProposalViolation.MISSING_DATA_SNAPSHOT in result.violations


def test_quarantined_snapshot_is_rejected() -> None:
    proposal = _proposal_from_valid()
    uow = _uow(_snapshot(quality=DataQualityStatus.QUARANTINED))
    result = _service(uow).validate(
        proposal,
        account_id=ACCOUNT_ID,
        known_symbols=[SYMBOL],
        eligible_symbols=[SYMBOL],
        evaluation_time=NOW,
    )
    assert not result.valid
    assert ProposalViolation.STALE_DATA in result.violations


def test_stale_snapshot_is_rejected() -> None:
    proposal = _proposal_from_valid()
    uow = _uow(_snapshot(as_of=NOW - timedelta(days=3)))
    result = _service(uow).validate(
        proposal,
        account_id=ACCOUNT_ID,
        known_symbols=[SYMBOL],
        eligible_symbols=[SYMBOL],
        evaluation_time=NOW,
    )
    assert not result.valid
    assert ProposalViolation.STALE_DATA in result.violations


def test_prohibited_instruction_text_is_rejected() -> None:
    proposal = parse_proposal_payload(_fixture_payload("malicious_news_injection.json"))
    result = _validate(proposal)
    assert not result.valid
    assert ProposalViolation.PROHIBITED_INSTRUCTION in result.violations


def test_unknown_evidence_reference_is_rejected() -> None:
    proposal = _proposal_from_valid()
    result = _validate(proposal, known_evidence_refs=["snapshot:other"])
    assert not result.valid
    assert ProposalViolation.UNKNOWN_EVIDENCE_REF in result.violations


def test_missing_thesis_is_rejected() -> None:
    proposal = _proposal_from_valid(thesis="   ")
    result = _validate(proposal)
    assert not result.valid
    assert ProposalViolation.MISSING_THESIS in result.violations


def test_valid_proposal_passes() -> None:
    result = _validate(_proposal_from_valid())
    assert result.valid
    assert result.violations == ()


# ---------------------------------------------------------------------------
# Decision-lineage recording
# ---------------------------------------------------------------------------
def test_record_persists_full_lineage_for_an_accepted_proposal() -> None:
    uow = _uow()
    service = _service(uow)
    proposal = FixtureAgent(FIXTURES / "broad_etf_core_v0_1_0.json").propose()
    validation = service.validate(
        proposal,
        account_id=ACCOUNT_ID,
        known_symbols=[SYMBOL],
        eligible_symbols=[SYMBOL],
        evaluation_time=NOW,
    )
    assert validation.valid
    record = service.record(
        proposal,
        validation,
        account_id=ACCOUNT_ID,
        rule_set_id=RULE_SET_ID,
        rule_set_version=RULE_SET_VERSION,
    )
    assert isinstance(record, DecisionRecord)
    assert record.decision_type is DecisionType.ALLOCATION_PROPOSAL
    assert record.proposal_id == proposal.proposal_id
    assert record.strategy_id == "broad_etf_core"
    assert record.strategy_version == "0.1.0"
    assert record.model_id == "fixture://no-llm"
    assert record.prompt_version == "research_agent_v0_1_0"
    assert record.data_snapshot_id == SNAPSHOT_ID
    assert record.rule_set_id == RULE_SET_ID
    assert record.rule_set_version == RULE_SET_VERSION
    assert record.executable is False
    assert record.outcome == "ACCEPTED"
    assert record.inputs["validation"]["valid"] is True
    assert record.inputs["proposal"]["strategy_id"] == "broad_etf_core"
    # Money/weights are persisted as decimal strings, never floats.
    stored_weight = record.inputs["proposal"]["target_weights"][0]["weight"]
    assert isinstance(stored_weight, str)
    assert uow.decisions.records == [record]
    assert uow.commits == 1


def test_submit_records_rejected_proposals_too() -> None:
    uow = _uow()
    service = _service(uow)
    proposal = parse_proposal_payload(_fixture_payload("stale_proposal_v0_1_0.json"))
    validation, record = service.submit(
        proposal,
        account_id=ACCOUNT_ID,
        known_symbols=[SYMBOL],
        eligible_symbols=[SYMBOL],
        evaluation_time=NOW,
        rule_set_id=RULE_SET_ID,
        rule_set_version=RULE_SET_VERSION,
    )
    assert not validation.valid
    assert record.executable is False
    assert record.outcome.startswith("REJECTED")
    assert record.inputs["validation"]["valid"] is False
    assert uow.decisions.records == [record]
