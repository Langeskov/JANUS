"""Accounting service: the only writer of cash and position ledger entries.

Implements the Phase 0 accounting invariants (spec s5.1):

* initial capital is an opening ledger event;
* cash changes only through explicit entries (fills, fees, corporate actions);
* holdings change only through fills and corporate-action events;
* an order never changes cash or holdings before a fill is recorded;
* every fill carries a unique idempotency key, so replayed execution events are
  harmless;
* position availability follows the effective-dated settlement rule;
* balances are always reproducible from the opening event plus history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from janus.application.ports.clock import Clock
from janus.application.ports.repositories import UnitOfWork, UnitOfWorkFactory
from janus.domain.common.enums import OrderSide, SettlementRule
from janus.domain.common.errors import (
    InvariantViolation,
    NotFoundError,
    UnsupportedEntityError,
)
from janus.domain.common.ids import new_id
from janus.domain.common.money import ZERO_CNY, Money, quantize_cny
from janus.domain.ledger.models import (
    CashLedgerEntry,
    LedgerEntryType,
    PositionEvent,
    PositionEventType,
    RemainingLot,
)
from janus.domain.ledger.replay import (
    RebuiltAccount,
    rebuild_account,
    replay_cash,
    replay_positions,
)
from janus.domain.market_data.models import CorporateAction, CorporateActionType
from janus.domain.market_rules.calendar import TradingCalendar
from janus.domain.market_rules.models import ResolvedTradingRules
from janus.domain.orders.models import ExecutionFill, PaperOrder
from janus.domain.portfolio.models import PortfolioAccount
from janus.infrastructure.config.loader import AccountConfig

__all__ = ["AccountingService", "CorporateActionResult", "FillRecording"]


@dataclass(frozen=True, slots=True)
class FillRecording:
    """What :meth:`AccountingService.record_fill` actually wrote."""

    fill: ExecutionFill
    duplicate: bool
    cash_entries: tuple[CashLedgerEntry, ...]
    position_event: PositionEvent | None
    cash_after: Money
    realized_pnl: Money
    available_quantity_after: int


@dataclass(frozen=True, slots=True)
class CorporateActionResult:
    """Outcome of applying a corporate action to paper holdings."""

    action_id: str
    cash_entry: CashLedgerEntry | None
    cash_after: Money
    description: str


class AccountingService:
    """Deterministic paper-account accounting over the append-only ledger."""

    __slots__ = ("_calendar", "_clock", "_uow_factory")

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        calendar: TradingCalendar,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._calendar = calendar

    # -- account opening -------------------------------------------------
    def open_account(self, config: AccountConfig) -> PortfolioAccount:
        """Create the fixed-capital paper account exactly once (idempotent)."""
        now = self._clock.now()
        account = PortfolioAccount.create_paper_account(
            account_id=config.id,
            initial_capital=Money(amount=config.initial_capital),
            created_at=now,
        )
        with self._uow_factory() as uow:
            existing = uow.accounts.get(config.id)
            if existing is not None:
                return existing
            uow.accounts.save(account)
            opening = CashLedgerEntry(
                entry_id=new_id("cash"),
                account_id=account.account_id,
                sequence=1,
                entry_type=LedgerEntryType.OPENING_CAPITAL,
                amount=account.initial_capital,
                trade_date=now.date(),
                occurred_at=now,
                source_ref=f"account:{account.account_id}",
                idempotency_key=f"{account.account_id}:opening-capital:v{account.version}",
                description="Opening paper capital (fixed principal, no later flows)",
                balance_after=account.initial_capital,
            )
            uow.cash_ledger.append(opening)
            uow.commit()
        return account

    def get_account(self, account_id: str, *, uow: UnitOfWork | None = None) -> PortfolioAccount:
        if uow is not None:
            account = uow.accounts.get(account_id)
        else:
            with self._uow_factory() as own:
                account = own.accounts.get(account_id)
        if account is None:
            raise NotFoundError("account not found", account_id=account_id)
        return account

    # -- queries ---------------------------------------------------------
    def cash_balance(self, account_id: str, *, uow: UnitOfWork | None = None) -> Money:
        entries = self._cash_entries(account_id, uow=uow)
        return replay_cash(entries).balance

    def positions(
        self, account_id: str, *, as_of: date, uow: UnitOfWork | None = None
    ) -> dict[str, object]:
        events = self._position_events(account_id, uow=uow)
        return dict(replay_positions(events, as_of=as_of))

    def available_quantity(
        self, account_id: str, symbol: str, *, as_of: date, uow: UnitOfWork | None = None
    ) -> int:
        events = self._position_events(account_id, uow=uow)
        rebuilt = replay_positions(events, as_of=as_of)
        position = rebuilt.get(symbol)
        return 0 if position is None else position.available_quantity

    def lots(
        self, account_id: str, symbol: str, *, as_of: date, uow: UnitOfWork | None = None
    ) -> tuple[RemainingLot, ...]:
        events = self._position_events(account_id, uow=uow)
        rebuilt = replay_positions(events, as_of=as_of)
        position = rebuilt.get(symbol)
        return () if position is None else position.lots

    def rebuild(self, account_id: str, *, as_of: date | None = None) -> RebuiltAccount:
        """Rebuild the account from immutable history (acceptance criterion 3)."""
        with self._uow_factory() as uow:
            account = uow.accounts.get(account_id)
            if account is None:
                raise NotFoundError("account not found", account_id=account_id)
            entries = uow.cash_ledger.list_for_account(account_id)
            events = uow.position_ledger.list_for_account(account_id)
        return rebuild_account(
            opening_capital=account.initial_capital,
            cash_entries=entries,
            position_events=events,
            as_of=as_of,
        )

    # -- writes ----------------------------------------------------------
    def record_fill(
        self,
        *,
        fill: ExecutionFill,
        order: PaperOrder | None,
        rules: ResolvedTradingRules,
        uow: UnitOfWork | None = None,
    ) -> FillRecording:
        """Record a fill's cash, fee and position effects exactly once."""
        if uow is not None:
            return self._record_fill_inner(fill=fill, order=order, rules=rules, uow=uow)
        with self._uow_factory() as own:
            recording = self._record_fill_inner(fill=fill, order=order, rules=rules, uow=own)
            own.commit()
            return recording

    def _record_fill_inner(
        self,
        *,
        fill: ExecutionFill,
        order: PaperOrder | None,
        rules: ResolvedTradingRules,
        uow: UnitOfWork,
    ) -> FillRecording:
        account_id = fill.account_id
        existing = uow.fills.get_by_idempotency_key(fill.idempotency_key)
        if existing is not None:
            return FillRecording(
                fill=existing,
                duplicate=True,
                cash_entries=(),
                position_event=None,
                cash_after=self._cash_balance_in(uow, account_id),
                realized_pnl=ZERO_CNY,
                available_quantity_after=0,
            )

        self._clock.now()
        fees = fill.fees.total
        fill_id = fill.fill_id

        settlement_sequence = uow.cash_ledger.next_sequence(account_id)
        fee_sequence = settlement_sequence + 1
        position_sequence = uow.position_ledger.next_sequence(account_id)

        settlement_amount = -fill.notional if fill.side is OrderSide.BUY else fill.notional
        settlement_entry = CashLedgerEntry(
            entry_id=new_id("cash"),
            account_id=account_id,
            sequence=settlement_sequence,
            entry_type=LedgerEntryType.FILL_SETTLEMENT,
            amount=settlement_amount,
            trade_date=fill.trade_date,
            occurred_at=fill.executed_at,
            source_ref=fill_id,
            idempotency_key=f"{fill.idempotency_key}:settlement",
            description=(
                f"{fill.side.value} {fill.quantity} {fill.symbol} @ {fill.price} "
                f"(fill model {fill.fill_model_version})"
            ),
        )
        fee_entry = CashLedgerEntry(
            entry_id=new_id("cash"),
            account_id=account_id,
            sequence=fee_sequence,
            entry_type=LedgerEntryType.FILL_FEE,
            amount=Money(amount=-fees),
            trade_date=fill.trade_date,
            occurred_at=fill.executed_at,
            source_ref=fill_id,
            idempotency_key=f"{fill.idempotency_key}:fee",
            description=(
                f"fees for {fill_id}: "
                + ", ".join(f"{line.component.value}={line.amount}" for line in fill.fees.lines)
            ),
        )

        position_event: PositionEvent
        realized = ZERO_CNY

        if fill.side is OrderSide.BUY:
            available_date = self._available_date(fill.trade_date, rules.settlement_rule)
            total_cost = Money(amount=fill.notional.amount + fees)
            unit_cost = (total_cost.amount / Decimal(fill.quantity)).quantize(Decimal("0.0001"))
            position_event = PositionEvent(
                event_id=new_id("pos"),
                account_id=account_id,
                sequence=position_sequence,
                event_type=PositionEventType.BUY_FILL,
                symbol=fill.symbol,
                quantity_delta=fill.quantity,
                trade_date=fill.trade_date,
                occurred_at=fill.executed_at,
                source_ref=fill_id,
                idempotency_key=f"{fill.idempotency_key}:position",
                available_date=available_date,
                unit_cost=unit_cost,
                cost_basis_delta=total_cost,
                realized_pnl=ZERO_CNY,
                description=f"buy lot from {fill_id} ({rules.settlement_rule.value})",
            )
        else:
            events = uow.position_ledger.list_for_account(account_id)
            rebuilt = replay_positions(events, as_of=fill.trade_date)
            position = rebuilt.get(fill.symbol)
            held = 0 if position is None else position.quantity
            if position is None or held < fill.quantity:
                raise InvariantViolation(
                    "cannot record a sale larger than the position held",
                    symbol=fill.symbol,
                    held=held,
                    selling=fill.quantity,
                )
            consumed_cost = self._fifo_cost(position.lots, fill.quantity)
            net_proceeds = Money(amount=fill.notional.amount - fees)
            realized = Money(amount=net_proceeds.amount - consumed_cost.amount)
            position_event = PositionEvent(
                event_id=new_id("pos"),
                account_id=account_id,
                sequence=position_sequence,
                event_type=PositionEventType.SELL_FILL,
                symbol=fill.symbol,
                quantity_delta=-fill.quantity,
                trade_date=fill.trade_date,
                occurred_at=fill.executed_at,
                source_ref=fill_id,
                idempotency_key=f"{fill.idempotency_key}:position",
                cost_basis_delta=Money(amount=-consumed_cost.amount),
                realized_pnl=realized,
                description=f"sale lot from {fill_id}",
            )

        # Parent (order) must exist before the fill row can reference it.
        if order is not None:
            uow.orders.save(order)
        uow.cash_ledger.append(settlement_entry)
        uow.cash_ledger.append(fee_entry)
        uow.position_ledger.append(position_event)
        uow.fills.append(fill)

        # Cached running balances keep the audit trail human-readable; replay
        # verifies them and fails loudly if they ever disagree.
        updated_settlement = settlement_entry.model_copy(
            update={"balance_after": self._balance_after(uow, account_id, settlement_entry)}
        )
        updated_fee = fee_entry.model_copy(
            update={"balance_after": self._balance_after(uow, account_id, fee_entry)}
        )

        cash_after = self._cash_balance_in(uow, account_id)
        available_after = self._available_after(uow, account_id, fill, rules)
        return FillRecording(
            fill=fill,
            duplicate=False,
            cash_entries=(updated_settlement, updated_fee),
            position_event=position_event,
            cash_after=cash_after,
            realized_pnl=realized,
            available_quantity_after=available_after,
        )

    def apply_corporate_action(
        self,
        action: CorporateAction,
        *,
        account_id: str,
        held_quantity: int,
        uow: UnitOfWork | None = None,
    ) -> CorporateActionResult:
        """Apply the cash effects of a corporate action to paper holdings.

        Quantity-changing actions (splits, stock dividends, rights issues) are
        explicitly out of Phase 0 scope: they are recorded for research and
        represented through separate adjustment factors, never by rewriting
        historical execution prices. Attempting one fails closed.
        """
        if uow is not None:
            return self._apply_action_inner(
                action, account_id=account_id, held_quantity=held_quantity, uow=uow
            )
        with self._uow_factory() as own:
            result = self._apply_action_inner(
                action, account_id=account_id, held_quantity=held_quantity, uow=own
            )
            own.commit()
            return result

    def _apply_action_inner(
        self,
        action: CorporateAction,
        *,
        account_id: str,
        held_quantity: int,
        uow: UnitOfWork,
    ) -> CorporateActionResult:
        if action.action_type is not CorporateActionType.CASH_DIVIDEND:
            raise UnsupportedEntityError(
                "Phase 0 applies cash dividends only; share-changing corporate "
                "actions are recorded for research via adjustment factors and do "
                "not rewrite historical prices or quantities",
                action_type=action.action_type.value,
                action_id=action.action_id,
            )
        if held_quantity <= 0:
            raise InvariantViolation(
                "a cash dividend requires a non-zero held quantity",
                action_id=action.action_id,
            )
        if action.cash_per_share is None:  # pragma: no cover - model validated
            raise InvariantViolation("dividend has no cash_per_share")

        amount = quantize_cny(action.cash_per_share * Decimal(held_quantity))
        now = self._clock.now()
        entry = CashLedgerEntry(
            entry_id=new_id("cash"),
            account_id=account_id,
            sequence=uow.cash_ledger.next_sequence(account_id),
            entry_type=LedgerEntryType.CORPORATE_ACTION_CASH,
            amount=Money(amount=amount),
            trade_date=action.pay_date or action.ex_date,
            occurred_at=now,
            source_ref=action.action_id,
            idempotency_key=f"corporate-action:{action.action_id}:{account_id}",
            description=f"cash dividend {action.cash_per_share} x {held_quantity}",
        )
        uow.cash_ledger.append(entry)
        return CorporateActionResult(
            action_id=action.action_id,
            cash_entry=entry,
            cash_after=self._cash_balance_in(uow, account_id),
            description=entry.description,
        )

    # -- helpers ---------------------------------------------------------
    def _cash_entries(
        self, account_id: str, *, uow: UnitOfWork | None = None
    ) -> list[CashLedgerEntry]:
        if uow is not None:
            return uow.cash_ledger.list_for_account(account_id)
        with self._uow_factory() as own:
            return own.cash_ledger.list_for_account(account_id)

    def _position_events(
        self, account_id: str, *, uow: UnitOfWork | None = None
    ) -> list[PositionEvent]:
        if uow is not None:
            return uow.position_ledger.list_for_account(account_id)
        with self._uow_factory() as own:
            return own.position_ledger.list_for_account(account_id)

    def _cash_balance_in(self, uow: UnitOfWork, account_id: str) -> Money:
        return replay_cash(uow.cash_ledger.list_for_account(account_id)).balance

    def _balance_after(self, uow: UnitOfWork, account_id: str, entry: CashLedgerEntry) -> Money:
        entries = [
            e for e in uow.cash_ledger.list_for_account(account_id) if e.sequence <= entry.sequence
        ]
        return replay_cash(entries).balance

    def _available_after(
        self, uow: UnitOfWork, account_id: str, fill: ExecutionFill, rules: ResolvedTradingRules
    ) -> int:
        events = uow.position_ledger.list_for_account(account_id)
        rebuilt = replay_positions(events, as_of=fill.trade_date)
        position = rebuilt.get(fill.symbol)
        if position is None:
            return 0
        if rules.settlement_rule is SettlementRule.T_PLUS_0:
            return position.quantity
        return position.available_quantity

    def _available_date(self, trade_date: date, settlement: SettlementRule) -> date:
        if settlement is SettlementRule.T_PLUS_0:
            return trade_date
        return self._calendar.settlement_available_date(trade_date)

    @staticmethod
    def _fifo_cost(lots: Sequence[RemainingLot], quantity: int) -> Money:
        """Cost basis released by selling ``quantity`` units FIFO."""
        ordered = sorted(
            lots,
            key=lambda lot: (lot.available_date, lot.acquired_trade_date, lot.lot_id),
        )
        remaining = quantity
        total = ZERO_CNY
        for lot in ordered:
            if remaining <= 0:
                break
            if lot.remaining_quantity <= 0:
                continue
            take = min(lot.remaining_quantity, remaining)
            total = total + Money(amount=lot.unit_cost * Decimal(take))
            remaining -= take
        if remaining > 0:
            raise InvariantViolation(
                "insufficient held quantity for the requested sale",
                requested=quantity,
                shortfall=remaining,
            )
        return total

    def values_at(
        self, account_id: str, *, as_of: date, prices: Mapping[str, Decimal]
    ) -> tuple[Money, dict[str, Decimal]]:
        """Return (gross market value, per-symbol market value) for holdings."""
        rebuilt = replay_positions(self._position_events(account_id), as_of=as_of)
        values: dict[str, Decimal] = {}
        total = ZERO_CNY
        for symbol, position in rebuilt.items():
            price = prices.get(symbol)
            if price is None:
                raise NotFoundError(
                    "no valuation price for a held symbol", symbol=symbol, as_of=str(as_of)
                )
            value = quantize_cny(price * Decimal(position.quantity))
            values[symbol] = value
            total = total + Money(amount=value)
        return total, values
