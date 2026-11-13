"""Point-in-time selection semantics (PR-001 §4.4, §9.3).

The canonical scenario from the specification:

* Revision 1 becomes available on **5 November**.
* Revision 2 becomes available on **10 November**.
* A query on **7 November** must return Revision 1.
* A query on **11 November** must return Revision 2.

Every test here fails if the implementation leaks look-ahead information.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from janus.domain.common.errors import ValidationError
from janus.domain.common.time import MARKET_TZ
from janus.domain.data_foundation.availability import (
    AvailabilityBasis,
    AvailabilityPolicy,
    AvailabilityPrecision,
    DateOnlyAvailabilityMode,
)
from janus.domain.data_foundation.query import (
    LoadedRevision,
    ObservationMode,
    RevisionQueryFilters,
    invalidated_revision_ids_at,
    select_latest_per_logical_key,
)
from janus.domain.data_foundation.revisions import (
    DataValue,
    RevisionInvalidation,
)
from tests.conftest import HASH_A, HASH_B, loaded, provenance, revision

pytestmark = pytest.mark.phase0

KEY = "fin:600519.SH:revenue:2026Q3"


def _dt(day: int, hour: int = 15, minute: int = 30) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=MARKET_TZ)


def _pair(
    *,
    public_available_at: datetime,
    sequence: int,
    value_text: str,
    payload_hash: str,
    published_at: datetime | None = None,
    retrieved_at: datetime | None = None,
    first_observed_at: datetime | None = None,
    logical_key: str = KEY,
    subject_symbol: str | None = "600519.SH",
    period_end: date | None = None,
    precision: AvailabilityPrecision = AvailabilityPrecision.INSTANT,
    basis: AvailabilityBasis = AvailabilityBasis.SOURCE_TIMESTAMP,
    record_id: str | None = None,
    policy: AvailabilityPolicy | None = None,
) -> LoadedRevision:
    prov = provenance(
        public_available_at=public_available_at,
        published_at=published_at or public_available_at,
        retrieved_at=retrieved_at or public_available_at,
        first_observed_at=first_observed_at or public_available_at,
        payload_hash=payload_hash,
        source_revision=f"r{sequence}",
        record_id=record_id or f"rec-{sequence}",
        precision=precision,
        basis=basis,
        policy=policy,
    )
    rev = revision(
        provenance_record=prov,
        sequence=sequence,
        logical_key=logical_key,
        value=DataValue.decimal_value(Decimal(value_text), unit="CNY"),
        subject_symbol=subject_symbol,
        period_end=period_end,
    )
    return loaded(revision_record=rev, provenance_record=prov)


@pytest.fixture
def revision_one() -> LoadedRevision:
    return _pair(
        public_available_at=_dt(5),
        sequence=1,
        value_text="10000000000",
        payload_hash=HASH_A,
        period_end=date(2026, 9, 30),
    )


@pytest.fixture
def revision_two() -> LoadedRevision:
    """A restatement published on 10 November."""
    return _pair(
        public_available_at=_dt(10),
        sequence=2,
        value_text="9800000000",
        payload_hash=HASH_B,
        period_end=date(2026, 9, 30),
    )


# ---------------------------------------------------------------------------
# The canonical timeline
# ---------------------------------------------------------------------------
def test_as_of_7_november_returns_revision_one(
    revision_one: LoadedRevision, revision_two: LoadedRevision
) -> None:
    selected = select_latest_per_logical_key([revision_one, revision_two], as_of=_dt(7))
    assert len(selected) == 1
    assert selected[0].revision.revision_id == revision_one.revision.revision_id
    assert selected[0].revision.value.as_decimal() == Decimal("10000000000")


def test_as_of_11_november_returns_revision_two(
    revision_one: LoadedRevision, revision_two: LoadedRevision
) -> None:
    selected = select_latest_per_logical_key([revision_one, revision_two], as_of=_dt(11))
    assert len(selected) == 1
    assert selected[0].revision.revision_id == revision_two.revision.revision_id
    assert selected[0].revision.value.as_decimal() == Decimal("9800000000")


def test_before_any_revision_nothing_is_visible(revision_one: LoadedRevision) -> None:
    assert select_latest_per_logical_key([revision_one], as_of=_dt(4)) == ()


def test_both_revisions_remain_in_history(
    revision_one: LoadedRevision, revision_two: LoadedRevision
) -> None:
    """Selection is a view; it must never delete the superseded revision."""
    assert revision_one.revision.revision_id != revision_two.revision.revision_id
    assert {
        revision_one.revision.revision_sequence,
        revision_two.revision.revision_sequence,
    } == {1, 2}
    # Each remains independently selectable at its own cutoff.
    assert select_latest_per_logical_key([revision_one], as_of=_dt(7))[0] == revision_one
    assert select_latest_per_logical_key([revision_two], as_of=_dt(11))[0] == revision_two


# ---------------------------------------------------------------------------
# Look-ahead prevention
# ---------------------------------------------------------------------------
def test_information_published_after_the_cutoff_is_invisible(
    revision_one: LoadedRevision, revision_two: LoadedRevision
) -> None:
    for as_of in (_dt(5), _dt(6), _dt(9), _dt(9, 23, 59)):
        selected = select_latest_per_logical_key([revision_one, revision_two], as_of=as_of)
        assert all(
            entry.revision.revision_id != revision_two.revision.revision_id for entry in selected
        )


def test_information_retrieved_after_the_cutoff(
    revision_one: LoadedRevision,
) -> None:
    """Public in the past but observed by JANUS later.

    Standard backtest mode uses availability; the stricter system-observed mode
    hides it, proving the two notions are not conflated.
    """
    late_import = _pair(
        public_available_at=_dt(5),
        sequence=2,
        value_text="10000000000",
        payload_hash=HASH_B,
        retrieved_at=_dt(12),
        first_observed_at=_dt(12),
    )
    availability_mode = select_latest_per_logical_key(
        [revision_one, late_import],
        as_of=_dt(7),
        observation_mode=ObservationMode.AVAILABILITY_ONLY,
    )
    assert len(availability_mode) == 1  # visible: the information was public

    observed_mode = select_latest_per_logical_key(
        [revision_one, late_import],
        as_of=_dt(7),
        observation_mode=ObservationMode.SYSTEM_OBSERVED_ONLY,
    )
    assert [entry.revision.revision_id for entry in observed_mode] == [
        revision_one.revision.revision_id
    ]


def test_date_only_publication_is_not_visible_same_day() -> None:
    entry = _pair(
        public_available_at=_dt(5, 0, 0),
        sequence=1,
        value_text="1",
        payload_hash=HASH_A,
        precision=AvailabilityPrecision.DATE,
        basis=AvailabilityBasis.SOURCE_PUBLICATION_DATE,
    )
    assert entry.provenance.strategy_usable_at == _dt(5, 0, 0) + timedelta(days=1)
    assert select_latest_per_logical_key([entry], as_of=_dt(5, 23, 59)) == ()
    assert select_latest_per_logical_key([entry], as_of=_dt(6, 0, 0))[0] == entry


def test_unknown_availability_is_excluded() -> None:
    entry = _pair(
        public_available_at=_dt(5, 0, 0),
        sequence=1,
        value_text="1",
        payload_hash=HASH_A,
        precision=AvailabilityPrecision.DATE,
        basis=AvailabilityBasis.SOURCE_PUBLICATION_DATE,
        policy=AvailabilityPolicy(date_only_mode=DateOnlyAvailabilityMode.STRICT_UNKNOWN),
    )
    assert entry.provenance.strategy_usable_at is None
    assert select_latest_per_logical_key([entry], as_of=_dt(30)) == ()


def test_out_of_order_ingestion_still_answers_historical_queries() -> None:
    """Revision 2 loaded before revision 1 must not change the timeline."""
    rev1 = _pair(
        public_available_at=_dt(5),
        sequence=1,
        value_text="10000000000",
        payload_hash=HASH_A,
    )
    rev2 = _pair(
        public_available_at=_dt(10),
        sequence=2,
        value_text="9800000000",
        payload_hash=HASH_B,
    )
    ingestion_order = [rev2, rev1]
    assert (
        select_latest_per_logical_key(ingestion_order, as_of=_dt(7))[0].revision.revision_id
        == rev1.revision.revision_id
    )
    assert (
        select_latest_per_logical_key(ingestion_order, as_of=_dt(11))[0].revision.revision_id
        == rev2.revision.revision_id
    )


# ---------------------------------------------------------------------------
# Deterministic tie-breaking
# ---------------------------------------------------------------------------
def test_same_availability_tie_break_prefers_higher_sequence(
    revision_one: LoadedRevision,
) -> None:
    same_instant = _pair(
        public_available_at=_dt(5),
        sequence=2,
        value_text="9800000000",
        payload_hash=HASH_B,
    )
    selected = select_latest_per_logical_key([revision_one, same_instant], as_of=_dt(6))
    assert selected[0].revision.revision_sequence == 2


def test_same_timestamp_and_sequence_tie_break_is_lexicographic(
    revision_one: LoadedRevision,
) -> None:
    """Total ordering: identical (usable_at, sequence) falls back to revision id."""
    twin = _pair(
        public_available_at=_dt(5),
        sequence=1,
        value_text="1",
        payload_hash=HASH_B,
    )
    selected = select_latest_per_logical_key([revision_one, twin, revision_one], as_of=_dt(6))
    expected = max(
        [revision_one.revision.revision_id, twin.revision.revision_id],
    )
    assert selected[0].revision.revision_id == expected


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------
def test_invalidation_applies_only_from_its_effective_time(
    revision_one: LoadedRevision, revision_two: LoadedRevision
) -> None:
    invalidation = RevisionInvalidation.record(
        revision_id=revision_two.revision.revision_id,
        reason="provider confirmed the restatement was wrong",
        effective_at=_dt(15),
        recorded_at=_dt(14),
    )
    assert invalidation.revision_id == revision_two.revision.revision_id

    before = select_latest_per_logical_key(
        [revision_one, revision_two],
        as_of=_dt(11),
        invalidated_revision_ids=invalidated_revision_ids_at([invalidation], as_of=_dt(11)),
    )
    assert before[0].revision.revision_id == revision_two.revision.revision_id

    after = select_latest_per_logical_key(
        [revision_one, revision_two],
        as_of=_dt(16),
        invalidated_revision_ids=invalidated_revision_ids_at([invalidation], as_of=_dt(16)),
    )
    # The invalidated revision is skipped and the last good revision is returned.
    assert after[0].revision.revision_id == revision_one.revision.revision_id


# ---------------------------------------------------------------------------
# Filters and multiple logical keys
# ---------------------------------------------------------------------------
def test_one_result_per_logical_key_sorted_deterministically() -> None:
    first = _pair(
        public_available_at=_dt(5),
        sequence=1,
        value_text="1",
        payload_hash=HASH_A,
        logical_key="fin:600519.SH:revenue:2026Q3",
    )
    second = _pair(
        public_available_at=_dt(5),
        sequence=1,
        value_text="2",
        payload_hash=HASH_B,
        logical_key="fin:600519.SH:revenue:2026Q2",
    )
    selected = select_latest_per_logical_key([first, second], as_of=_dt(6))
    assert [entry.revision.logical_key for entry in selected] == sorted(
        [first.revision.logical_key, second.revision.logical_key]
    )


def test_filters_narrow_by_key_prefix_symbol_and_period() -> None:
    revenue = _pair(
        public_available_at=_dt(5),
        sequence=1,
        value_text="1",
        payload_hash=HASH_A,
        logical_key="fin:600519.SH:revenue:2026Q3",
        subject_symbol="600519.SH",
        period_end=date(2026, 9, 30),
    )
    other = _pair(
        public_available_at=_dt(5),
        sequence=1,
        value_text="2",
        payload_hash=HASH_B,
        logical_key="fin:000001.SZ:revenue:2026Q3",
        subject_symbol="000001.SZ",
        period_end=date(2026, 9, 30),
    )
    candidates = [revenue, other]

    assert (
        len(
            select_latest_per_logical_key(
                candidates,
                as_of=_dt(6),
                filters=RevisionQueryFilters(logical_key_prefix="fin:600519"),
            )
        )
        == 1
    )
    assert (
        len(
            select_latest_per_logical_key(
                candidates, as_of=_dt(6), filters=RevisionQueryFilters(subject_symbol="000001.SZ")
            )
        )
        == 1
    )
    assert (
        len(
            select_latest_per_logical_key(
                candidates,
                as_of=_dt(6),
                filters=RevisionQueryFilters(period_end_from=date(2026, 10, 1)),
            )
        )
        == 0
    )


def test_business_time_interval_is_respected() -> None:
    effective = _pair(
        public_available_at=_dt(5),
        sequence=1,
        value_text="1",
        payload_hash=HASH_A,
    )
    # Rebuild with an explicit validity window.
    prov = effective.provenance
    bordered = revision(
        provenance_record=prov,
        sequence=1,
        logical_key=KEY,
        effective_from=_dt(6, 0, 0),
        effective_to=_dt(8, 0, 0),
    )
    entry = loaded(revision_record=bordered, provenance_record=prov)
    inside = RevisionQueryFilters(business_at=_dt(7))
    outside = RevisionQueryFilters(business_at=_dt(9))
    assert select_latest_per_logical_key([entry], as_of=_dt(10), filters=inside)
    assert select_latest_per_logical_key([entry], as_of=_dt(10), filters=outside) == ()


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------
def test_naive_as_of_is_rejected(revision_one: LoadedRevision) -> None:
    with pytest.raises(ValidationError):
        select_latest_per_logical_key(
            [revision_one],
            as_of=datetime(2026, 11, 7, 12, 0),  # naive
        )


def test_timezone_equivalent_cutoffs_agree(revision_one: LoadedRevision) -> None:
    local = _dt(7)
    as_utc = local.astimezone(UTC)
    assert select_latest_per_logical_key(
        [revision_one], as_of=local
    ) == select_latest_per_logical_key([revision_one], as_of=as_utc)
