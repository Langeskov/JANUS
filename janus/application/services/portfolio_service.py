"""Portfolio service: valuation snapshots and target-to-order translation.

The Portfolio Manager compares current and target weights and computes rebalance
intents (Phase 0 spec s4.1 #4). It never submits orders and never touches the
risk policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from janus.application.ports.clock import Clock
from janus.application.ports.market_data import MarketDataProvider
from janus.domain.common.enums import OrderSide, OrderType, OrderValidity
from janus.domain.common.errors import NotFoundError, ValidationError
from janus.domain.common.ids import new_id
from janus.domain.common.money import CASH_SYMBOL, Money, quantize_cny
from janus.domain.instruments.models import Instrument
from janus.domain.ledger.replay import RebuiltAccount
from janus.domain.market_data.models import MarketBar
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.models import ResolvedTradingRules
from janus.domain.orders.models import OrderIntent
from janus.domain.portfolio.models import PortfolioSnapshot, Position, TargetAllocation

__all__ = ["AccountingReader", "PortfolioService", "RebalancePlan"]


class AccountingReader(Protocol):
    """The read-only accounting surface the portfolio manager requires."""

    def rebuild(self, account_id: str, *, as_of: date | None = None) -> RebuiltAccount: ...

    def available_quantity(self, account_id: str, symbol: str, *, as_of: date) -> int: ...


_BUY_PRICE_BUFFER = Decimal("0.03")
_SELL_PRICE_BUFFER = Decimal("0.03")


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    """Intents derived from a target allocation, plus why some were skipped."""

    intents: tuple[OrderIntent, ...]
    notes: tuple[str, ...]
    signal_bar_date: date
    earliest_execution_date: date
    valuation: PortfolioSnapshot


class PortfolioService:
    """Valuation and rebalance planning."""

    __slots__ = ("_accounting", "_calendar", "_clock", "_market_data")

    def __init__(
        self,
        *,
        accounting: AccountingReader,
        market_data: MarketDataProvider,
        calendar: TradingCalendar,
        clock: Clock,
    ) -> None:
        self._accounting: AccountingReader = accounting
        self._market_data = market_data
        self._calendar = calendar
        self._clock = clock

    # -- valuation -------------------------------------------------------
    def valuation_prices(
        self, symbols: Sequence[str], *, valuation_date: date, stale_after_days: int = 1
    ) -> dict[str, MarketBar | None]:
        """Latest usable bar at or before ``valuation_date`` for each symbol."""
        bars: dict[str, MarketBar | None] = {}
        for symbol in symbols:
            if symbol == CASH_SYMBOL:
                continue
            bar = self._market_data.get_daily_bar(symbol, valuation_date)
            if bar is None:
                bar = self._latest_bar_before(symbol, valuation_date, stale_after_days)
            bars[symbol] = bar
        return bars

    def _latest_bar_before(
        self, symbol: str, valuation_date: date, lookback_days: int
    ) -> MarketBar | None:
        for offset in range(1, max(lookback_days, 0) + 1):
            candidate = valuation_date - timedelta(days=offset)
            bar = self._market_data.get_daily_bar(symbol, candidate)
            if bar is not None:
                return bar
        return None

    def snapshot(
        self,
        *,
        account_id: str,
        valuation_date: date,
        data_snapshot_id: str | None = None,
        rule_set_id: str | None = None,
        as_of: datetime | None = None,
    ) -> PortfolioSnapshot:
        """Value the account at ``valuation_date`` using raw daily closes."""
        rebuilt = self._accounting.rebuild(account_id, as_of=valuation_date)
        symbols = sorted(rebuilt.positions)
        bars = self.valuation_prices(symbols, valuation_date=valuation_date)
        now = as_of or self._clock.now()

        positions: list[Position] = []
        oldest: datetime | None = None
        any_stale = False
        for symbol in symbols:
            position = rebuilt.positions[symbol]
            bar = bars.get(symbol)
            if bar is None:
                raise NotFoundError(
                    "no valuation bar available for a held instrument",
                    symbol=symbol,
                    valuation_date=valuation_date.isoformat(),
                )
            stale = bar.trade_date < valuation_date
            any_stale = any_stale or stale
            if oldest is None or bar.point_in_time.event_time < oldest:
                oldest = bar.point_in_time.event_time
            market_value = Money(amount=quantize_cny(bar.close * Decimal(position.quantity)))
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=position.quantity,
                    available_quantity=position.available_quantity,
                    cost_basis=position.cost_basis,
                    market_price=bar.close,
                    market_value=market_value,
                    price_as_of=bar.point_in_time.event_time,
                    price_is_stale=stale,
                )
            )

        gross = sum((p.market_value.amount for p in positions), Decimal("0"))
        nav = Money(amount=rebuilt.cash.amount + gross)
        unrealized = sum((p.unrealized_pnl.amount for p in positions), Decimal("0"))
        return PortfolioSnapshot(
            snapshot_id=new_id("psnap"),
            account_id=account_id,
            as_of=now,
            valuation_date=valuation_date,
            cash=rebuilt.cash,
            positions=tuple(positions),
            nav=nav,
            unrealized_pnl=Money(amount=unrealized),
            realized_pnl=rebuilt.realized_pnl,
            oldest_price_as_of=oldest,
            any_price_stale=any_stale,
            data_snapshot_id=data_snapshot_id,
            rule_set_id=rule_set_id,
        )

    # -- rebalance planning ---------------------------------------------
    def plan_rebalance(
        self,
        *,
        account_id: str,
        target: TargetAllocation,
        snapshot: PortfolioSnapshot,
        instruments: Mapping[str, Instrument],
        rules_by_symbol: Mapping[str, ResolvedTradingRules],
        signal_bar_date: date,
        decision_id: str,
        data_snapshot_id: str,
        strategy_id: str,
        strategy_version: str,
        rationale: str = "",
    ) -> RebalancePlan:
        """Translate target weights into validated-later :class:`OrderIntent`s.

        These intents are *unapproved*. Nothing here bypasses the risk
        controller: the caller must run each intent through it.
        """
        earliest_execution = self._calendar.next_trading_day(signal_bar_date)
        notes: list[str] = []
        intents: list[OrderIntent] = []

        prices: dict[str, Decimal] = {}
        bars = self.valuation_prices(
            [symbol for symbol in target.as_dict() if symbol != CASH_SYMBOL],
            valuation_date=signal_bar_date,
        )
        for symbol, bar in bars.items():
            if bar is None:
                notes.append(f"no signal bar for {symbol}; target ignored")
                continue
            prices[symbol] = bar.close

        nav = snapshot.nav.amount
        if nav <= 0:  # pragma: no cover - defensive
            raise ValidationError("cannot rebalance a non-positive NAV")

        cash = snapshot.cash.amount
        for symbol, weight in target.as_dict().items():
            if symbol == CASH_SYMBOL:
                continue
            price = prices.get(symbol)
            instrument = instruments.get(symbol)
            rules = rules_by_symbol.get(symbol)
            if price is None or instrument is None or rules is None:
                notes.append(f"skipping {symbol}: missing price, instrument or rules")
                continue

            target_value = nav * weight
            current_position = snapshot.position(symbol)
            current_value = (
                current_position.market_value.amount
                if current_position is not None
                else Decimal("0")
            )
            delta_value = target_value - current_value
            if abs(delta_value) < Decimal("0.01"):
                continue

            if delta_value > 0:
                side = OrderSide.BUY
                increment = rules.buy_quantity_increment
                spend_cap = min(delta_value, cash)
                if spend_cap < rules.min_buy_quantity * price:
                    notes.append(f"{symbol}: target delta too small to buy one lot")
                    continue
                quantity = (
                    int(
                        (spend_cap / price / Decimal(increment)).to_integral_value(
                            rounding="ROUND_FLOOR"
                        )
                    )
                    * increment
                )
                cash -= price * Decimal(quantity)
            else:
                side = OrderSide.SELL
                increment = rules.sell_quantity_increment
                available = self._accounting.available_quantity(
                    account_id, symbol, as_of=earliest_execution
                )
                if available <= 0:
                    notes.append(f"{symbol}: no available quantity to sell (T+1 settlement)")
                    continue
                desired = int((-delta_value / price).to_integral_value(rounding="ROUND_FLOOR"))
                quantity = min(desired, available)
                quantity = max(quantity, 1)

            if quantity <= 0:
                continue

            limit_price = self._limit_price(side, price, rules)
            intents.append(
                OrderIntent(
                    intent_id=new_id("intent"),
                    account_id=account_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    order_type=OrderType.LIMIT,
                    limit_price=limit_price,
                    validity=OrderValidity.DAY,
                    created_at=self._clock.now(),
                    earliest_execution_date=earliest_execution,
                    signal_bar_date=signal_bar_date,
                    decision_id=decision_id,
                    data_snapshot_id=data_snapshot_id,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    rule_set_id=rules.rule_set_id,
                    rule_set_version=rules.rule_set_version,
                    rationale=rationale,
                )
            )

        return RebalancePlan(
            intents=tuple(intents),
            notes=tuple(notes),
            signal_bar_date=signal_bar_date,
            earliest_execution_date=earliest_execution,
            valuation=snapshot,
        )

    @staticmethod
    def _limit_price(side: OrderSide, reference: Decimal, rules: ResolvedTradingRules) -> Decimal:
        if side is OrderSide.BUY:
            raw = reference * (Decimal(1) + _BUY_PRICE_BUFFER)
            price = raw.quantize(rules.tick_size, rounding="ROUND_CEILING")
        else:
            raw = reference * (Decimal(1) - _SELL_PRICE_BUFFER)
            price = raw.quantize(rules.tick_size, rounding="ROUND_FLOOR")
        if price <= 0:  # pragma: no cover - defensive
            price = rules.tick_size
        return price
