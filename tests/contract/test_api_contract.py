"""Contract tests for the JANUS HTTP API transport (no auth, localhost only).

Every test injects a hand-written ``JanusContainer`` fake into
``create_app(container=...)``, so the suite is offline and independent of the
real persistence/container implementation.

Aside from wiring, these tests pin three non-negotiable Phase 0 guarantees:

* money/weights are decimal **strings**, never JSON floats;
* adversarial/unknown request fields (``mode: "live"``, ``add_capital``,
  ``risk_policy_override``, credentials) are rejected with a 4xx and no
  container call -- they can never create an order;
* controlled rejections map to 4xx, unexpected failures to a bare 500, and
  ``/health`` never leaks secrets.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
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
from janus_api.app import create_app

SENTINEL_SECRET = "sk-live-do-not-leak-0123456789"
CREDENTIAL_NAMES = frozenset({"api_key", "token", "secret", "authorization", "password"})


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeJanusError(Exception):
    """Duck-typed stand-in for ``janus.domain.common.errors.JanusError``."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "error_type": type(self).__name__,
            "message": self.message,
            "details": {},
        }


class FakeContainer:
    """Deterministic in-memory container with call recording and error injection."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, BaseException] = {}
        self.accept_proposals = True
        self.rebuild_matches = True

    def _call(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        error = self.errors.get(name)
        if error is not None:
            raise error

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def _account(self, *, state: str = "ACTIVE", pause_reason: str | None = None) -> AccountSummary:
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

    def health(self) -> HealthReport:
        self._call("health")
        return HealthReport(
            ok=True,
            checked_at=datetime(2026, 11, 5, 7, 0, tzinfo=UTC),
            components=(ComponentHealth(name="database", ok=True, detail="sqlite"),),
            detail="phase0 paper environment",
        )

    def initialize_account(self) -> AccountSummary:
        self._call("initialize_account")
        return self._account()

    def account_summary(self, *, account_id: str | None = None) -> AccountSummary:
        self._call("account_summary", account_id=account_id)
        return self._account()

    def ingest_fixture_data(self, *, days: int = 20) -> dict[str, int]:
        self._call("ingest_fixture_data", days=days)
        return {"bars": days * 2, "instruments": 2}

    def submit_proposal(self, proposal: Any, *, account_id: str | None = None) -> ProposalOutcome:
        self._call("submit_proposal", proposal=proposal, account_id=account_id)
        if self.accept_proposals:
            return ProposalOutcome(
                accepted=True,
                proposal_id="proposal-test",
                decision_id="decision-test",
                target_weights={"510300.SH": "0.20", "CASH.CNY": "0.80"},
            )
        return ProposalOutcome(
            accepted=False,
            violations=("UNKNOWN_SYMBOL", "STALE_DATA"),
            messages=("unknown symbol 999999.SH",),
        )

    def run_daily_cycle(
        self, *, trade_date: date | None = None, account_id: str | None = None
    ) -> CycleResult:
        self._call("run_daily_cycle", trade_date=trade_date, account_id=account_id)
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
        self._call("kill_switch", reason=reason, account_id=account_id)
        return self._account(state="PAUSED", pause_reason=reason)

    def resume(self, reason: str, *, account_id: str | None = None) -> AccountSummary:
        self._call("resume", reason=reason, account_id=account_id)
        return self._account(state="ACTIVE")

    def rebuild_from_ledger(self, *, account_id: str | None = None) -> RebuildResult:
        self._call("rebuild_from_ledger", account_id=account_id)
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
        self._call("list_orders", account_id=account_id, limit=limit)
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
        self._call("list_audit", limit=limit)
        return (
            AuditEntry(
                decision_id="decision-1",
                decision_type="PROPOSAL",
                created_at="2026-11-05T01:30:00+00:00",
                outcome="ACCEPTED",
                executable=False,
            ),
        )


def _client(fake: FakeContainer) -> TestClient:
    # ``raise_server_exceptions=False`` so a genuinely unexpected 500 can be
    # asserted as an HTTP response (the real server sends the same response and
    # logs the exception; Starlette re-raises it after sending).
    return TestClient(create_app(container=fake), raise_server_exceptions=False)


def _proposal_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    payload.update(overrides)
    return payload


def _walk_floats(value: Any) -> list[float]:
    if isinstance(value, float):
        return [value]
    if isinstance(value, bool):
        return []
    if isinstance(value, dict):
        found: list[float] = []
        for item in value.values():
            found.extend(_walk_floats(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_walk_floats(item))
        return found
    return []


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key).lower())
            keys |= _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _walk_keys(item)
    return keys


# ---------------------------------------------------------------------------
# System / health
# ---------------------------------------------------------------------------
def test_health_returns_200_and_json() -> None:
    fake = FakeContainer()
    response = _client(fake).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert isinstance(body["checked_at"], str)
    assert body["components"][0]["name"] == "database"
    assert fake.call_names() == ["health"]


def test_health_never_leaks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", SENTINEL_SECRET)
    monkeypatch.setenv("TUSHARE_TOKEN", SENTINEL_SECRET)
    fake = FakeContainer()
    response = _client(fake).get("/health")
    assert response.status_code == 200
    assert SENTINEL_SECRET not in response.text
    assert SENTINEL_SECRET not in str(fake.health().as_dict())
    body = response.json()
    assert not (CREDENTIAL_NAMES & _walk_keys(body))
    assert _walk_floats(body) == []


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
def test_init_account_returns_money_as_strings() -> None:
    fake = FakeContainer()
    response = _client(fake).post("/admin/init-account")
    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "janus-paper-cny-001"
    assert body["mode"] == "paper"
    assert isinstance(body["cash"], str)
    assert isinstance(body["initial_capital"], str)
    assert body["cash"] == "100000.00"
    assert _walk_floats(body) == []
    assert fake.call_names() == ["initialize_account"]


def test_get_account() -> None:
    fake = FakeContainer()
    response = _client(fake).get("/account")
    assert response.status_code == 200
    body = response.json()
    assert body["positions"][0]["symbol"] == "510300.SH"
    assert isinstance(body["positions"][0]["average_cost"], str)
    assert _walk_floats(body) == []


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------
def test_submit_proposal_accepted() -> None:
    fake = FakeContainer()
    response = _client(fake).post("/proposals", json=_proposal_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["target_weights"] == {"510300.SH": "0.20", "CASH.CNY": "0.80"}
    assert _walk_floats(body) == []
    name, kwargs = fake.calls[-1]
    assert name == "submit_proposal"
    sent = kwargs["proposal"]
    assert sent["strategy_id"] == "broad_etf_core"
    assert sent["as_of"] == "2026-11-05T15:30:00+08:00"
    assert isinstance(sent["target_weights"][0]["weight"], str)


def test_rejected_proposal_maps_to_422_with_codes() -> None:
    fake = FakeContainer()
    fake.accept_proposals = False
    response = _client(fake).post("/proposals", json=_proposal_payload())
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["accepted"] is False
    assert detail["violations"] == ["UNKNOWN_SYMBOL", "STALE_DATA"]


@pytest.mark.parametrize(
    "forbidden_field",
    [
        {"mode": "live"},
        {"add_capital": "1000"},
        {"risk_policy_override": {"max_single_instrument_weight": "1.0"}},
        {"execute_order": {"symbol": "510300.SH"}},
        {"api_key": SENTINEL_SECRET},
        {"authorization": f"Bearer {SENTINEL_SECRET}"},
        {"strategy_code": "import os; os.system('echo pwned')"},
        {"sql": "DROP TABLE ledger"},
        {"live": True},
    ],
)
def test_adversarial_unknown_fields_are_rejected_and_create_nothing(
    forbidden_field: dict[str, Any],
) -> None:
    fake = FakeContainer()
    payload = _proposal_payload()
    payload.update(forbidden_field)
    response = _client(fake).post("/proposals", json=payload)
    assert response.status_code == 422
    assert fake.calls == []  # no proposal recorded, no order created
    assert SENTINEL_SECRET not in response.text


def test_proposal_missing_required_field_is_rejected() -> None:
    fake = FakeContainer()
    payload = _proposal_payload()
    del payload["strategy_id"]
    response = _client(fake).post("/proposals", json=payload)
    assert response.status_code == 422
    assert fake.calls == []


def test_proposal_oversized_thesis_is_rejected() -> None:
    fake = FakeContainer()
    payload = _proposal_payload(thesis="x" * 2001)
    response = _client(fake).post("/proposals", json=payload)
    assert response.status_code == 422
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------
def test_cycle_run_with_trade_date() -> None:
    fake = FakeContainer()
    response = _client(fake).post("/cycles/run", json={"trade_date": "2026-11-05"})
    assert response.status_code == 200
    body = response.json()
    assert body["trade_date"] == "2026-11-05"
    assert isinstance(body["cash_after"], str)
    assert _walk_floats(body) == []
    name, kwargs = fake.calls[-1]
    assert name == "run_daily_cycle"
    assert kwargs["trade_date"] == date(2026, 11, 5)


def test_cycle_run_without_body() -> None:
    fake = FakeContainer()
    response = _client(fake).post("/cycles/run")
    assert response.status_code == 200
    name, kwargs = fake.calls[-1]
    assert name == "run_daily_cycle"
    assert kwargs["trade_date"] is None


def test_cycle_run_rejects_live_mode_field() -> None:
    fake = FakeContainer()
    response = _client(fake).post("/cycles/run", json={"mode": "live"})
    assert response.status_code == 422
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------
def test_orders_money_is_strings() -> None:
    fake = FakeContainer()
    response = _client(fake).get("/orders", params={"limit": 5})
    assert response.status_code == 200
    orders = response.json()
    assert orders[0]["order_id"] == "order-1"
    assert orders[0]["limit_price"] == "4.000"
    assert _walk_floats(orders) == []
    assert fake.calls[-1] == ("list_orders", {"account_id": None, "limit": 5})


@pytest.mark.parametrize("limit", ["0", "abc", "501"])
def test_orders_rejects_invalid_limits(limit: str) -> None:
    fake = FakeContainer()
    response = _client(fake).get("/orders", params={"limit": limit})
    assert response.status_code == 422
    assert fake.calls == []


def test_audit_listing() -> None:
    fake = FakeContainer()
    response = _client(fake).get("/audit", params={"limit": 10})
    assert response.status_code == 200
    entries = response.json()
    assert entries[0]["decision_id"] == "decision-1"
    assert fake.calls[-1] == ("list_audit", {"limit": 10})


# ---------------------------------------------------------------------------
# Administrative controls
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [{}, {"reason": ""}, {"reason": "risk breach", "mode": "live"}],
)
def test_kill_switch_requires_a_sane_reason(body: dict[str, Any]) -> None:
    fake = FakeContainer()
    response = _client(fake).post("/admin/kill-switch", json=body)
    assert response.status_code == 422
    assert fake.calls == []


def test_kill_switch_and_resume() -> None:
    fake = FakeContainer()
    response = _client(fake).post("/admin/kill-switch", json={"reason": "data integrity failure"})
    assert response.status_code == 200
    assert response.json()["state"] == "PAUSED"
    name, kwargs = fake.calls[-1]
    assert name == "kill_switch"
    assert kwargs["reason"] == "data integrity failure"

    response = _client(fake).post("/admin/resume", json={"reason": "administrator reviewed"})
    assert response.status_code == 200
    assert response.json()["state"] == "ACTIVE"
    assert fake.calls[-1][0] == "resume"


def test_rebuild_returns_strings() -> None:
    fake = FakeContainer()
    response = _client(fake).post("/admin/rebuild")
    assert response.status_code == 200
    body = response.json()
    assert body["matches"] is True
    assert isinstance(body["stored_cash"], str)
    assert isinstance(body["realized_pnl"], str)
    assert _walk_floats(body) == []
    assert fake.calls == [("rebuild_from_ledger", {"account_id": None})]


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------
def test_risk_rejection_maps_to_422() -> None:
    fake = FakeContainer()
    fake.errors["submit_proposal"] = FakeJanusError("risk limits breached", code="RISK_REJECTED")
    response = _client(fake).post("/proposals", json=_proposal_payload())
    assert response.status_code == 422
    assert response.json()["code"] == "RISK_REJECTED"


def test_paused_account_maps_to_409() -> None:
    fake = FakeContainer()
    fake.errors["run_daily_cycle"] = FakeJanusError("account is paused", code="ACCOUNT_PAUSED")
    response = _client(fake).post("/cycles/run")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "ACCOUNT_PAUSED"
    assert "Traceback" not in response.text


def test_unexpected_error_maps_to_bare_500() -> None:
    fake = FakeContainer()
    fake.errors["health"] = RuntimeError("internal detail must stay private")
    response = _client(fake).get("/health")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL"
    assert body["message"] == "internal error"
    assert "internal detail must stay private" not in response.text
    assert "Traceback" not in response.text


def test_unknown_route_is_404() -> None:
    response = _client(FakeContainer()).get("/nope")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Static security checks
# ---------------------------------------------------------------------------
def test_openapi_has_no_dangerous_request_fields() -> None:
    """Request schemas must not expose execution/policy/capital/credential knobs."""
    schema = _client(FakeContainer()).get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    allowed = {
        "TargetWeightIn",
        "ProposalIn",
        "CycleRunIn",
        "PauseIn",
        "KillSwitchIn",
        "ResumeIn",
    }
    forbidden = {
        "mode",
        "live",
        "add_capital",
        "risk_policy_override",
        "execute_order",
        "api_key",
        "token",
        "secret",
        "authorization",
        "strategy_code",
        "sql",
    }
    for name, definition in schemas.items():
        if name not in allowed:
            continue
        field_names = set(definition.get("properties", {}))
        assert not (field_names & forbidden), (name, field_names & forbidden)


def test_api_does_not_require_authentication_header() -> None:
    """Phase 0 has no auth: localhost binding is the only control (documented)."""
    assert _client(FakeContainer()).get("/health").status_code == 200


def test_api_module_does_not_import_sqlalchemy() -> None:
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    prefix = str(root / "apps" / "api")
    env["PYTHONPATH"] = prefix if not existing else prefix + os.pathsep + existing
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, janus_api.app; print('sqlalchemy' in sys.modules)",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "False"
