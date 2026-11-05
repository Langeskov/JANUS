"""Deterministic domain-object builders shared by the persistence tests.

These builders are intentionally explicit: every field is set so that a
round-trip through SQLite that drops or coerces any value fails the test.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

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
)
from janus.domain.common.ids import new_id
from janus.domain.common.money import Money
from janus.domain.common.time import MARKET_TZ, PointInTime
from janus.domain.instruments.models import Instrument
from janus.domain.ledger.models import (
    CashLedgerEntry,
    LedgerEntryType,
    PositionEvent,
    PositionEventType,
)
from janus.domain.market_data.models import DataSnapshot, MarketBar
from janus.domain.market_rules.models import (
    FeeBasis,
    FeeBreakdown,
    FeeLine,
)
from janus.domain.orders.models import ExecutionFill, PaperOrder
from janus.domain.portfolio.models import PortfolioAccount, PortfolioSnapshot, Position
from janus.domain.risk.models import RiskCheck, RiskDecision, RiskRuleId
from janus.domain.strategy.decision import DecisionRecord, DecisionType

ACCOUNT_ID = "acct-janus-paper-001"
SYMBOL = "600519.SH"
ETF_SYMBOL = "510300.SH"
RULE_SET_ID = "sse-szse-2026"
RULE_SET_VERSION = "2026.07.06"
FEE_SCHEDULE_ID = "CN-EQUITY-DEFAULT"
FEE_SCHEDULE_VERSION = "2026.07.06"
DATA_SNAPSHOT_ID = "dsnap-fixture-001"
DECISION_ID = "decision-fixture-001"
RISK_DECISION_ID = "risk-fixture-001"
TRADE_DATE = date(2026, 11, 5)
NEXT_TRADE_DATE = date(2026, 11, 6)
BAR_DATE = date(2026, 11, 4)

T0 = datetime(2026, 11, 5, 9, 30, tzinfo=MARKET_TZ)
"""Aware Asia/Shanghai timestamp used for every fixture."""

INITIAL_CAPITAL = Money(amount=Decimal("100000.00"))


def point_in_time() -> PointInTime:
    """A consistent point-in-time provenance record."""
    return PointInTime(
        event_time=datetime(2026, 11, 4, 15, 0, tzinfo=MARKET_TZ),
        published_at=datetime(2026, 11, 4, 18, 0, tzinfo=MARKET_TZ),
        retrieved_at=datetime(2026, 11, 5, 8, 0, tzinfo=MARKET_TZ),
        known_at=datetime(2026, 11, 5, 8, 30, tzinfo=MARKET_TZ),
    )


def make_instrument(
    symbol: str = SYMBOL,
    *,
    name: str = "Kweichow Moutai",
    instrument_type: InstrumentType = InstrumentType.A_SHARE,
    lot_quantity: int | None = 100,
) -> Instrument:
    return Instrument(
        symbol=symbol,
        name=name,
        exchange=Exchange.SSE,
        board=Board.SSE_MAIN,
        instrument_type=instrument_type,
        status=SecurityStatus.ACTIVE,
        listing_date=date(2001, 8, 27),
        lot_quantity=lot_quantity,
        is_etf=instrument_type is InstrumentType.DOMESTIC_EQUITY_ETF,
    )


def make_etf_instrument() -> Instrument:
    return Instrument(
        symbol=ETF_SYMBOL,
        name="CSI 300 ETF",
        exchange=Exchange.SSE,
        board=Board.SSE_MAIN,
        instrument_type=InstrumentType.DOMESTIC_EQUITY_ETF,
        status=SecurityStatus.ACTIVE,
        listing_date=date(2012, 5, 28),
        lot_quantity=100,
        is_etf=True,
    )


def make_account(
    account_id: str = ACCOUNT_ID,
    *,
    initial_capital: Money = INITIAL_CAPITAL,
    created_at: datetime = T0,
) -> PortfolioAccount:
    return PortfolioAccount.create_paper_account(
        account_id=account_id,
        initial_capital=initial_capital,
        created_at=created_at,
    )


def make_opening_entry(
    account: PortfolioAccount,
    *,
    sequence: int = 1,
    occurred_at: datetime = T0,
    trade_date: date = TRADE_DATE,
    idempotency_key: str = "opening-capital-v1",
) -> CashLedgerEntry:
    return CashLedgerEntry(
        entry_id=new_id("cash"),
        account_id=account.account_id,
        sequence=sequence,
        entry_type=LedgerEntryType.OPENING_CAPITAL,
        amount=account.initial_capital,
        trade_date=trade_date,
        occurred_at=occurred_at,
        source_ref=account.account_id,
        idempotency_key=idempotency_key,
        description="Opening fixed paper capital",
        balance_after=account.initial_capital,
    )


def make_cash_entry(
    account_id: str,
    *,
    sequence: int,
    amount: Money,
    entry_type: LedgerEntryType,
    idempotency_key: str,
    source_ref: str,
    occurred_at: datetime = T0,
    trade_date: date = TRADE_DATE,
    balance_after: Money | None = None,
) -> CashLedgerEntry:
    return CashLedgerEntry(
        entry_id=new_id("cash"),
        account_id=account_id,
        sequence=sequence,
        entry_type=entry_type,
        amount=amount,
        trade_date=trade_date,
        occurred_at=occurred_at,
        source_ref=source_ref,
        idempotency_key=idempotency_key,
        balance_after=balance_after,
    )


def make_position_acquisition(
    account_id: str,
    *,
    sequence: int,
    symbol: str,
    quantity: int,
    unit_cost: Decimal,
    trade_date: date = TRADE_DATE,
    available_date: date = NEXT_TRADE_DATE,
    idempotency_key: str,
    source_ref: str,
    occurred_at: datetime = T0,
) -> PositionEvent:
    cost_basis = Money(amount=unit_cost * Decimal(quantity))
    return PositionEvent(
        event_id=new_id("pevent"),
        account_id=account_id,
        sequence=sequence,
        event_type=PositionEventType.BUY_FILL,
        symbol=symbol,
        quantity_delta=quantity,
        trade_date=trade_date,
        occurred_at=occurred_at,
        source_ref=source_ref,
        idempotency_key=idempotency_key,
        available_date=available_date,
        unit_cost=unit_cost,
        cost_basis_delta=cost_basis,
        realized_pnl=Money(amount=Decimal("0")),
    )


def make_position_disposal(
    account_id: str,
    *,
    sequence: int,
    symbol: str,
    quantity: int,
    unit_cost: Decimal,
    realized_pnl: Money,
    trade_date: date = TRADE_DATE,
    idempotency_key: str,
    source_ref: str,
    occurred_at: datetime = T0,
) -> PositionEvent:
    return PositionEvent(
        event_id=new_id("pevent"),
        account_id=account_id,
        sequence=sequence,
        event_type=PositionEventType.SELL_FILL,
        symbol=symbol,
        quantity_delta=-quantity,
        trade_date=trade_date,
        occurred_at=occurred_at,
        source_ref=source_ref,
        idempotency_key=idempotency_key,
        cost_basis_delta=Money(amount=-(unit_cost * Decimal(quantity))),
        realized_pnl=realized_pnl,
    )


def make_order(
    *,
    account_id: str = ACCOUNT_ID,
    symbol: str = SYMBOL,
    side: OrderSide = OrderSide.BUY,
    quantity: int = 100,
    status: OrderStatus = OrderStatus.SUBMITTED_PAPER,
    order_id: str | None = None,
    limit_price: Decimal | None = Decimal("100.00"),
    target_trade_date: date = TRADE_DATE,
    filled_quantity: int = 0,
    terminal_at: datetime | None = None,
) -> PaperOrder:
    order_key = order_id or new_id("order")
    return PaperOrder(
        order_id=order_key,
        intent_id=new_id("intent"),
        account_id=account_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        filled_quantity=filled_quantity,
        order_type=OrderType.LIMIT,
        limit_price=limit_price,
        validity=OrderValidity.DAY,
        status=status,
        created_at=T0,
        updated_at=T0,
        target_trade_date=target_trade_date,
        earliest_execution_date=target_trade_date,
        rule_set_id=RULE_SET_ID,
        rule_set_version=RULE_SET_VERSION,
        fee_schedule_id=FEE_SCHEDULE_ID,
        fee_schedule_version=FEE_SCHEDULE_VERSION,
        data_snapshot_id=DATA_SNAPSHOT_ID,
        decision_id=DECISION_ID,
        strategy_id="broad_etf_core",
        strategy_version="0.1.0",
        risk_decision_id=RISK_DECISION_ID,
        approved_at=T0,
        submitted_at=T0,
        terminal_at=terminal_at,
    )


def make_fee_breakdown(
    *,
    side: OrderSide = OrderSide.BUY,
    notional: Decimal = Decimal("10000.00"),
) -> FeeBreakdown:
    return FeeBreakdown(
        fee_schedule_id=FEE_SCHEDULE_ID,
        fee_schedule_version=FEE_SCHEDULE_VERSION,
        side=side,
        notional=notional,
        lines=(
            FeeLine(
                component=FeeComponent.BROKER_COMMISSION,
                basis=FeeBasis.NOTIONAL,
                rate=Decimal("0.0005"),
                amount=Decimal("5.00"),
            ),
            FeeLine(
                component=FeeComponent.TRANSFER_FEE,
                basis=FeeBasis.QUANTITY,
                rate=Decimal("0.001"),
                amount=Decimal("0.10"),
            ),
        ),
    )


def make_fill(
    order: PaperOrder,
    *,
    fill_id: str | None = None,
    quantity: int | None = None,
    price: Decimal = Decimal("100.00"),
    fees: FeeBreakdown | None = None,
    is_partial: bool = False,
    idempotency_key: str | None = None,
    trade_date: date = TRADE_DATE,
    executed_at: datetime = T0,
) -> ExecutionFill:
    executed_quantity = quantity if quantity is not None else order.quantity
    notional = Decimal(price) * Decimal(executed_quantity)
    key = idempotency_key or f"{fill_id or new_id('fill')}:delivery"
    return ExecutionFill(
        fill_id=fill_id or new_id("fill"),
        order_id=order.order_id,
        account_id=order.account_id,
        symbol=order.symbol,
        side=order.side,
        quantity=executed_quantity,
        price=price,
        notional=Money(amount=notional),
        fees=fees if fees is not None else make_fee_breakdown(side=order.side, notional=notional),
        executed_at=executed_at,
        trade_date=trade_date,
        slippage_bps=Decimal("5"),
        is_partial=is_partial,
        rule_set_id=RULE_SET_ID,
        rule_set_version=RULE_SET_VERSION,
        fee_schedule_id=FEE_SCHEDULE_ID,
        fee_schedule_version=FEE_SCHEDULE_VERSION,
        data_snapshot_id=DATA_SNAPSHOT_ID,
        decision_id=DECISION_ID,
        idempotency_key=key,
    )


def make_risk_decision(
    *,
    account_id: str = ACCOUNT_ID,
    risk_decision_id: str = RISK_DECISION_ID,
    order_intent_id: str = "intent-fixture-001",
) -> RiskDecision:
    return RiskDecision(
        risk_decision_id=risk_decision_id,
        order_intent_id=order_intent_id,
        account_id=account_id,
        outcome=RiskOutcome.PASS,
        checks=(
            RiskCheck.ok(
                RiskRuleId.AVAILABLE_CASH,
                measured="10000.00",
                threshold="100000.00",
            ),
            RiskCheck.ok(RiskRuleId.PRICE_TICK),
        ),
        policy_version="0.1.0",
        decided_at=T0,
        rule_set_id=RULE_SET_ID,
        rule_set_version=RULE_SET_VERSION,
        data_snapshot_id=DATA_SNAPSHOT_ID,
        summary="All deterministic checks passed",
    )


def make_decision_record(
    *,
    account_id: str | None = ACCOUNT_ID,
    decision_id: str = DECISION_ID,
) -> DecisionRecord:
    return DecisionRecord(
        decision_id=decision_id,
        decision_type=DecisionType.ALLOCATION_PROPOSAL,
        created_at=T0,
        as_of=T0,
        account_id=account_id,
        proposal_id="proposal-fixture-001",
        strategy_id="broad_etf_core",
        strategy_version="0.1.0",
        model_id="deepseek-chat",
        prompt_version="prompt-v1",
        llm_request_id="req-fixture-001",
        data_snapshot_id=DATA_SNAPSHOT_ID,
        rule_set_id=RULE_SET_ID,
        rule_set_version=RULE_SET_VERSION,
        risk_decision_id=RISK_DECISION_ID,
        inputs={
            "target_weights": {SYMBOL: "0.20", "CASH.CNY": "0.80"},
            "horizon_days": 20,
        },
        thesis="Deterministic fixture thesis; not a live recommendation.",
        evidence_refs=(f"snapshot:{DATA_SNAPSHOT_ID}",),
        uncertainties=("Fixture data only",),
        outcome="ACCEPTED",
        executable=False,
    )


def make_data_snapshot(snapshot_id: str = DATA_SNAPSHOT_ID) -> DataSnapshot:
    return DataSnapshot(
        snapshot_id=snapshot_id,
        created_at=T0,
        as_of=T0,
        provider=QuoteSource.FIXTURE,
        symbols=(SYMBOL, ETF_SYMBOL),
        bar_dates=(BAR_DATE,),
        bar_count=1,
        content_hash="sha256:0123456789abcdef",
        quality=DataQualityStatus.OK,
        rule_set_id=RULE_SET_ID,
        notes="Deterministic fixture snapshot",
    )


def make_position(
    *,
    symbol: str = SYMBOL,
    quantity: int = 120,
    available_quantity: int = 120,
    cost_basis: str = "12106.05",
    market_price: str = "105.00",
) -> Position:
    price = Decimal(market_price)
    return Position(
        symbol=symbol,
        quantity=quantity,
        available_quantity=available_quantity,
        cost_basis=Money(amount=Decimal(cost_basis)),
        market_price=price,
        market_value=Money(amount=price * Decimal(quantity)),
        price_as_of=T0,
        price_is_stale=False,
    )


def make_portfolio_snapshot(
    *,
    account_id: str = ACCOUNT_ID,
    cash: str = "88276.55",
    position: Position | None = None,
    realized_pnl: str = "382.60",
    as_of: datetime = T0,
    valuation_date: date = TRADE_DATE,
) -> PortfolioSnapshot:
    held = position if position is not None else make_position()
    cash_money = Money(amount=Decimal(cash))
    nav = Money(amount=cash_money.amount + held.market_value.amount)
    return PortfolioSnapshot(
        snapshot_id=new_id("psnap"),
        account_id=account_id,
        as_of=as_of,
        valuation_date=valuation_date,
        cash=cash_money,
        positions=(held,),
        nav=nav,
        unrealized_pnl=Money(amount=held.market_value.amount - held.cost_basis.amount),
        realized_pnl=Money(amount=Decimal(realized_pnl)),
        oldest_price_as_of=as_of,
        any_price_stale=False,
        data_snapshot_id=DATA_SNAPSHOT_ID,
        rule_set_id=RULE_SET_ID,
    )


def make_market_bar(
    *,
    symbol: str = SYMBOL,
    trade_date: date = BAR_DATE,
) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        trade_date=trade_date,
        open=Decimal("100.00"),
        high=Decimal("102.00"),
        low=Decimal("99.50"),
        close=Decimal("101.00"),
        volume=1_000_000,
        amount=Decimal("100800000.00"),
        adjustment=AdjustmentMode.UNADJUSTED,
        quality=DataQualityStatus.OK,
        source=QuoteSource.FIXTURE,
        source_ref=f"fixture:{symbol}:{trade_date.isoformat()}",
        raw_payload_hash="sha256:fedcba9876543210",
        point_in_time=point_in_time(),
    )
