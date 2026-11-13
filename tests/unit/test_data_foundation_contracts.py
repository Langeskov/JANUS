"""Data-foundation domain contracts (PR-001 §4.2, §4.3, §5, §7).

Pure domain tests: no database, no filesystem, no clock. They pin the invariants
that the persistence and service layers must not weaken.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from janus.domain.common.errors import UnsupportedEntityError, ValidationError
from janus.domain.common.time import MARKET_TZ
from janus.domain.data_foundation.capabilities import (
    DataCapability,
    DataSource,
    DataSourceKind,
    ProviderCapabilities,
    capability_change,
)
from janus.domain.data_foundation.quality import (
    IngestionReport,
    QualityIssue,
    QualityIssueCode,
    QualityState,
    derive_quality_state,
    severity_of,
)
from janus.domain.data_foundation.raw import (
    RawPayload,
    RedactionFinding,
    find_credential_material,
)
from janus.domain.data_foundation.revisions import DataRevision, DataValue, ValueKind
from pydantic import ValidationError as PydanticValidationError
from tests.conftest import HASH_A, HASH_B, provenance, revision

pytestmark = pytest.mark.phase0


def _dt(day: int = 5, hour: int = 15, minute: int = 30) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=MARKET_TZ)


# ---------------------------------------------------------------------------
# DataValue: exact, typed, never float
# ---------------------------------------------------------------------------
def test_decimal_value_preserves_exact_digits() -> None:
    value = DataValue.decimal_value(Decimal("9800000000.123456789"), unit="CNY")
    assert value.kind is ValueKind.DECIMAL
    assert value.as_decimal() == Decimal("9800000000.123456789")
    assert value.unit == "CNY"
    # Canonical form is what feeds the deterministic revision identity.
    assert value.canonical() == "DECIMAL\x1f9800000000.123456789\x1fCNY"


@pytest.mark.parametrize(
    ("value", "expected_kind"),
    [
        (DataValue.integer_value(42, unit="SHARES"), ValueKind.INTEGER),
        (DataValue.text_value("沪市主板"), ValueKind.TEXT),
        (DataValue.boolean_value(True), ValueKind.BOOLEAN),
        (DataValue.date_value(date(2026, 9, 30)), ValueKind.DATE),
        (DataValue.datetime_value(_dt()), ValueKind.DATETIME),
    ],
)
def test_value_kinds_round_trip(value: DataValue, expected_kind: ValueKind) -> None:
    assert value.kind is expected_kind
    assert value.text


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": ValueKind.DECIMAL, "text": "not-a-number"},
        {"kind": ValueKind.INTEGER, "text": "1.5"},
        {"kind": ValueKind.BOOLEAN, "text": "yes"},
        {"kind": ValueKind.DATE, "text": "2026-13-01"},
        {"kind": ValueKind.DATETIME, "text": "2026-11-05T15:30:00"},
    ],
)
def test_invalid_values_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DataValue.model_validate(payload)


def test_non_decimal_accessor_refuses() -> None:
    with pytest.raises(ValidationError):
        DataValue.text_value("hello").as_decimal()
    with pytest.raises(ValidationError):
        DataValue.text_value("hello").as_int()


# ---------------------------------------------------------------------------
# Revisions: immutable, deterministic, ordered
# ---------------------------------------------------------------------------
def test_revision_identity_is_deterministic_and_content_addressed() -> None:
    prov = provenance(public_available_at=_dt())
    first = revision(provenance_record=prov, sequence=1)
    same = revision(provenance_record=prov, sequence=1)
    changed = revision(
        provenance_record=prov,
        sequence=1,
        value=DataValue.decimal_value(Decimal("1"), unit="CNY"),
    )
    assert first.revision_id == same.revision_id
    assert first.revision_id != changed.revision_id


def test_revision_is_frozen() -> None:
    prov = provenance(public_available_at=_dt())
    rev = revision(provenance_record=prov)
    with pytest.raises((ValidationError, TypeError, ValueError)):
        rev.logical_key = "other"  # type: ignore[misc]


def test_ordering_key_is_total() -> None:
    prov_a = provenance(public_available_at=_dt(), payload_hash=HASH_A)
    prov_b = provenance(public_available_at=_dt(), payload_hash=HASH_B)
    first = revision(provenance_record=prov_a, sequence=1)
    second = revision(provenance_record=prov_b, sequence=2)
    assert second.ordering_key() > first.ordering_key()


def test_revision_sequence_must_be_positive() -> None:
    prov = provenance(public_available_at=_dt())
    # A pydantic field constraint (not a business-rule error) guards the range.
    with pytest.raises(PydanticValidationError):
        revision(provenance_record=prov, sequence=0)


def test_business_time_window_is_start_inclusive_end_exclusive() -> None:
    prov = provenance(public_available_at=_dt())
    rev = revision(
        provenance_record=prov,
        effective_from=_dt(6, 0, 0),
        effective_to=_dt(8, 0, 0),
    )
    assert rev.covers_business_time(_dt(6, 0, 0)) is True
    assert rev.covers_business_time(_dt(7, 12, 0)) is True
    assert rev.covers_business_time(_dt(8, 0, 0)) is False
    assert rev.covers_business_time(_dt(5, 23, 59)) is False


def test_invalid_period_or_window_is_rejected() -> None:
    prov = provenance(public_available_at=_dt())
    with pytest.raises(ValidationError):
        DataRevision.build(
            logical_key="k",
            dataset="financials",
            source_id="fixture",
            revision_sequence=1,
            value=DataValue.text_value("x"),
            provenance_id=prov.provenance_id,
            payload_hash=HASH_A,
            source_revision="r1",
            ingested_at=_dt(),
            period_start=date(2026, 9, 30),
            period_end=date(2026, 6, 30),
        )
    with pytest.raises(ValidationError):
        DataRevision.build(
            logical_key="k",
            dataset="financials",
            source_id="fixture",
            revision_sequence=1,
            value=DataValue.text_value("x"),
            provenance_id=prov.provenance_id,
            payload_hash=HASH_A,
            source_revision="r1",
            ingested_at=_dt(),
            effective_from=_dt(8),
            effective_to=_dt(7),
        )


def test_a_revision_cannot_supersede_itself() -> None:
    prov = provenance(public_available_at=_dt())
    rev = revision(provenance_record=prov)
    with pytest.raises(ValidationError):
        DataRevision(
            revision_id=rev.revision_id,
            logical_key=rev.logical_key,
            dataset=rev.dataset,
            source_id=rev.source_id,
            subject_symbol=rev.subject_symbol,
            revision_sequence=rev.revision_sequence,
            value=rev.value,
            period_start=rev.period_start,
            period_end=rev.period_end,
            effective_from=rev.effective_from,
            effective_to=rev.effective_to,
            provenance_id=rev.provenance_id,
            payload_hash=rev.payload_hash,
            source_revision=rev.source_revision,
            supersedes_revision_id=rev.revision_id,
            ingested_at=rev.ingested_at,
        )


# ---------------------------------------------------------------------------
# Ingestion report: deterministic, reconciled
# ---------------------------------------------------------------------------
def test_all_accepted_yields_accepted_status() -> None:
    report = IngestionReport.build(
        ingestion_id="ing-1",
        dataset="daily_bars",
        source_id="fixture",
        started_at=_dt(),
        finished_at=_dt(),
        received_rows=3,
        accepted_rows=3,
        incomplete_rows=0,
        rejected_rows=0,
        duplicate_rows=0,
    )
    assert report.status is QualityState.ACCEPTED


@pytest.mark.parametrize(
    ("rejected", "duplicates", "incomplete", "expected"),
    [
        (1, 0, 0, QualityState.WARNING),
        (0, 2, 0, QualityState.WARNING),
        (0, 0, 1, QualityState.INCOMPLETE),
    ],
)
def test_status_derivation_matrix(
    rejected: int, duplicates: int, incomplete: int, expected: QualityState
) -> None:
    accepted = 3 - rejected - duplicates - incomplete
    assert (
        derive_quality_state(
            received_rows=3,
            accepted_rows=accepted,
            incomplete_rows=incomplete,
            rejected_rows=rejected,
            duplicate_rows=duplicates,
            issues=(),
        )
        is expected
    )


def test_critical_issue_or_provider_failure_rejects_the_run() -> None:
    credential_issue = QualityIssue(
        code=QualityIssueCode.CREDENTIAL_MATERIAL_DETECTED, message="token in payload"
    )
    assert severity_of(QualityIssueCode.CREDENTIAL_MATERIAL_DETECTED).value == "CRITICAL"
    assert (
        derive_quality_state(
            received_rows=1,
            accepted_rows=1,
            incomplete_rows=0,
            rejected_rows=0,
            duplicate_rows=0,
            issues=(credential_issue,),
        )
        is QualityState.REJECTED
    )
    assert (
        derive_quality_state(
            received_rows=0,
            accepted_rows=0,
            incomplete_rows=0,
            rejected_rows=0,
            duplicate_rows=0,
            issues=(),
            provider_failure=True,
        )
        is QualityState.REJECTED
    )


def test_report_counters_must_reconcile() -> None:
    with pytest.raises(ValidationError):
        IngestionReport.build(
            ingestion_id="ing-1",
            dataset="daily_bars",
            source_id="fixture",
            started_at=_dt(),
            finished_at=_dt(),
            received_rows=10,
            accepted_rows=1,
            incomplete_rows=0,
            rejected_rows=0,
            duplicate_rows=0,
        )


def test_report_status_cannot_contradict_derivation() -> None:
    with pytest.raises(ValidationError):
        IngestionReport(
            ingestion_id="ing-1",
            dataset="daily_bars",
            source_id="fixture",
            started_at=_dt(),
            finished_at=_dt(5, 15, 31),
            received_rows=1,
            accepted_rows=1,
            incomplete_rows=0,
            rejected_rows=0,
            duplicate_rows=0,
            status=QualityState.REJECTED,  # contradicts counters
        )


def test_issue_codes_are_ordered_and_deduplicated() -> None:
    report = IngestionReport.build(
        ingestion_id="ing-1",
        dataset="daily_bars",
        source_id="fixture",
        started_at=_dt(),
        finished_at=_dt(),
        received_rows=2,
        accepted_rows=0,
        incomplete_rows=0,
        rejected_rows=0,
        duplicate_rows=2,
        issues=(
            QualityIssue(code=QualityIssueCode.DUPLICATE_RECORD),
            QualityIssue(code=QualityIssueCode.DUPLICATE_RECORD),
            QualityIssue(code=QualityIssueCode.UNKNOWN_AVAILABILITY),
        ),
    )
    assert report.issue_codes() == (
        QualityIssueCode.DUPLICATE_RECORD,
        QualityIssueCode.UNKNOWN_AVAILABILITY,
    )


# ---------------------------------------------------------------------------
# Provider capabilities
# ---------------------------------------------------------------------------
def test_capability_declaration_gates_explicitly() -> None:
    declaration = ProviderCapabilities(
        provider_id="fixture",
        supported=frozenset({DataCapability.DAILY_BARS}),
        declared_at=_dt(),
        declaration_reason="offline fixtures",
    )
    assert declaration.supports(DataCapability.DAILY_BARS) is True
    declaration.require(DataCapability.DAILY_BARS)  # must not raise
    with pytest.raises(UnsupportedEntityError):
        declaration.require(DataCapability.HISTORICAL_MINUTES)
    assert DataCapability.HISTORICAL_MINUTES in declaration.unsupported()


def test_empty_declaration_supports_nothing() -> None:
    declaration = ProviderCapabilities(
        provider_id="tushare",
        supported=frozenset(),
        declared_at=_dt(),
        notes="Phase 1",
    )
    assert declaration.unsupported() == tuple(sorted(DataCapability, key=lambda item: item.value))
    with pytest.raises(UnsupportedEntityError):
        declaration.require(DataCapability.DAILY_BARS)


def test_capability_change_is_deterministic() -> None:
    previous = ProviderCapabilities(
        provider_id="p",
        supported=frozenset({DataCapability.DAILY_BARS}),
        declared_at=_dt(5),
    )
    current = ProviderCapabilities(
        provider_id="p",
        supported=frozenset({DataCapability.DAILY_BARS, DataCapability.FINANCIAL_REPORTS}),
        declared_at=_dt(6),
    )
    change = capability_change(previous, current)
    assert change.added == (DataCapability.FINANCIAL_REPORTS,)
    assert change.removed == ()
    assert change.is_change is True
    assert capability_change(previous, current).change_id == change.change_id
    assert capability_change(current, current).is_change is False


def test_declaration_id_is_content_addressed() -> None:
    first = ProviderCapabilities(
        provider_id="p", supported=frozenset({DataCapability.DAILY_BARS}), declared_at=_dt()
    )
    second = ProviderCapabilities(
        provider_id="p", supported=frozenset({DataCapability.FINANCIAL_REPORTS}), declared_at=_dt()
    )
    assert first.declaration_id != second.declaration_id


# ---------------------------------------------------------------------------
# Raw payload archive
# ---------------------------------------------------------------------------
def test_archive_computes_hash_and_size() -> None:
    payload = RawPayload.archive(
        source_id="fixture",
        dataset="daily_bars",
        payload_text='{"bars": [1, 2, 3]}',
        retrieved_at=_dt(),
        ingested_at=_dt(),
    )
    assert len(payload.content_hash) == 64
    assert payload.byte_size == len(b'{"bars": [1, 2, 3]}')
    assert payload.payload_id == f"raw-{payload.content_hash[:32]}"


def test_identical_content_maps_to_the_same_payload_id() -> None:
    first = RawPayload.archive(
        source_id="fixture",
        dataset="daily_bars",
        payload_text="same bytes",
        retrieved_at=_dt(5),
        ingested_at=_dt(5),
    )
    second = RawPayload.archive(
        source_id="fixture",
        dataset="daily_bars",
        payload_text="same bytes",
        retrieved_at=_dt(9),
        ingested_at=_dt(9),
    )
    assert first.payload_id == second.payload_id


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"headers": {"authorization": "Bearer abcdefghijklmnop"}}',
            RedactionFinding.AUTHORIZATION_HEADER,
        ),
        ("Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature", RedactionFinding.BEARER_TOKEN),
        ('{"api_key": "sk-live-abcdefgh12345678"}', RedactionFinding.API_KEY_ASSIGNMENT),
        ("Cookie: session=abcdef123456", RedactionFinding.COOKIE_HEADER),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIE...", RedactionFinding.PRIVATE_KEY_BLOCK),
        ('{"password": "hunter2hunter2"}', RedactionFinding.PASSWORD_ASSIGNMENT),
    ],
)
def test_credential_material_is_detected(text: str, expected: RedactionFinding) -> None:
    assert expected in find_credential_material(text)
    with pytest.raises(ValidationError):
        RawPayload.archive(
            source_id="fixture",
            dataset="d",
            payload_text=text,
            retrieved_at=_dt(),
            ingested_at=_dt(),
        )


def test_clean_payload_archives() -> None:
    payload = RawPayload.archive(
        source_id="fixture",
        dataset="d",
        payload_text='{"revenue": "10000000000"}',
        retrieved_at=_dt(),
        ingested_at=_dt(),
    )
    assert payload.redaction_findings == ()


def test_request_scope_rejects_credential_keys_and_sorts() -> None:
    payload = RawPayload.archive(
        source_id="fixture",
        dataset="daily_bars",
        payload_text="{}",
        retrieved_at=_dt(),
        ingested_at=_dt(),
        request_scope={"symbol": "600519.SH", "trade_date": "2026-11-05"},
    )
    assert list(payload.request_scope) == ["symbol", "trade_date"]
    with pytest.raises(ValidationError):
        RawPayload.archive(
            source_id="fixture",
            dataset="daily_bars",
            payload_text="{}",
            retrieved_at=_dt(),
            ingested_at=_dt(),
            request_scope={"api_token": "x"},
        )


def test_tampered_payload_is_rejected() -> None:
    payload = RawPayload.archive(
        source_id="fixture",
        dataset="d",
        payload_text="original",
        retrieved_at=_dt(),
        ingested_at=_dt(),
    )
    with pytest.raises(ValidationError):
        RawPayload(
            payload_id=payload.payload_id,
            content_hash=payload.content_hash,
            byte_size=payload.byte_size,
            media_type=payload.media_type,
            source_id=payload.source_id,
            dataset=payload.dataset,
            request_scope={},
            retrieved_at=payload.retrieved_at,
            ingested_at=payload.ingested_at,
            payload_text="tampered",
        )


# ---------------------------------------------------------------------------
# Data source identity
# ---------------------------------------------------------------------------
def test_source_ids_are_lower_case_and_registration_is_aware() -> None:
    source = DataSource(
        source_id="fixture",
        kind=DataSourceKind.LOCAL_FIXTURE,
        display_name="Deterministic fixtures",
        registered_at=_dt(),
    )
    assert source.source_id == "fixture"
    with pytest.raises(ValidationError):
        DataSource(
            source_id="Fixture",
            kind=DataSourceKind.LOCAL_FIXTURE,
            display_name="x",
            registered_at=_dt(),
        )
    with pytest.raises(ValidationError):
        DataSource(
            source_id="fixture",
            kind=DataSourceKind.LOCAL_FIXTURE,
            display_name="x",
            registered_at=datetime(2026, 11, 5),  # naive
        )
