"""Contract tests for the JANUS CLI transport.

The CLI is exercised entirely through its injected ``JanusContainer`` fake, so
these tests are offline and independent of the persistence/container
implementation. They pin the public behaviour of ``main``:

* exit codes ``0`` success, ``2`` invalid usage, ``3`` rejection/validation,
  ``4`` internal;
* JSON vs human-readable output, stdout vs stderr separation;
* delegation to the container with the documented arguments;
* no SQLAlchemy (or any provider SDK) import from the transport layer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from janus.application.container import (
    AccountSummary,
    AuditEntry,
    ComponentHealth,
    CycleResult,
    HealthReport,
    OrderSummary,
    PositionSummary,
    ProposalOutcome,
    RebuildResult,
)
from janus_cli.main import EXIT_INTERNAL, EXIT_OK, EXIT_REJECTED, EXIT_USAGE, main

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Hand-written JanusContainer fake (no dependency on the real composition root)
# ---------------------------------------------------------------------------
class FakeContainer:
    """Deterministic in-memory container used by every CLI contract test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.accept_proposals = True
        self.rebuild_matches = True

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def health(self) -> HealthReport:
        self._record("health")
        return HealthReport(
            ok=True,
            checked_at=datetime(2026, 11, 5, 7, 0, tzinfo=UTC),
            components=(ComponentHealth(name="database", ok=True, detail="sqlite"),),
            detail="phase0 paper environment",
        )

    def initialize_account(self) -> AccountSummary:
        self._record("initialize_account")
        return _account_summary()

    def account_summary(self, *, account_id: str | None = None) -> AccountSummary:
        self._record("account_summary", account_id=account_id)
        return _account_summary()

    def ingest_fixture_data(self, *, days: int = 20) -> dict[str, int]:
        self._record("ingest_fixture_data", days=days)
        return {"bars": days * 2, "instruments": 2}

    def submit_proposal(
        self,
        proposal: Any,
        *,
        account_id: str | None = None,
    ) -> ProposalOutcome:
        self._record("submit_proposal", proposal=proposal, account_id=account_id)
        if self.accept_proposals:
            return ProposalOutcome(
                accepted=True,
                proposal_id="proposal-test",
                decision_id="decision-test",
                target_weights={"510300.SH": "0.20", "CASH.CNY": "0.80"},
            )
        return ProposalOutcome(
            accepted=False,
            violations=("UNKNOWN_SYMBOL",),
            messages=("unknown symbol 999999.SH",),
        )

    def run_daily_cycle(
        self,
        *,
        trade_date: date | None = None,
        account_id: str | None = None,
    ) -> CycleResult:
        self._record("run_daily_cycle", trade_date=trade_date, account_id=account_id)
        return CycleResult(
            trade_date=(trade_date or date(2026, 11, 5)).isoformat(),
            orders_proposed=2,
            orders_rejected=0,
            orders_filled=2,
            fills=2,
            cash_after="99500.00",
            nav_after="100000.00",
            account_state="ACTIVE",
            notes=("paper only",),
        )

    def kill_switch(self, reason: str, *, account_id: str | None = None) -> AccountSummary:
        self._record("kill_switch", reason=reason, account_id=account_id)
        return _account_summary(state="PAUSED", pause_reason=reason)

    def resume(self, reason: str, *, account_id: str | None = None) -> AccountSummary:
        self._record("resume", reason=reason, account_id=account_id)
        return _account_summary(state="ACTIVE")

    def rebuild_from_ledger(self, *, account_id: str | None = None) -> RebuildResult:
        self._record("rebuild_from_ledger", account_id=account_id)
        mismatches: tuple[str, ...] = () if self.rebuild_matches else ("cash mismatch",)
        return RebuildResult(
            account_id="janus-paper-cny-001",
            stored_cash="100000.00",
            rebuilt_cash="100000.00",
            matches=self.rebuild_matches,
            cash_entry_count=1,
            position_event_count=0,
            realized_pnl="0.00",
            mismatches=mismatches,
        )

    def list_orders(
        self, *, account_id: str | None = None, limit: int = 50
    ) -> tuple[OrderSummary, ...]:
        self._record("list_orders", account_id=account_id, limit=limit)
        return (
            OrderSummary(
                order_id="order-1",
                symbol="510300.SH",
                side="BUY",
                quantity=100,
                filled_quantity=100,
                status="FILLED",
                target_trade_date="2026-11-05",
                created_at="2026-11-05T01:30:00+00:00",
                limit_price="4.000",
            ),
        )

    def list_audit(self, *, limit: int = 50) -> tuple[AuditEntry, ...]:
        self._record("list_audit", limit=limit)
        return (
            AuditEntry(
                decision_id="decision-1",
                decision_type="PROPOSAL",
                created_at="2026-11-05T01:30:00+00:00",
                outcome="ACCEPTED",
                executable=False,
            ),
        )


def _account_summary(*, state: str = "ACTIVE", pause_reason: str | None = None) -> AccountSummary:
    return AccountSummary(
        account_id="janus-paper-cny-001",
        mode="paper",
        state=state,
        initial_capital="100000.00",
        cash="100000.00",
        nav="100000.00",
        cash_weight="1.0000",
        valuation_date="2026-11-05",
        as_of="2026-11-05T07:00:00+00:00",
        positions=(
            PositionSummary(
                symbol="510300.SH",
                quantity=100,
                available_quantity=0,
                average_cost="4.0000",
                market_value="400.00",
                weight="0.0040",
                price_is_stale=False,
            ),
        ),
        pause_reason=pause_reason,
    )


def _last_call(fake: FakeContainer) -> tuple[str, dict[str, Any]]:
    assert fake.calls, "expected the CLI to call the container"
    return fake.calls[-1]


# ---------------------------------------------------------------------------
# Global behaviour
# ---------------------------------------------------------------------------
def test_help_exits_zero_without_building_a_container(capsys: Any) -> None:
    assert main(["--help"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "usage" in captured.out.lower()
    assert "paper" in captured.out.lower()


def test_missing_command_exits_usage(capsys: Any) -> None:
    assert main([], container=FakeContainer()) == EXIT_USAGE
    assert "usage" in capsys.readouterr().err.lower()


def test_unknown_command_exits_usage(capsys: Any) -> None:
    assert main(["frobnicate"], container=FakeContainer()) == EXIT_USAGE
    assert capsys.readouterr().err


def test_json_flag_before_subcommand(capsys: Any, tmp_path: Path) -> None:
    fake = FakeContainer()
    code = main(
        ["--json", "--config-dir", str(tmp_path), "--db-url", "sqlite:///x.db", "health"],
        container=fake,
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_json_flag_after_subcommand(capsys: Any) -> None:
    assert main(["health", "--json"], container=FakeContainer()) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["components"][0]["name"] == "database"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
def test_health_human_readable(capsys: Any) -> None:
    fake = FakeContainer()
    assert main(["health"], container=fake) == EXIT_OK
    captured = capsys.readouterr()
    assert "JANUS health: OK" in captured.out
    assert "database" in captured.out
    assert captured.err == ""
    assert _last_call(fake)[0] == "health"


def test_init_account_exits_zero(capsys: Any) -> None:
    fake = FakeContainer()
    assert main(["init-account", "--json"], container=fake) == EXIT_OK
    account = json.loads(capsys.readouterr().out)
    assert account["account_id"] == "janus-paper-cny-001"
    assert account["mode"] == "paper"
    assert isinstance(account["cash"], str)
    assert "initialize_account" in fake.call_names()


def test_status_prints_account(capsys: Any) -> None:
    assert main(["status"], container=FakeContainer()) == EXIT_OK
    out = capsys.readouterr().out
    assert "janus-paper-cny-001" in out
    assert "cash: 100000.00" in out


def test_ingest_fixtures_passes_days(capsys: Any) -> None:
    fake = FakeContainer()
    assert main(["ingest-fixtures", "--days", "5", "--json"], container=fake) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"bars": 10, "instruments": 2}
    assert fake.calls == [("ingest_fixture_data", {"days": 5})]


def test_run_cycle_default_date(capsys: Any) -> None:
    fake = FakeContainer()
    assert main(["run-cycle", "--json"], container=fake) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["orders_filled"] == 2
    assert isinstance(payload["cash_after"], str)
    name, kwargs = _last_call(fake)
    assert name == "run_daily_cycle"
    assert kwargs["trade_date"] is None


def test_run_cycle_explicit_date(capsys: Any) -> None:
    fake = FakeContainer()
    assert main(["run-cycle", "--date", "2026-11-05"], container=fake) == EXIT_OK
    _name, kwargs = _last_call(fake)
    assert kwargs["trade_date"] == date(2026, 11, 5)


def test_kill_switch_and_resume(capsys: Any) -> None:
    fake = FakeContainer()
    assert main(["kill-switch", "--reason", "data integrity failure"], container=fake) == 0
    assert "PAUSED" in capsys.readouterr().out
    name, kwargs = _last_call(fake)
    assert name == "kill_switch"
    assert kwargs == {"reason": "data integrity failure", "account_id": None}
    assert main(["resume", "--reason", "administrator reviewed"], container=fake) == 0
    assert "ACTIVE" in capsys.readouterr().out


def test_orders_and_audit_limits(capsys: Any) -> None:
    fake = FakeContainer()
    assert main(["orders", "--limit", "5", "--json"], container=fake) == EXIT_OK
    orders = json.loads(capsys.readouterr().out)["orders"]
    assert orders[0]["order_id"] == "order-1"
    assert _last_call(fake) == ("list_orders", {"account_id": None, "limit": 5})

    assert main(["audit", "--limit", "3"], container=fake) == EXIT_OK
    assert "decision-1" in capsys.readouterr().out
    assert _last_call(fake) == ("list_audit", {"limit": 3})


def test_rebuild_success(capsys: Any) -> None:
    fake = FakeContainer()
    assert main(["rebuild", "--json"], container=fake) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["matches"] is True
    assert payload["stored_cash"] == "100000.00"


def test_rebuild_mismatch_exits_rejected(capsys: Any) -> None:
    fake = FakeContainer()
    fake.rebuild_matches = False
    assert main(["rebuild"], container=fake) == EXIT_REJECTED
    captured = capsys.readouterr()
    assert "rejected" in captured.err.lower()


# ---------------------------------------------------------------------------
# Usage errors (exit 2)
# ---------------------------------------------------------------------------
def test_invalid_date_exits_usage(capsys: Any) -> None:
    fake = FakeContainer()
    assert main(["run-cycle", "--date", "2026-13-45"], container=fake) == EXIT_USAGE
    assert "YYYY-MM-DD" in capsys.readouterr().err
    assert fake.calls == []


def test_invalid_limit_exits_usage(capsys: Any) -> None:
    assert main(["orders", "--limit", "0"], container=FakeContainer()) == EXIT_USAGE
    assert capsys.readouterr().err
    assert main(["audit", "--limit", "abc"], container=FakeContainer()) == EXIT_USAGE


def test_missing_required_options_exit_usage(capsys: Any) -> None:
    assert main(["propose"], container=FakeContainer()) == EXIT_USAGE
    assert main(["kill-switch"], container=FakeContainer()) == EXIT_USAGE
    assert main(["kill-switch", "--reason", "   "], container=FakeContainer()) == EXIT_USAGE
    assert main(["resume"], container=FakeContainer()) == EXIT_USAGE
    assert capsys.readouterr().err


# ---------------------------------------------------------------------------
# Proposal input / rejection (exit 3)
# ---------------------------------------------------------------------------
def test_valid_proposal_file_exits_zero(capsys: Any, tmp_path: Path) -> None:
    proposal = _proposal_payload()
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(proposal), encoding="utf-8")
    fake = FakeContainer()
    assert main(["propose", "--file", str(path)], container=fake) == EXIT_OK
    out = capsys.readouterr().out
    assert "accepted: True" in out
    name, kwargs = _last_call(fake)
    assert name == "submit_proposal"
    assert kwargs["proposal"]["strategy_id"] == "broad_etf_core"


def test_malformed_proposal_json_exits_rejected(capsys: Any, tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")
    fake = FakeContainer()
    assert main(["propose", "--file", str(path)], container=fake) == EXIT_REJECTED
    assert "error:" in capsys.readouterr().err
    assert fake.calls == []


def test_proposal_file_must_be_a_json_object(capsys: Any, tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert main(["propose", "--file", str(path)], container=FakeContainer()) == EXIT_REJECTED
    assert "JSON object" in capsys.readouterr().err


def test_missing_proposal_file_exits_rejected(capsys: Any, tmp_path: Path) -> None:
    assert (
        main(["propose", "--file", str(tmp_path / "nope.json")], container=FakeContainer())
        == EXIT_REJECTED
    )
    assert "cannot load proposal file" in capsys.readouterr().err


def test_rejected_proposal_exits_rejected(capsys: Any, tmp_path: Path) -> None:
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(_proposal_payload()), encoding="utf-8")
    fake = FakeContainer()
    fake.accept_proposals = False
    assert main(["propose", "--file", str(path), "--json"], container=fake) == EXIT_REJECTED
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["accepted"] is False
    assert payload["violations"] == ["UNKNOWN_SYMBOL"]
    assert "UNKNOWN_SYMBOL" in captured.err


def test_input_validation_error_maps_to_exit_three(capsys: Any, tmp_path: Path) -> None:
    """A container schema-validation failure is a rejection, not an internal error."""
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps({"strategy_id": "broad_etf_core"}), encoding="utf-8")

    class FakeValidationError(ValueError):
        """Stand-in for pydantic.ValidationError (also a ValueError)."""

    class ValidatingContainer(FakeContainer):
        def submit_proposal(
            self, proposal: Any, *, account_id: str | None = None
        ) -> ProposalOutcome:
            raise FakeValidationError("as_of: field required")

    assert main(["propose", "--file", str(path)], container=ValidatingContainer()) == EXIT_REJECTED
    assert "field required" in capsys.readouterr().err


def _proposal_payload() -> dict[str, Any]:
    return {
        "as_of": "2026-11-05T15:30:00+08:00",
        "strategy_id": "broad_etf_core",
        "strategy_version": "0.1.0",
        "regime": "RISK_OFF",
        "target_weights": [
            {"symbol": "510300.SH", "weight": "0.20"},
            {"symbol": "CASH.CNY", "weight": "0.80"},
        ],
        "horizon_days": 20,
        "thesis": "Fixture proposal for contract tests only.",
        "data_snapshot_id": "snapshot-2026-11-05",
    }


# ---------------------------------------------------------------------------
# Internal errors (exit 4)
# ---------------------------------------------------------------------------
class FakeDomainError(Exception):
    """Stand-in for janus.domain.common.errors.JanusError (duck-typed code)."""

    code = "RISK_REJECTED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "error_type": type(self).__name__,
            "message": str(self),
            "details": {},
        }


def test_domain_error_maps_to_rejected_exit(capsys: Any) -> None:
    class RaisingContainer(FakeContainer):
        def health(self) -> HealthReport:
            raise FakeDomainError("risk limits breached")

    assert main(["health"], container=RaisingContainer()) == EXIT_REJECTED
    err = capsys.readouterr().err
    assert "RISK_REJECTED" in err
    assert "Traceback" not in err


def test_unexpected_error_maps_to_internal_exit(capsys: Any) -> None:
    class BrokenContainer(FakeContainer):
        def health(self) -> HealthReport:
            raise RuntimeError("unexpected boom")

    assert main(["health"], container=BrokenContainer()) == EXIT_INTERNAL
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Layering guard
# ---------------------------------------------------------------------------
def test_cli_import_does_not_pull_in_sqlalchemy() -> None:
    """The transport layer must not import the persistence stack."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    prefix = str(ROOT / "apps" / "cli")
    env["PYTHONPATH"] = prefix if not existing else prefix + os.pathsep + existing
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, janus_cli.main; print('sqlalchemy' in sys.modules)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "False"
