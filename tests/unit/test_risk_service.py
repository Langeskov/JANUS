"""Unit tests for :class:`RiskService` orchestration using a fake unit of work.

These tests prove the wrapper gathers the trusted inputs, persists the risk
decision and its decision-lineage record, and returns an order that is either
``RISK_APPROVED`` or ``RISK_REJECTED`` -- never executable on rejection.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from janus.application.ports.clock import FixedClock
from janus.application.services.risk_service import RiskService
from janus.domain.common.enums import (
    AdjustmentMode,
    Board,
    DataQualityStatus,
    Exchange,
    FeeComponent,
    InstrumentType,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidity,
    QuoteSource,
    RiskOutcome,
    SecurityStatus,
    SessionType,
    SettlementRule,
    TradingDayType,
)
from janus.domain.common.errors import ValidationError
from janus.domain.common.money import Money
from janus.domain.common.time import MARKET_TZ, PointInTime
from janus.domain.instruments.models import Instrument
from janus.domain.market_data.models import DataSnapshot, MarketBar
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.models import (
    FeeBasis,
    FeeBreakdown,
    FeeLine,
    ResolvedTradingRules,
    TradingDay,
    TradingSession,
)
from janus.domain.orders.models import OrderIntent, PaperOrder
from janus.domain.orders.state_machine import GATEWAY_ADMITTED
from janus.domain.portfolio.models import PortfolioAccount, PortfolioSnapshot
from janus.domain.risk.models import RiskDecision, RiskPolicy, RiskRuleId
from janus.domain.strategy.decision import DecisionRecord, DecisionType

ACCOUNT_ID = "janus-paper-cny-001"
SYMBOL = "600519.SH"
NOW = datetime(2026, 11, 5, 15, 30, tzinfo=MARKET_TZ)
T_DATE = date(2026, 11, 5)
EXEC_DATE = date(2026, 11, 6)
RULE_SET_ID = "SSE-SZSE-2026"
RULE_SET_VERSION = "2026.07.06"
DECISION_ID = "decision-1"
SNAPSHOT_ID = "dsnap-1"


# ---------------------------------------------------------------------------
# Fake repositories / unit of work
# ---------------------------------------------------------------------------
class _Accounts:
    def __init__(self, account: PortfolioAccount | None) -> None:
        self._account = account

    def get(self, account_id: str) -> PortfolioAccount | None:
        if self._account is not None and self._account.account_id == account_id:
            return self._account
        return None

    def save(self, account: PortfolioAccount) -> None:
        self._account = account

    def list_all(self) -> list[PortfolioAccount]:
        return [] if self._account is None else [self._account]


class _PortfolioSnapshots:
    def __init__(self, snapshot: PortfolioSnapshot | None) -> None:
        self._snapshot = snapshot

    def save(self, snapshot: PortfolioSnapshot) -> None:
        self._snapshot = snapshot

    def get(self, snapshot_id: str) -> PortfolioSnapshot | None:
        if self._snapshot is not None and self._snapshot.snapshot_id == snapshot_id:
            return self._snapshot
        return None

    def latest_for_account(self, account_id: str) -> PortfolioSnapshot | None:
        if self._snapshot is not None and self._snapshot.account_id == account_id:
            return self._snapshot
        return None


class _Instruments:
    def __init__(self, instrument: Instrument | None) -> None:
        self._instrument = instrument

    def get(self, symbol: str) -> Instrument | None:
        if self._instrument is not None and self._instrument.symbol == symbol:
            return self._instrument
        return None

    def list_all(self) -> list[Instrument]:
        return [] if self._instrument is None else [self._instrument]


class _MarketBars:
    def __init__(self, bar: MarketBar | None) -> None:
        self._bar = bar

    def latest(self, symbol: str) -> MarketBar | None:
        if self._bar is not None and self._bar.symbol == symbol:
            return self._bar
        return None

    def get(self, symbol: str, trade_date: date) -> MarketBar | None:
        if (
            self._bar is not None
            and self._bar.symbol == symbol
            and self._bar.trade_date == trade_date
        ):
            return self._bar
        return None


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
    def __init__(self, record: DecisionRecord | None) -> None:
        self._records: list[DecisionRecord] = [] if record is None else [record]

    def get(self, decision_id: str) -> DecisionRecord | None:
        for record in self._records:
            if record.decision_id == decision_id:
                return record
        return None

    def append(self, record: DecisionRecord) -> None:
        self._records.append(record)

    def all_records(self) -> list[DecisionRecord]:
        return list(self._records)


class _RiskDecisions:
    def __init__(self) -> None:
        self.appended: list[RiskDecision] = []

    def append(self, decision: RiskDecision) -> None:
        self.appended.append(decision)


class _Orders:
    def __init__(self, orders: tuple[PaperOrder, ...] = ()) -> None:
        self._orders: list[PaperOrder] = list(orders)

    def get(self, order_id: str) -> PaperOrder | None:
        for order in self._orders:
            if order.order_id == order_id:
                return order
        return None

    def save(self, order: PaperOrder) -> None:
        self._orders.append(order)

    def count_orders_on(self, account_id: str, trade_date: date) -> int:
        return sum(
            1
            for order in self._orders
            if order.account_id == account_id and order.target_trade_date == trade_date
        )

    def list_by_trade_date(self, account_id: str, trade_date: date) -> list[PaperOrder]:
        return [
            order
            for order in self._orders
            if order.account_id == account_id and order.target_trade_date == trade_date
        ]


class _Fills:
    def list_by_trade_date(self, account_id: str, trade_date: date) -> list[object]:
        return []


class FakeUnitOfWork:
    """Minimal in-memory unit of work covering the methods RiskService uses."""

    def __init__(
        self,
        *,
        account: PortfolioAccount | None,
        portfolio: PortfolioSnapshot | None,
        instrument: Instrument | None,
        bar: MarketBar | None,
        data_snapshot: DataSnapshot | None,
        decision: DecisionRecord | None,
        orders: tuple[PaperOrder, ...] = (),
    ) -> None:
        self.accounts = _Accounts(account)
        self.portfolio_snapshots = _PortfolioSnapshots(portfolio)
        self.instruments = _Instruments(instrument)
        self.market_bars = _MarketBars(bar)
        self.data_snapshots = _DataSnapshots(data_snapshot)
        self.decisions = _Decisions(decision)
        self.risk_decisions = _RiskDecisions()
        self.orders = _Orders(orders)
        self.fills = _Fills()
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
def _sessions() -> tuple[TradingSession, ...]:
    return (
        TradingSession(
            session_type=SessionType.CONTINUOUS,
            start_time=time(9, 30),
            end_time=time(11, 30),
        ),
        TradingSession(
            session_type=SessionType.CONTINUOUS,
            start_time=time(13, 0),
            end_time=time(15, 0),
        ),
    )


def _calendar() -> TradingCalendar:
    # Mirrors the production fixture-calendar coverage (2026-07-06..2026-12-31),
    # which is longer than the 60-day default seasoning window.
    days: list[TradingDay] = []
    cursor = date(2026, 7, 6)
    while cursor <= date(2026, 12, 31):
        is_trading = cursor.weekday() < 5
        days.append(
            TradingDay(
                trade_date=cursor,
                day_type=TradingDayType.TRADING if is_trading else TradingDayType.WEEKEND,
                sessions=_sessions() if is_trading else (),
            )
        )
        cursor += timedelta(days=1)
    return TradingCalendar(days, name="risk-service-test")


def _account() -> PortfolioAccount:
    return PortfolioAccount.create_paper_account(
        account_id=ACCOUNT_ID,
        initial_capital=Money(amount=Decimal("100000")),
        created_at=NOW,
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot.empty(
        account_id=ACCOUNT_ID,
        cash=Money(amount=Decimal("100000")),
        as_of=NOW,
        valuation_date=T_DATE,
    )


def _instrument() -> Instrument:
    return Instrument(
        symbol=SYMBOL,
        name="Fixture Main Board Equity",
        exchange=Exchange.SSE,
        board=Board.SSE_MAIN,
        instrument_type=InstrumentType.A_SHARE,
        status=SecurityStatus.ACTIVE,
        listing_date=date(2020, 1, 6),
    )


def _pit(trade_date: date) -> PointInTime:
    event = datetime.combine(trade_date, time(15, 0), tzinfo=MARKET_TZ)
    return PointInTime(
        event_time=event,
        published_at=event,
        retrieved_at=event + timedelta(minutes=5),
        known_at=event + timedelta(minutes=5),
    )


def _bar() -> MarketBar:
    close = Decimal("9.90")
    return MarketBar(
        symbol=SYMBOL,
        trade_date=T_DATE,
        open=close - Decimal("0.05"),
        high=close + Decimal("0.05"),
        low=close - Decimal("0.10"),
        close=close,
        volume=1_000_000,
        adjustment=AdjustmentMode.UNADJUSTED,
        quality=DataQualityStatus.OK,
        point_in_time=_pit(T_DATE),
    )


def _rules() -> ResolvedTradingRules:
    return ResolvedTradingRules(
        rule_set_id=RULE_SET_ID,
        rule_set_version=RULE_SET_VERSION,
        profile_id="SSE-MAIN-A",
        symbol=SYMBOL,
        trade_date=EXEC_DATE,
        settlement_rule=SettlementRule.T_PLUS_1,
        tick_size=Decimal("0.01"),
        buy_quantity_increment=100,
        min_buy_quantity=100,
        sell_quantity_increment=1,
        price_limit_pct=Decimal("0.10"),
        risk_warning_price_limit_pct=Decimal("0.05"),
        allowed_order_types=(OrderType.LIMIT,),
        allowed_validities=(OrderValidity.DAY,),
        sessions=_sessions(),
        fee_schedule_id="CN-EQUITY-DEFAULT",
        fee_schedule_version=RULE_SET_VERSION,
        effective_from=datetime(2026, 7, 6, 0, 0, tzinfo=MARKET_TZ),
    )


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        account_id=ACCOUNT_ID,
        symbol=SYMBOL,
        side=OrderSide.BUY,
        quantity=1000,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10.00"),
        validity=OrderValidity.DAY,
        created_at=NOW,
        earliest_execution_date=EXEC_DATE,
        signal_bar_date=T_DATE,
        decision_id=DECISION_ID,
        data_snapshot_id=SNAPSHOT_ID,
        strategy_id="broad_etf_core",
        strategy_version="0.1.0",
        rule_set_id=RULE_SET_ID,
        rule_set_version=RULE_SET_VERSION,
    )


def _stored_decision() -> DecisionRecord:
    return DecisionRecord(
        decision_id=DECISION_ID,
        decision_type=DecisionType.ALLOCATION_PROPOSAL,
        created_at=NOW,
        as_of=NOW,
        account_id=ACCOUNT_ID,
        strategy_id="broad_etf_core",
        strategy_version="0.1.0",
    )


def _data_snapshot() -> DataSnapshot:
    return DataSnapshot(
        snapshot_id=SNAPSHOT_ID,
        created_at=NOW,
        as_of=NOW - timedelta(minutes=5),
        provider=QuoteSource.FIXTURE,
        symbols=(SYMBOL,),
        bar_dates=(T_DATE,),
        bar_count=1,
        content_hash="sha256:fixture",
    )


def _uow(
    *,
    account: PortfolioAccount | None = None,
    portfolio: PortfolioSnapshot | None = None,
    instrument: Instrument | None = None,
    bar: MarketBar | None = None,
    data_snapshot: DataSnapshot | None = None,
    decision: DecisionRecord | None = None,
) -> FakeUnitOfWork:
    return FakeUnitOfWork(
        account=_account() if account is None else account,
        portfolio=_portfolio() if portfolio is None else portfolio,
        instrument=_instrument() if instrument is None else instrument,
        bar=_bar() if bar is None else bar,
        data_snapshot=_data_snapshot() if data_snapshot is None else data_snapshot,
        decision=_stored_decision() if decision is None else decision,
    )


def _raise_unresolvable_rules(
    instrument: Instrument,
    trade_date: date,
    *,
    at: datetime | None = None,
    new_listing_stage: bool = False,
) -> ResolvedTradingRules:
    raise ValidationError("no rule set covers this instrument/date", symbol=instrument.symbol)


def _fee_breakdown(
    rules: ResolvedTradingRules,
    side: OrderSide,
    quantity: int,
    price: Decimal,
) -> FeeBreakdown:
    notional = price * Decimal(quantity)
    commission = (notional * Decimal("0.00025")).quantize(Decimal("0.01"))
    amount = max(commission, Decimal("5.00"))
    return FeeBreakdown(
        fee_schedule_id=rules.fee_schedule_id,
        fee_schedule_version=rules.fee_schedule_version,
        side=side,
        notional=notional,
        lines=(
            FeeLine(
                component=FeeComponent.BROKER_COMMISSION,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.00025"),
                amount=amount,
            ),
        ),
    )


class _FakeEngine:
    def resolve(
        self,
        instrument: Instrument,
        trade_date: date,
        *,
        at: datetime | None = None,
        new_listing_stage: bool = False,
    ) -> ResolvedTradingRules:
        return _rules()

    def compute_fees(
        self,
        *,
        rules: ResolvedTradingRules,
        side: OrderSide,
        quantity: int,
        price: Decimal,
    ) -> FeeBreakdown:
        return _fee_breakdown(rules, side, quantity, price)


class _UnresolvableEngine:
    def resolve(
        self,
        instrument: Instrument,
        trade_date: date,
        *,
        at: datetime | None = None,
        new_listing_stage: bool = False,
    ) -> ResolvedTradingRules:
        return _raise_unresolvable_rules(instrument, trade_date)

    def compute_fees(
        self,
        *,
        rules: ResolvedTradingRules,
        side: OrderSide,
        quantity: int,
        price: Decimal,
    ) -> FeeBreakdown:  # pragma: no cover - resolve fails first
        raise ValidationError("no fee schedule available")


def _service(uow: FakeUnitOfWork) -> RiskService:
    return RiskService(
        uow_factory=FakeUoWFactory(uow),
        clock=FixedClock(NOW),
        policy=RiskPolicy(),
        calendar=_calendar(),
        engine=_FakeEngine(),
    )


# ---------------------------------------------------------------------------
# Approved path
# ---------------------------------------------------------------------------
def test_approved_intent_returns_risk_approved_order() -> None:
    uow = _uow()
    decision, order = _service(uow).evaluate(_intent())
    assert decision.outcome is RiskOutcome.PASS
    assert decision.approved
    assert order.status is OrderStatus.RISK_APPROVED
    assert order.status in GATEWAY_ADMITTED
    assert order.risk_decision_id == decision.risk_decision_id
    assert order.rule_set_id == RULE_SET_ID
    assert order.rule_set_version == RULE_SET_VERSION
    assert order.fee_schedule_id == "CN-EQUITY-DEFAULT"
    assert order.target_trade_date == EXEC_DATE
    assert order.earliest_execution_date == EXEC_DATE
    assert order.approved_at is not None
    assert order.rejection_reason is None


def test_decision_and_lineage_are_persisted() -> None:
    uow = _uow()
    decision, _order = _service(uow).evaluate(_intent())
    assert uow.risk_decisions.appended == [decision]
    records = uow.decisions.all_records()
    lineage = [record for record in records if record.decision_type is DecisionType.RISK_EVALUATION]
    assert len(lineage) == 1
    assert lineage[0].risk_decision_id == decision.risk_decision_id
    assert lineage[0].executable is True
    assert lineage[0].inputs["symbol"] == SYMBOL
    assert uow.commits == 1


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------
def test_missing_decision_record_is_rejected() -> None:
    # A decision reference that does not resolve to a stored decision must fail
    # closed (the known-decision set is authoritative, never "skip if empty").
    uow = FakeUnitOfWork(
        account=_account(),
        portfolio=_portfolio(),
        instrument=_instrument(),
        bar=_bar(),
        data_snapshot=_data_snapshot(),
        decision=None,
    )
    decision, order = _service(uow).evaluate(_intent())
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.DECISION_REFERENCE in decision.failed_rule_ids
    assert order.status is OrderStatus.RISK_REJECTED
    assert order.status not in GATEWAY_ADMITTED
    assert order.terminal_at is not None
    assert order.rejection_reason
    assert uow.risk_decisions.appended == [decision]
    assert uow.commits == 1


def test_missing_account_is_rejected_fail_closed() -> None:
    uow = _uow()
    uow.accounts = _Accounts(None)
    decision, order = _service(uow).evaluate(_intent())
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.ACCOUNT_ACTIVE in decision.failed_rule_ids
    assert order.status is OrderStatus.RISK_REJECTED
    # The intent's rule-set reference is preserved even when resolution failed.
    assert order.rule_set_id == RULE_SET_ID
    assert order.risk_decision_id == decision.risk_decision_id


def test_missing_portfolio_snapshot_is_rejected_fail_closed() -> None:
    uow = _uow()
    uow.portfolio_snapshots = _PortfolioSnapshots(None)
    decision, order = _service(uow).evaluate(_intent())
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.AVAILABLE_CASH in decision.failed_rule_ids
    assert order.status is OrderStatus.RISK_REJECTED


def test_unresolvable_rules_are_rejected_fail_closed() -> None:
    uow = _uow()
    service = RiskService(
        uow_factory=FakeUoWFactory(uow),
        clock=FixedClock(NOW),
        policy=RiskPolicy(),
        calendar=_calendar(),
        engine=_UnresolvableEngine(),
    )
    decision, order = service.evaluate(_intent())
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.RULESET_KNOWN in decision.failed_rule_ids
    assert order.status is OrderStatus.RISK_REJECTED
    assert order.fee_schedule_id == "UNRESOLVED-FEE-SCHEDULE"
    assert order.rule_set_id == RULE_SET_ID


def test_unknown_instrument_is_rejected_fail_closed() -> None:
    uow = _uow()
    uow.instruments = _Instruments(None)
    decision, order = _service(uow).evaluate(_intent())
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.INSTRUMENT_KNOWN in decision.failed_rule_ids
    assert order.status is OrderStatus.RISK_REJECTED


def test_paused_account_is_rejected() -> None:
    uow = _uow()
    uow.accounts = _Accounts(_account().paused("kill switch", NOW, new_version=2))
    decision, order = _service(uow).evaluate(_intent())
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.KILL_SWITCH_ENGAGED in decision.failed_rule_ids
    assert order.status is OrderStatus.RISK_REJECTED


def test_unknown_snapshot_reference_is_rejected() -> None:
    uow = _uow()
    uow.data_snapshots = _DataSnapshots(None)
    decision, order = _service(uow).evaluate(_intent())
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.DATA_SNAPSHOT_REFERENCE in decision.failed_rule_ids
    assert order.status is OrderStatus.RISK_REJECTED


def test_missing_market_bar_is_rejected() -> None:
    uow = _uow()
    uow.market_bars = _MarketBars(None)
    decision, order = _service(uow).evaluate(_intent())
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.MARKET_DATA_FRESH in decision.failed_rule_ids
    assert order.status is OrderStatus.RISK_REJECTED


def test_seasoning_window_is_enforced() -> None:
    recent_listing = Instrument(
        symbol=SYMBOL,
        name="Recent fixture listing",
        exchange=Exchange.SSE,
        board=Board.SSE_MAIN,
        instrument_type=InstrumentType.A_SHARE,
        status=SecurityStatus.ACTIVE,
        listing_date=date(2026, 10, 20),
    )
    uow = _uow()
    uow.instruments = _Instruments(recent_listing)
    decision, order = _service(uow).evaluate(_intent())
    assert decision.outcome is RiskOutcome.REJECT
    assert RiskRuleId.INSTRUMENT_ELIGIBLE in decision.failed_rule_ids
    assert order.status is OrderStatus.RISK_REJECTED


def test_borderline_cash_uses_engine_fees_or_explicit_estimate() -> None:
    uow = _uow()
    uow.portfolio_snapshots = _PortfolioSnapshots(
        PortfolioSnapshot.empty(
            account_id=ACCOUNT_ID,
            cash=Money(amount=Decimal("10004")),
            as_of=NOW,
            valuation_date=T_DATE,
        )
    )
    service = _service(uow)
    rejected_decision, _ = service.evaluate(_intent())
    assert RiskRuleId.AVAILABLE_CASH in rejected_decision.failed_rule_ids
    approved_decision, approved_order = service.evaluate(_intent(), estimated_fees=Decimal("2.00"))
    assert RiskRuleId.AVAILABLE_CASH not in approved_decision.failed_rule_ids
    assert (approved_order.status is OrderStatus.RISK_APPROVED) is approved_decision.approved
