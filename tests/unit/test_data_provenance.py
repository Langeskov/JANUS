"""Provenance and availability semantics (PR-001 §4.1).

These are the tests that prevent the classic point-in-time bugs: conflating
publication with retrieval, back-dating a historical import, or manufacturing
sub-day precision from a date-only source.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from janus.domain.common.errors import ValidationError
from janus.domain.common.time import MARKET_TZ
from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPolicy,
    AvailabilityPrecision,
    DateOnlyAvailabilityMode,
    derive_strategy_usable_at,
    market_day_start,
    next_market_day_start,
)
from janus.domain.data_foundation.provenance import (
    DataProvenance,
    ProvenanceTimestamps,
)
from tests.conftest import HASH_A, provenance, timestamps

pytestmark = pytest.mark.phase0

UTC = MARKET_TZ  # market timezone is used for readability in these tests


def _dt(day: int, hour: int = 15, minute: int = 30, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=MARKET_TZ)


# ---------------------------------------------------------------------------
# Exact publication timestamps
# ---------------------------------------------------------------------------
def test_exact_publication_time_is_used_verbatim() -> None:
    published = _dt(5, 16, 0)
    record = provenance(
        public_available_at=published,
        published_at=published,
        retrieved_at=_dt(5, 16, 5),
    )
    assert record.strategy_usable_at == published
    assert record.availability_basis is AvailabilityBasis.SOURCE_TIMESTAMP
    assert record.availability_precision is AvailabilityPrecision.INSTANT
    assert record.strategy_usable_at_derivation is AvailabilityBasis.SOURCE_TIMESTAMP
    assert record.has_known_availability is True


def test_minute_precision_is_treated_as_exact() -> None:
    published = _dt(5, 16, 0)
    record = provenance(
        public_available_at=published,
        published_at=published,
        precision=AvailabilityPrecision.MINUTE,
        basis=AvailabilityBasis.PROVIDER_TIMESTAMP,
    )
    assert record.strategy_usable_at == published
    assert record.strategy_usable_at_derivation is AvailabilityBasis.PROVIDER_TIMESTAMP


# ---------------------------------------------------------------------------
# Date-only publication
# ---------------------------------------------------------------------------
def test_date_only_publication_uses_next_calendar_day_midnight() -> None:
    day = date(2026, 11, 5)
    published_midnight = market_day_start(_dt(5, 0, 0))
    record = provenance(
        public_available_at=published_midnight,
        published_at=published_midnight,
        precision=AvailabilityPrecision.DATE,
        basis=AvailabilityBasis.SOURCE_PUBLICATION_DATE,
    )
    # Conservative lower bound: the following calendar day's 00:00 Shanghai.
    assert record.strategy_usable_at == next_market_day_start(published_midnight)
    assert record.strategy_usable_at == datetime(2026, 11, 6, 0, 0, tzinfo=MARKET_TZ)
    assert record.availability_precision is AvailabilityPrecision.DATE
    assert record.strategy_usable_at_derivation is AvailabilityBasis.CONSERVATIVE_POLICY
    assert day == market_day_start(record.strategy_usable_at).date() - timedelta(days=1)


def test_date_only_never_invents_an_intraday_time() -> None:
    """A mid-afternoon reported date must not become ``same-day 15:00``."""
    record = provenance(
        public_available_at=_dt(5, 15, 0),  # a datetime supplied with DATE precision
        precision=AvailabilityPrecision.DATE,
        basis=AvailabilityBasis.SOURCE_PUBLICATION_DATE,
    )
    assert record.strategy_usable_at is not None
    # The sub-day component is discarded entirely.
    assert record.strategy_usable_at.hour == 0
    assert record.strategy_usable_at.minute == 0
    assert record.strategy_usable_at.date() == date(2026, 11, 6)


def test_strict_unknown_mode_marks_date_only_availability_unknown() -> None:
    policy = AvailabilityPolicy(date_only_mode=DateOnlyAvailabilityMode.STRICT_UNKNOWN)
    record = provenance(
        public_available_at=market_day_start(_dt(5, 0, 0)),
        precision=AvailabilityPrecision.DATE,
        basis=AvailabilityBasis.SOURCE_PUBLICATION_DATE,
        policy=policy,
    )
    assert record.strategy_usable_at is None
    assert record.has_known_availability is False
    assert record.strategy_usable_at_derivation is AvailabilityBasis.UNKNOWN
    assert record.knowledge_policy_version == policy.policy_version


# ---------------------------------------------------------------------------
# Unknown availability fails closed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("basis", "precision"),
    [
        (AvailabilityBasis.UNKNOWN, AvailabilityPrecision.UNKNOWN),
        (AvailabilityBasis.UNKNOWN, AvailabilityPrecision.INSTANT),
        (AvailabilityBasis.SOURCE_TIMESTAMP, AvailabilityPrecision.UNKNOWN),
    ],
)
def test_unknown_metadata_yields_unknown_availability(
    basis: AvailabilityBasis, precision: AvailabilityPrecision
) -> None:
    usable_at, derivation = derive_strategy_usable_at(
        public_available_at=_dt(5),
        published_at=_dt(5),
        precision=precision,
        basis=basis,
        policy=AvailabilityPolicy(),
    )
    assert usable_at is None
    assert derivation is AvailabilityBasis.UNKNOWN


def test_unknown_availability_is_never_visible() -> None:
    record = provenance(
        public_available_at=market_day_start(_dt(5, 0, 0)),
        precision=AvailabilityPrecision.DATE,
        basis=AvailabilityBasis.SOURCE_PUBLICATION_DATE,
        policy=AvailabilityPolicy(date_only_mode=DateOnlyAvailabilityMode.STRICT_UNKNOWN),
    )
    assert record.is_available_at(_dt(30)) is False
    with pytest.raises(ValidationError):
        record.to_point_in_time()


def test_missing_public_availability_yields_unknown() -> None:
    usable_at, derivation = derive_strategy_usable_at(
        public_available_at=None,
        published_at=_dt(5),
        precision=AvailabilityPrecision.INSTANT,
        basis=AvailabilityBasis.SOURCE_TIMESTAMP,
        policy=AvailabilityPolicy(),
    )
    assert usable_at is None
    assert derivation is AvailabilityBasis.UNKNOWN


# ---------------------------------------------------------------------------
# The four times are distinct
# ---------------------------------------------------------------------------
def test_historical_bulk_import_does_not_back_date_observation() -> None:
    """A 2026-09-30 fact imported on 2026-11-12 keeps a modern observation time."""
    published = datetime(2026, 10, 28, 17, 0, tzinfo=MARKET_TZ)
    retrieved = datetime(2026, 11, 12, 9, 0, tzinfo=MARKET_TZ)
    record = provenance(
        event_at=datetime(2026, 9, 30, 0, 0, tzinfo=MARKET_TZ),
        public_available_at=published,
        published_at=published,
        retrieved_at=retrieved,
        first_observed_at=retrieved,
    )
    assert record.strategy_usable_at == published
    assert record.timestamps.system_first_observed_at == retrieved
    assert record.timestamps.retrieved_at == retrieved
    # Available (it was public) but not system-observed at the historical cutoff.
    assert record.is_available_at(datetime(2026, 11, 7, tzinfo=MARKET_TZ)) is True
    assert record.is_system_observed_at(datetime(2026, 11, 7, tzinfo=MARKET_TZ)) is False
    assert record.is_system_observed_at(datetime(2026, 11, 12, 12, 0, tzinfo=MARKET_TZ)) is True


def test_availability_boundary_is_inclusive() -> None:
    usable_at = _dt(5, 16, 0)
    record = provenance(public_available_at=usable_at, published_at=usable_at)
    assert record.is_available_at(usable_at) is True
    assert record.is_available_at(usable_at - timedelta(microseconds=1)) is False


# ---------------------------------------------------------------------------
# Ordering / consistency validation
# ---------------------------------------------------------------------------
def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProvenanceTimestamps(
            event_at=datetime(2026, 11, 5, 15, 30),  # naive
            retrieved_at=_dt(5),
            ingested_at=_dt(5),
            system_first_observed_at=_dt(5),
        )


def test_strategy_usable_cannot_precede_public_availability() -> None:
    with pytest.raises(ValidationError):
        DataProvenance(
            provenance_id="prov-1",
            source_id="fixture",
            dataset="financials",
            source_record_id="rec-1",
            timestamps=timestamps(event_at=_dt(5), published_at=_dt(5)),
            public_available_at=_dt(5, 17, 0),
            strategy_usable_at=_dt(5, 16, 0),  # earlier than public availability
            availability_basis=AvailabilityBasis.SOURCE_TIMESTAMP,
            availability_precision=AvailabilityPrecision.INSTANT,
            strategy_usable_at_derivation=AvailabilityBasis.SOURCE_TIMESTAMP,
            source_revision="r1",
            knowledge_policy_version="p1a-availability-v1",
            payload_hash=HASH_A,
        )


def test_public_availability_cannot_precede_publication() -> None:
    with pytest.raises(ValidationError):
        DataProvenance(
            provenance_id="prov-1",
            source_id="fixture",
            dataset="financials",
            source_record_id="rec-1",
            timestamps=timestamps(event_at=_dt(5), published_at=_dt(5, 18, 0)),
            public_available_at=_dt(5, 17, 0),
            strategy_usable_at=_dt(5, 17, 0),
            availability_basis=AvailabilityBasis.SOURCE_TIMESTAMP,
            availability_precision=AvailabilityPrecision.INSTANT,
            strategy_usable_at_derivation=AvailabilityBasis.SOURCE_TIMESTAMP,
            source_revision="r1",
            knowledge_policy_version="p1a-availability-v1",
            payload_hash=HASH_A,
        )


def test_retrieval_cannot_follow_ingestion_and_observation_cannot_follow_retrieval() -> None:
    with pytest.raises(ValidationError):
        timestamps(event_at=_dt(5), retrieved_at=_dt(6), ingested_at=_dt(5))
    with pytest.raises(ValidationError):
        ProvenanceTimestamps(
            event_at=_dt(5),
            retrieved_at=_dt(5),
            ingested_at=_dt(5),
            system_first_observed_at=_dt(6),  # after retrieval
        )


def test_payload_hash_must_be_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        provenance(public_available_at=_dt(5), payload_hash="not-a-hash")


def test_provenance_ids_are_content_addressed() -> None:
    first = provenance(public_available_at=_dt(5))
    second = provenance(public_available_at=_dt(5))
    assert first.provenance_id == second.provenance_id
    different = provenance(public_available_at=_dt(5), source_revision="r2")
    assert different.provenance_id != first.provenance_id


def test_point_in_time_bridge_uses_strategy_availability() -> None:
    record = provenance(public_available_at=_dt(5), published_at=_dt(5))
    bridge = record.to_point_in_time()
    assert bridge.known_at == record.strategy_usable_at
    assert bridge.published_at == record.timestamps.published_at
    assert bridge.retrieved_at == record.timestamps.retrieved_at
