"""Composition root: build a fully wired Phase 0 application container.

This is the only place where infrastructure objects are constructed and injected
into application services. Transport layers (CLI/API) receive the resulting
:class:`~janus.application.container.JanusContainer` and never touch a
repository, a session, or a provider.

Everything here is deterministic given the same database, clock and fixture
data: no network, no wall-clock dependence beyond the injected ``Clock``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from janus.agents.schemas import parse_proposal_payload
from janus.application.container import (
    AccountSummary,
    AuditEntry,
    ComponentHealth,
    CycleResult,
    HealthReport,
    JanusContainer,
    OrderSummary,
    PositionSummary,
    ProposalOutcome,
    RebuildResult,
)
from janus.application.ports.clock import Clock, SystemClock
from janus.application.ports.repositories import UnitOfWorkFactory
from janus.application.services.accounting_service import AccountingService
from janus.application.services.portfolio_service import PortfolioService
from janus.application.services.proposal_service import ProposalService
from janus.application.services.risk_service import RiskService
from janus.domain.common.enums import (
    AccountState,
    DataQualityStatus,
    OrderSide,
    QuoteSource,
)
from janus.domain.common.errors import (
    ConfigurationError,
    JanusError,
    NotFoundError,
)
from janus.domain.common.ids import new_id
from janus.domain.common.money import CASH_SYMBOL
from janus.domain.common.time import MARKET_TZ, market_date
from janus.domain.instruments.eligibility import EligibilityPolicy
from janus.domain.instruments.models import Instrument
from janus.domain.market_data.models import DataSnapshot
from janus.domain.market_rules.models import FeeBreakdown, ResolvedTradingRules
from janus.domain.orders.models import ExecutionFill, OrderIntent, PaperOrder
from janus.domain.portfolio.models import PortfolioAccount, PortfolioSnapshot
from janus.domain.strategy.decision import DecisionRecord, DecisionType
from janus.domain.strategy.models import (
    StrategyDefinition,
    StrategyRegistry,
)
from janus.domain.strategy.proposal import (
    AllocationProposal,
    ProposalViolation,
)
from janus.infrastructure.config.loader import AppConfig, load_app_config
from janus.infrastructure.database.engine import Database
from janus.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWorkFactory
from janus.infrastructure.market_data.fixture_provider import (
    FixtureProvider,
    load_instruments,
    load_strategies,
)
from janus.infrastructure.paper_execution.fill_model import NextOpenWithSlippageV1
from janus.infrastructure.paper_execution.gateway import PaperExecutionGateway
from janus.infrastructure.rulesets.registry import RuleSetRegistry

__all__ = ["ConfiguredJanusContainer", "build_container"]

_DEFAULT_DB_RELATIVE = Path("data") / "local" / "janus.db"
_MARKET_OPEN = time(9, 30)
_SNAPSHOT_STAMP = time(16, 30)
"""Valuation snapshots are stamped after the close so ordering is chronological."""
_DEFAULT_BAR_HISTORY_DAYS = 10


class ConfiguredJanusContainer:
    """Concrete Phase 0 application container."""

    def __init__(
        self,
        *,
        config: AppConfig,
        database: Database,
        uow_factory: UnitOfWorkFactory,
        registry: RuleSetRegistry,
        market_data: FixtureProvider,
        instruments: Mapping[str, Instrument],
        strategies: StrategyRegistry,
        clock: Clock,
        accounting: AccountingService,
        portfolio: PortfolioService,
        risk: RiskService,
        proposals: ProposalService,
        gateway: PaperExecutionGateway,
        fill_model: NextOpenWithSlippageV1,
    ) -> None:
        self._config = config
        self._database = database
        self._uow_factory: UnitOfWorkFactory = uow_factory
        self._registry = registry
        self._engine = registry.engine()
        self._calendar = registry.calendar()
        self._market_data = market_data
        self._instruments = dict(instruments)
        self._strategies = strategies
        self._clock = clock
        self._accounting = accounting
        self._portfolio = portfolio
        self._risk = risk
        self._proposals = proposals
        self._gateway = gateway
        self._fill_model = fill_model
        self._eligibility = EligibilityPolicy(config.universe_policy())

    # ==================================================================
    # Lifecycle / health
    # ==================================================================
    def health(self) -> HealthReport:
        """Probe storage, calendar, rule-sets and data availability."""
        now = self._clock.now()
        components: list[ComponentHealth] = []

        try:
            with self._uow_factory() as uow:
                accounts = uow.accounts.list_all()
            components.append(
                ComponentHealth(name="database", ok=True, detail=f"{len(accounts)} account(s)")
            )
        except Exception as exc:
            components.append(ComponentHealth(name="database", ok=False, detail=type(exc).__name__))

        try:
            rule_sets = self._registry.rule_sets()
            components.append(
                ComponentHealth(
                    name="market_rules",
                    ok=bool(rule_sets),
                    detail=", ".join(f"{rs.rule_set_id}@{rs.version}" for rs in rule_sets),
                )
            )
        except Exception as exc:
            components.append(
                ComponentHealth(name="market_rules", ok=False, detail=type(exc).__name__)
            )

        components.append(
            ComponentHealth(
                name="trading_calendar",
                ok=True,
                detail=(
                    f"coverage {self._calendar.min_date.isoformat()}.."
                    f"{self._calendar.max_date.isoformat()}"
                ),
            )
        )
        components.append(
            ComponentHealth(
                name="market_data",
                ok=bool(self._instruments),
                detail=f"provider=fixture, {len(self._instruments)} instruments",
            )
        )
        components.append(
            ComponentHealth(
                name="risk_policy",
                ok=True,
                detail=f"v{self._config.risk.version}, paper_only",
            )
        )
        components.append(
            ComponentHealth(
                name="llm",
                ok=True,
                detail="disabled (Phase 0 stub agent only)",
            )
        )
        ok = all(component.ok for component in components)
        return HealthReport(
            ok=ok,
            checked_at=now,
            components=tuple(components),
            detail="Phase 0 paper-trading system (no live brokerage)",
        )

    # ==================================================================
    # Account
    # ==================================================================
    def initialize_account(self) -> AccountSummary:
        account = self._accounting.open_account(self._config.account)
        self._ensure_instruments_persisted()
        return self._summary(account)

    def _require_account(self, account_id: str | None = None) -> PortfolioAccount:
        resolved = account_id or self._config.account.id
        with self._uow_factory() as uow:
            account = uow.accounts.get(resolved)
        if account is None:
            raise NotFoundError("account has not been initialised", account_id=resolved)
        return account

    def account_summary(self, *, account_id: str | None = None) -> AccountSummary:
        account = self._require_account(account_id)
        return self._summary(account)

    def _summary(self, account: PortfolioAccount) -> AccountSummary:
        valuation_date = self._signal_date()
        self._ingest_bars_until(valuation_date)
        snapshot = self._store_snapshot(account.account_id, valuation_date)
        positions = tuple(
            PositionSummary(
                symbol=position.symbol,
                quantity=position.quantity,
                available_quantity=position.available_quantity,
                average_cost=f"{position.cost_basis.amount / Decimal(position.quantity):.4f}"
                if position.quantity
                else "0.0000",
                market_value=f"{position.market_value.amount:.2f}",
                weight=f"{snapshot.weight_of(position.symbol):.6f}",
                price_is_stale=position.price_is_stale,
            )
            for position in snapshot.positions
        )
        return AccountSummary(
            account_id=account.account_id,
            mode=account.mode.value,
            state=account.state.value,
            initial_capital=f"{account.initial_capital.amount:.2f}",
            cash=f"{snapshot.cash.amount:.2f}",
            nav=f"{snapshot.nav.amount:.2f}",
            cash_weight=f"{snapshot.cash_weight:.6f}",
            valuation_date=valuation_date.isoformat(),
            as_of=snapshot.as_of.isoformat(),
            positions=positions,
            pause_reason=account.pause_reason,
            data_snapshot_id=snapshot.data_snapshot_id,
            rule_set_id=snapshot.rule_set_id,
        )

    def kill_switch(self, reason: str, *, account_id: str | None = None) -> AccountSummary:
        if not reason or not reason.strip():
            raise ConfigurationError("a kill-switch action requires a recorded reason")
        account = self._require_account(account_id)
        if account.state is AccountState.PAUSED:
            return self._summary(account)
        paused = account.paused(reason.strip(), self._clock.now(), new_version=account.version + 1)
        with self._uow_factory() as uow:
            uow.accounts.save(paused)
            uow.decisions.append(
                DecisionRecord(
                    decision_id=new_id("decision"),
                    decision_type=DecisionType.ADMIN_ACTION,
                    created_at=self._clock.now(),
                    as_of=self._clock.now(),
                    account_id=account.account_id,
                    inputs={"action": "KILL_SWITCH", "reason": reason.strip()},
                    outcome="account paused; positions were NOT liquidated",
                    executable=False,
                )
            )
            uow.commit()
        return self._summary(paused)

    def resume(self, reason: str, *, account_id: str | None = None) -> AccountSummary:
        if not reason or not reason.strip():
            raise ConfigurationError("resuming an account requires a recorded reason")
        account = self._require_account(account_id)
        if account.state is AccountState.ACTIVE:
            return self._summary(account)
        resumed = account.resumed(self._clock.now(), new_version=account.version + 1)
        with self._uow_factory() as uow:
            uow.accounts.save(resumed)
            uow.decisions.append(
                DecisionRecord(
                    decision_id=new_id("decision"),
                    decision_type=DecisionType.ADMIN_ACTION,
                    created_at=self._clock.now(),
                    as_of=self._clock.now(),
                    account_id=account.account_id,
                    inputs={"action": "RESUME", "reason": reason.strip()},
                    outcome="account resumed",
                    executable=False,
                )
            )
            uow.commit()
        return self._summary(resumed)

    def rebuild_from_ledger(self, *, account_id: str | None = None) -> RebuildResult:
        account = self._require_account(account_id)
        with self._uow_factory() as uow:
            stored = uow.portfolio_snapshots.latest_for_account(account.account_id)
        # Compare like with like: rebuild the ledger *as of the stored
        # snapshot's valuation date* so the materialised view and the replayed
        # history are on exactly the same basis.
        valuation_date = stored.valuation_date if stored is not None else self._signal_date()
        rebuilt = self._accounting.rebuild(account.account_id, as_of=valuation_date)
        mismatches: list[str] = []
        stored_cash = rebuilt.cash
        stored_positions: dict[str, int] = {}
        if stored is not None:
            stored_cash = stored.cash
            stored_positions = {p.symbol: p.quantity for p in stored.positions}
            if stored.cash.amount != rebuilt.cash.amount:
                mismatches.append(
                    f"cash: stored {stored.cash.amount} != rebuilt {rebuilt.cash.amount}"
                )
            for symbol, position in rebuilt.positions.items():
                expected = stored_positions.get(symbol, 0)
                if expected != position.quantity:
                    mismatches.append(f"{symbol}: stored {expected} != rebuilt {position.quantity}")
            for symbol in stored_positions:
                if symbol not in rebuilt.positions:
                    mismatches.append(f"{symbol}: stored but not rebuildable")
        rebuilt_positions = {s: p.quantity for s, p in rebuilt.positions.items()}
        if stored is None:
            stored_positions = dict(rebuilt_positions)
        return RebuildResult(
            account_id=account.account_id,
            stored_cash=f"{stored_cash.amount:.2f}",
            rebuilt_cash=f"{rebuilt.cash.amount:.2f}",
            matches=not mismatches,
            cash_entry_count=rebuilt.cash_entry_count,
            position_event_count=rebuilt.position_event_count,
            realized_pnl=f"{rebuilt.realized_pnl.amount:.2f}",
            stored_positions=stored_positions,
            rebuilt_positions=rebuilt_positions,
            mismatches=tuple(mismatches),
        )

    # ==================================================================
    # Market data
    # ==================================================================
    def ingest_fixture_data(self, *, days: int = _DEFAULT_BAR_HISTORY_DAYS) -> dict[str, int]:
        if days < 1:
            raise ConfigurationError("days must be >= 1")
        end = self._signal_date()
        dates = self._trading_days_ending(end, days)
        bars: list[Any] = []
        for symbol in sorted(self._instruments):
            for trade_date in dates:
                bar = self._market_data.get_daily_bar(symbol, trade_date)
                if bar is not None:
                    bars.append(bar)
        with self._uow_factory() as uow:
            written = uow.market_bars.upsert_many(bars)
            uow.commit()
        snapshot = self._ensure_snapshot(end)
        return {
            "instruments": len(self._instruments),
            "bars_written": written,
            "trading_days": len(dates),
            "data_snapshot_id": 1 if snapshot else 0,
        }

    def _ensure_snapshot(self, as_of_date: date) -> DataSnapshot:
        symbols = sorted(self._instruments)
        bars = self._bars_on(symbols, as_of_date)
        digest = hashlib.sha256(
            "|".join(f"{b.symbol}:{b.trade_date.isoformat()}:{b.close}" for b in bars).encode()
        ).hexdigest()
        snapshot_id = f"dsnap-{as_of_date.isoformat()}-{digest[:12]}"
        with self._uow_factory() as uow:
            existing = uow.data_snapshots.get(snapshot_id)
            if existing is not None:
                return existing
            rule_set = self._engine.rule_set_for(
                datetime.combine(as_of_date, _MARKET_OPEN, tzinfo=MARKET_TZ)
            )
            snapshot = DataSnapshot(
                snapshot_id=snapshot_id,
                created_at=self._clock.now(),
                as_of=datetime.combine(as_of_date, _MARKET_OPEN, tzinfo=MARKET_TZ),
                provider=QuoteSource.FIXTURE,
                symbols=tuple(b.symbol for b in bars),
                bar_dates=(as_of_date,) if bars else (),
                bar_count=len(bars),
                content_hash=digest,
                quality=DataQualityStatus.OK,
                rule_set_id=f"{rule_set.rule_set_id}@{rule_set.version}",
                notes="deterministic fixture market-data snapshot",
            )
            uow.data_snapshots.save(snapshot)
            uow.commit()
        return snapshot

    def _bars_on(self, symbols: Sequence[str], trade_date: date) -> list[Any]:
        bars = []
        for symbol in symbols:
            bar = self._market_data.get_daily_bar(symbol, trade_date)
            if bar is not None:
                bars.append(bar)
        return bars

    def _ingest_bars_until(self, valuation_date: date) -> None:
        dates = self._trading_days_ending(valuation_date, _DEFAULT_BAR_HISTORY_DAYS)
        bars = []
        for symbol in sorted(self._instruments):
            for trade_date in dates:
                bar = self._market_data.get_daily_bar(symbol, trade_date)
                if bar is not None:
                    bars.append(bar)
        with self._uow_factory() as uow:
            uow.market_bars.upsert_many(bars)
            uow.commit()

    def _trading_days_ending(self, end: date, count: int) -> list[date]:
        days: list[date] = []
        cursor = end
        for _ in range(max(count, 1)):
            if not self._calendar.covers(cursor):
                break
            if self._calendar.is_trading_day(cursor):
                days.append(cursor)
            if cursor <= self._calendar.min_date:
                break
            cursor = cursor - __import__("datetime").timedelta(days=1)
        return sorted(days)

    def _store_snapshot(self, account_id: str, valuation_date: date) -> PortfolioSnapshot:
        self._ingest_bars_until(valuation_date)
        data_snapshot_id = self._ensure_snapshot(valuation_date).snapshot_id
        snapshot = self._portfolio.snapshot(
            account_id=account_id,
            valuation_date=valuation_date,
            data_snapshot_id=data_snapshot_id,
            rule_set_id=self._current_rule_set_id(valuation_date),
            # Stamp the snapshot after the market close on its valuation date so
            # that "latest" ordering is chronological even with a fixed clock.
            as_of=datetime.combine(valuation_date, _SNAPSHOT_STAMP, tzinfo=MARKET_TZ),
        )
        with self._uow_factory() as uow:
            uow.portfolio_snapshots.save(snapshot)
            uow.commit()
        return snapshot

    def _current_rule_set_id(self, trade_date: date) -> str:
        rule_set = self._engine.rule_set_for(
            datetime.combine(trade_date, _MARKET_OPEN, tzinfo=MARKET_TZ)
        )
        return f"{rule_set.rule_set_id}@{rule_set.version}"

    def _signal_date(self) -> date:
        today = self._clock.today()
        if self._calendar.covers(today):
            if self._calendar.is_trading_day(today):
                return today
            return self._calendar.previous_trading_day(today)
        # Fail closed: never guess a trading day for a clock outside the loaded
        # calendar window. A stale calendar is a configuration error, not a
        # licence to assume "probably a normal weekday".
        raise ConfigurationError(
            "the market calendar does not cover the current date",
            today=today.isoformat(),
            coverage_from=self._calendar.min_date.isoformat(),
            coverage_to=self._calendar.max_date.isoformat(),
        )

    def _ensure_instruments_persisted(self) -> None:
        with self._uow_factory() as uow:
            uow.instruments.upsert_many(list(self._instruments.values()))
            uow.commit()

    # ==================================================================
    # Proposals
    # ==================================================================
    def submit_proposal(
        self,
        proposal: AllocationProposal | Mapping[str, Any],
        *,
        account_id: str | None = None,
    ) -> ProposalOutcome:
        account = self._require_account(account_id)
        if isinstance(proposal, Mapping):
            # Untrusted, possibly model-authored payload: a structural or
            # boundary violation is a rejection, never an exception.
            try:
                proposal = parse_proposal_payload(proposal)
            except JanusError as exc:
                violations = [
                    ProposalViolation.MALFORMED.value,
                    ProposalViolation.PROHIBITED_INSTRUCTION.value,
                ]
                return ProposalOutcome(
                    accepted=False,
                    violations=tuple(dict.fromkeys(violations)),
                    messages=(type(exc).__name__,),
                )

        evaluation_time = self._clock.now()
        as_of_date = market_date(proposal.as_of)
        if not self._calendar.covers(as_of_date):
            return ProposalOutcome(
                accepted=False,
                violations=(ProposalViolation.STALE_DATA.value,),
                messages=(
                    f"proposal as_of {as_of_date.isoformat()} is outside the loaded "
                    "trading-calendar coverage",
                ),
                proposal_id=proposal.proposal_id,
            )

        snapshot = self._ensure_snapshot(as_of_date)
        self._ingest_bars_until(as_of_date)
        if proposal.data_snapshot_id is None:
            # A proposal that does not name a snapshot is bound to the current
            # data snapshot; a proposal naming an unknown snapshot is still
            # rejected by the service.
            proposal = proposal.model_copy(update={"data_snapshot_id": snapshot.snapshot_id})

        eligible = self._eligible_symbols(as_of_date)
        known: dict[str, Instrument] = dict(self._instruments)
        try:
            validation, record = self._proposals.submit(
                proposal,
                account_id=account.account_id,
                known_symbols=known,
                eligible_symbols=eligible,
                evaluation_time=evaluation_time,
                rule_set_id=self._current_rule_set_id(as_of_date),
            )
        except JanusError as exc:
            return ProposalOutcome(
                accepted=False,
                violations=(ProposalViolation.MALFORMED.value,),
                messages=(str(exc),),
                proposal_id=proposal.proposal_id,
            )
        return ProposalOutcome(
            accepted=validation.valid,
            violations=tuple(validation.reason_codes),
            messages=tuple(validation.messages),
            decision_id=record.decision_id,
            proposal_id=proposal.proposal_id,
            target_weights={
                weight.symbol: f"{weight.weight:.6f}" for weight in proposal.target_weights
            },
        )

    def _eligible_symbols(self, as_of: date) -> tuple[str, ...]:
        eligible: list[str] = []
        for symbol, instrument in self._instruments.items():
            listed: int | None = None
            if instrument.listing_date is None or instrument.listing_date < self._calendar.min_date:
                listed = self._calendar.count_trading_days(self._calendar.min_date, as_of)
            else:
                listed = self._calendar.count_trading_days(instrument.listing_date, as_of)
            result = self._eligibility.evaluate(instrument, as_of, listed_trading_days=listed)
            if result.eligible:
                eligible.append(symbol)
        return tuple(eligible)

    # ==================================================================
    # Daily cycle
    # ==================================================================
    def run_daily_cycle(
        self,
        *,
        trade_date: date | None = None,
        account_id: str | None = None,
    ) -> CycleResult:
        account = self._require_account(account_id)
        signal_date = trade_date or self._signal_date()
        self._calendar.ensure_trading_day(signal_date)
        execution_date = self._calendar.next_trading_day(signal_date)
        notes: list[str] = []

        self._ingest_bars_until(signal_date)
        data_snapshot = self._ensure_snapshot(signal_date)
        opening = self._store_snapshot(account.account_id, signal_date)

        if account.state is not AccountState.ACTIVE:
            return CycleResult(
                trade_date=signal_date.isoformat(),
                orders_proposed=0,
                orders_rejected=0,
                orders_filled=0,
                fills=0,
                cash_after=f"{opening.cash.amount:.2f}",
                nav_after=f"{opening.nav.amount:.2f}",
                account_state=account.state.value,
                notes=("account is paused; no new orders were created",),
            )

        accepted = self._latest_accepted_proposal(account.account_id)
        if accepted is None:
            return CycleResult(
                trade_date=signal_date.isoformat(),
                orders_proposed=0,
                orders_rejected=0,
                orders_filled=0,
                fills=0,
                cash_after=f"{opening.cash.amount:.2f}",
                nav_after=f"{opening.nav.amount:.2f}",
                account_state=account.state.value,
                notes=("no accepted allocation proposal; nothing to rebalance",),
            )
        proposal, proposal_decision_id = accepted

        target = proposal.to_target_allocation()
        rules_by_symbol: dict[str, Any] = {}
        for symbol in target.as_dict():
            if symbol == CASH_SYMBOL:
                continue
            instrument = self._instruments.get(symbol)
            if instrument is None:
                notes.append(f"{symbol}: not in the instrument master; skipped")
                continue
            try:
                rules_by_symbol[symbol] = self._engine.resolve(instrument, signal_date)
            except JanusError as exc:
                notes.append(f"{symbol}: rules unavailable ({exc.message}); skipped")

        rebalance_decision_id = new_id("decision")
        plan = self._portfolio.plan_rebalance(
            account_id=account.account_id,
            target=target,
            snapshot=opening,
            instruments={s: self._instruments[s] for s in rules_by_symbol},
            rules_by_symbol=rules_by_symbol,
            signal_bar_date=signal_date,
            decision_id=rebalance_decision_id,
            data_snapshot_id=data_snapshot.snapshot_id,
            strategy_id=proposal.strategy_id,
            strategy_version=proposal.strategy_version,
            rationale=proposal.thesis,
        )
        notes.extend(plan.notes)

        with self._uow_factory() as uow:
            uow.decisions.append(
                DecisionRecord(
                    decision_id=rebalance_decision_id,
                    decision_type=DecisionType.REBALANCE_PLAN,
                    created_at=self._clock.now(),
                    as_of=proposal.as_of,
                    account_id=account.account_id,
                    proposal_id=proposal.proposal_id,
                    strategy_id=proposal.strategy_id,
                    strategy_version=proposal.strategy_version,
                    data_snapshot_id=data_snapshot.snapshot_id,
                    rule_set_id=self._current_rule_set_id(signal_date),
                    inputs={
                        "signal_date": signal_date.isoformat(),
                        "execution_date": execution_date.isoformat(),
                        "intents": [
                            {
                                "intent_id": intent.intent_id,
                                "symbol": intent.symbol,
                                "side": intent.side.value,
                                "quantity": intent.quantity,
                                "limit_price": str(intent.limit_price),
                            }
                            for intent in plan.intents
                        ],
                    },
                    thesis=proposal.thesis,
                    evidence_refs=proposal.evidence_refs,
                    uncertainties=proposal.uncertainties,
                    outcome="rebalance plan created",
                    executable=True,
                )
            )
            uow.commit()

        approved = 0
        rejected = 0
        filled = 0
        fill_count = 0

        for intent in plan.intents:
            decision, order = self._risk.evaluate(intent)
            if decision.approved:
                approved += 1
                filled_order, fill_recorded = self._execute(intent, order, rules_by_symbol)
                if fill_recorded is not None:
                    fill_count += 1
                if filled_order.status.value in {"FILLED", "PARTIALLY_FILLED"}:
                    filled += 1
            else:
                rejected += 1
                failed = ", ".join(rule.value for rule in decision.failed_rule_ids)
                notes.append(f"{intent.symbol}: risk rejected ({failed})")

        closing = self._store_snapshot(account.account_id, execution_date)
        return CycleResult(
            trade_date=signal_date.isoformat(),
            orders_proposed=len(plan.intents),
            orders_rejected=rejected,
            orders_filled=filled,
            fills=fill_count,
            cash_after=f"{closing.cash.amount:.2f}",
            nav_after=f"{closing.nav.amount:.2f}",
            account_state=account.state.value,
            notes=(*tuple(notes), f"proposal {proposal_decision_id}"),
        )

    # ==================================================================
    # Execution helpers
    # ==================================================================
    def _latest_accepted_proposal(self, account_id: str) -> tuple[AllocationProposal, str] | None:
        """Most recent proposal whose validation passed, with its decision id."""
        with self._uow_factory() as uow:
            records = uow.decisions.list_recent(200)
        for record in reversed(records):
            if record.decision_type is not DecisionType.ALLOCATION_PROPOSAL:
                continue
            validation = record.inputs.get("validation")
            if not isinstance(validation, dict) or not validation.get("valid"):
                continue
            payload = record.inputs.get("proposal")
            if not isinstance(payload, dict):
                continue
            try:
                proposal = AllocationProposal.model_validate(payload)
            except Exception:
                continue
            return proposal, record.decision_id
        return None

    def _execute(
        self,
        intent: OrderIntent,
        order: PaperOrder,
        rules_by_symbol: Mapping[str, ResolvedTradingRules],
    ) -> tuple[PaperOrder, ExecutionFill | None]:
        """Submit an approved order and try to fill it on the next session."""
        from janus.domain.common.enums import OrderStatus

        if order.status is not OrderStatus.RISK_APPROVED:
            return order, None
        rules = rules_by_symbol.get(intent.symbol)
        instrument = self._instruments.get(intent.symbol)
        if rules is None or instrument is None:
            return order, None

        now = self._clock.now()
        submitted = self._gateway.submit(order, at=now)
        execution_date: date = intent.earliest_execution_date
        bar = self._market_data.get_daily_bar(intent.symbol, execution_date)
        signal_bar = (
            self._market_data.get_daily_bar(intent.symbol, intent.signal_bar_date)
            if intent.signal_bar_date is not None
            else None
        )
        prev_close = signal_bar.close if signal_bar is not None else None

        def fee_calculator(side: OrderSide, quantity: int, price: Decimal) -> FeeBreakdown:
            return self._engine.compute_fees(rules=rules, side=side, quantity=quantity, price=price)

        proposal = self._fill_model.propose(
            order=submitted,
            bar=bar,
            prev_close=prev_close,
            rules=rules,
            fee_calculator=fee_calculator,
            at=now,
            is_risk_warning=instrument.status.is_risk_warning,
            signal_bar_date=intent.signal_bar_date,
            data_snapshot_id=submitted.data_snapshot_id,
        )
        if not proposal.filled:
            with self._uow_factory() as uow:
                uow.orders.save(submitted)
                uow.commit()
            return submitted, None

        fill = proposal.require_fill()
        updated = self._gateway.apply_fill(submitted, proposal, at=now)
        with self._uow_factory() as uow:
            self._accounting.record_fill(
                fill=fill,
                order=updated,
                rules=rules,
                uow=uow,
            )
            uow.commit()
        return updated, fill

    # ==================================================================
    # Read models
    # ==================================================================
    def list_orders(
        self, *, account_id: str | None = None, limit: int = 50
    ) -> tuple[OrderSummary, ...]:
        account = self._require_account(account_id)
        with self._uow_factory() as uow:
            orders = uow.orders.list_by_account(account.account_id)
        selected = orders[-limit:] if limit > 0 else orders
        return tuple(
            OrderSummary(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side.value,
                quantity=order.quantity,
                filled_quantity=order.filled_quantity,
                status=order.status.value,
                target_trade_date=order.target_trade_date.isoformat(),
                created_at=order.created_at.isoformat(),
                limit_price=None if order.limit_price is None else str(order.limit_price),
                rejection_reason=order.rejection_reason,
                rule_set_id=order.rule_set_id,
                rule_set_version=order.rule_set_version,
                fill_model_version=order.fill_model_version,
                decision_id=order.decision_id,
                data_snapshot_id=order.data_snapshot_id,
            )
            for order in selected
        )

    def list_audit(self, *, limit: int = 50) -> tuple[AuditEntry, ...]:
        with self._uow_factory() as uow:
            records = uow.decisions.list_recent(limit)
        return tuple(
            AuditEntry(
                decision_id=record.decision_id,
                decision_type=record.decision_type.value,
                created_at=record.created_at.isoformat(),
                outcome=record.outcome,
                executable=record.executable,
                strategy_id=record.strategy_id,
                strategy_version=record.strategy_version,
                proposal_id=record.proposal_id,
                data_snapshot_id=record.data_snapshot_id,
                rule_set_id=record.rule_set_id,
                model_id=record.model_id,
            )
            for record in records
        )


def _load_strategy_registry(path: Path) -> StrategyRegistry:
    raw = load_strategies(path)
    registry = StrategyRegistry()
    for entry in raw:
        try:
            definition = StrategyDefinition.model_validate(dict(entry))
        except Exception as exc:
            raise ConfigurationError(
                "invalid strategy definition in the Phase 0 registry",
                file=str(path),
                error=str(exc),
            ) from exc
        registry.register(definition)
    return registry


def build_container(
    *,
    config_dir: Path | str | None = None,
    database_url: str | None = None,
    clock: Clock | None = None,
    apply_migrations: bool = True,
) -> JanusContainer:
    """Build a fully wired Phase 0 container.

    ``database_url`` defaults to a local SQLite file under ``data/local``. Pass
    ``sqlite+pysqlite:///:memory:`` (with ``apply_migrations=False``) for tests.
    """
    config = load_app_config(config_dir)

    if database_url is None:
        default_path = config.repo_root / _DEFAULT_DB_RELATIVE
        database_url = f"sqlite:///{default_path.as_posix()}"

    database = Database(database_url)
    if apply_migrations:
        database.upgrade_to_head()
    else:
        database.create_all()

    # The SQLAlchemy repositories implement the repository/UnitOfWork ports; the
    # cast records that contract in one place. `tests/integration` proves the
    # structural conformance at runtime.
    concrete_uow_factory = SqlAlchemyUnitOfWorkFactory(database)
    uow_factory = cast(UnitOfWorkFactory, concrete_uow_factory)
    resolved_clock: Clock = clock or SystemClock()
    registry = RuleSetRegistry.from_default_config(config_dir=config.config_dir / "market_rules")
    calendar = registry.calendar()

    fixture_dir = config.fixture_dir()
    instruments = load_instruments(fixture_dir / "instruments.json")
    strategy_registry = _load_strategy_registry(fixture_dir / "strategies.json")
    market_data = FixtureProvider(instruments)

    accounting = AccountingService(uow_factory=uow_factory, clock=resolved_clock, calendar=calendar)
    portfolio = PortfolioService(
        accounting=accounting,
        market_data=market_data,
        calendar=calendar,
        clock=resolved_clock,
    )
    risk = RiskService(
        uow_factory=uow_factory,
        clock=resolved_clock,
        policy=config.risk,
        calendar=calendar,
        engine=registry.engine(),
        universe_policy=config.universe_policy(),
    )
    proposals = ProposalService(
        uow_factory=uow_factory,
        registry=strategy_registry,
        clock=resolved_clock,
    )

    container = ConfiguredJanusContainer(
        config=config,
        database=database,
        uow_factory=uow_factory,
        registry=registry,
        market_data=market_data,
        instruments={instrument.symbol: instrument for instrument in instruments},
        strategies=strategy_registry,
        clock=resolved_clock,
        accounting=accounting,
        portfolio=portfolio,
        risk=risk,
        proposals=proposals,
        gateway=PaperExecutionGateway(),
        fill_model=NextOpenWithSlippageV1(),
    )
    with uow_factory() as uow:
        uow.instruments.upsert_many(list(container._instruments.values()))
        uow.commit()
    return container
