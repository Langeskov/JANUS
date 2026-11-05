"""Stub (no-LLM) agents and the explicit agent boundary (Phase 0 spec s8.3, s9).

Two agent objects live here:

``FixtureAgent``
    The P0-G stub agent. It returns a fixed, schema-validated
    :class:`~janus.domain.strategy.proposal.AllocationProposal` loaded from a local
    JSON fixture. It makes no model call, no network call and has no execution
    capability, so the whole Phase 0 workflow is deterministic and offline.

``ReadOnlyResearchAgent``
    Demonstrates the permission isolation required by spec s9.1: it receives a
    *read-only* market-data protocol, wraps external text as untrusted quoted
    data, and exposes no execution, risk-approval, ledger-mutation, policy or
    credential method. ``FORBIDDEN_AGENT_CAPABILITIES`` is the explicit,
    machine-checkable statement of that boundary; the security tests assert that
    neither the classes nor their instances define any of those names.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from janus.agents.schemas import parse_proposal_payload
from janus.domain.common.errors import SecurityViolationError, ValidationError
from janus.domain.market_data.models import MarketBar
from janus.domain.strategy.proposal import AllocationProposal, find_prohibited_content

__all__ = [
    "FORBIDDEN_AGENT_CAPABILITIES",
    "FixtureAgent",
    "QuotedExternalContent",
    "ReadOnlyMarketData",
    "ReadOnlyResearchAgent",
    "ResearchDigest",
    "load_fixture_payload",
]

FORBIDDEN_AGENT_CAPABILITIES: Final[tuple[str, ...]] = (
    # -- execution tools (spec s9.1, s9.10) ------------------------------
    "execute",
    "execute_order",
    "place_order",
    "submit_order",
    "submit_paper_order",
    "send_order",
    "call_broker",
    "broker_api",
    "invoke_tool",
    "call_tool",
    "use_tool",
    # -- risk self-approval (spec s8.3) ----------------------------------
    "approve_risk",
    "evaluate_risk",
    "override_risk",
    "bypass_risk",
    "set_risk_decision",
    "risk_approve",
    # -- ledger / account mutation (spec s5.1, s8.3) ---------------------
    "write_ledger",
    "append_ledger",
    "append_fill",
    "record_fill",
    "mutate_ledger",
    "save_order",
    "set_cash",
    "set_balance",
    "add_capital",
    "deposit",
    "withdraw",
    "top_up",
    # -- policy / config edit (spec s3.1, s8.1) --------------------------
    "set_risk_policy",
    "change_risk_limit",
    "edit_policy",
    "update_config",
    # -- credentials and hostile capabilities (spec s8.3, s9) ------------
    "load_credentials",
    "read_credentials",
    "read_env",
    "load_env",
    "get_api_key",
    "run_shell",
    "run_sql",
    "run_python",
    "exec_code",
    "execute_code",
)
"""Names an agent object must never expose. Asserted by the security tests."""

_FORBIDDEN_DATA_CAPABILITIES: Final[tuple[str, ...]] = (
    "execute",
    "execute_order",
    "place_order",
    "submit_order",
    "save_order",
    "append_fill",
    "write_ledger",
    "append_ledger",
    "set_balance",
    "add_capital",
    "withdraw",
    "approve_risk",
    "load_credentials",
    "read_credentials",
    "run_shell",
)
"""Subset refused on an injected data source: read-only means read-only."""

MAX_EXTERNAL_TEXT_CHARS: Final[int] = 20_000
"""External article/news text is truncated to this many characters before quoting."""

_UNTRUSTED_OPEN: Final[str] = "<<<UNTRUSTED_DATA>>>"
_UNTRUSTED_CLOSE: Final[str] = "<<<END_UNTRUSTED_DATA>>>"


@runtime_checkable
class ReadOnlyMarketData(Protocol):
    """The only data capability an agent receives: deterministic read methods."""

    def get_daily_bar(self, symbol: str, trade_date: date) -> MarketBar | None: ...

    def get_daily_bars(self, symbol: str, start: date, end: date) -> tuple[MarketBar, ...]: ...


@dataclass(frozen=True, slots=True)
class QuotedExternalContent:
    """One piece of external material, wrapped as *quoted data*.

    Spec s9.2: external text is passed as data content, never as privileged
    instructions. ``suspicious_instruction`` records the defence-in-depth text
    scan; it is *not* a security boundary (spec s9: classifiers are not a
    boundary, permission isolation and schema validation are).
    """

    source_id: str
    text: str
    truncated: bool = False
    suspicious_instruction: bool = False

    def as_quoted_block(self) -> str:
        """Render the content inside an explicit untrusted-data delimiter."""
        return f"{_UNTRUSTED_OPEN}\nsource: {self.source_id}\n{self.text}\n{_UNTRUSTED_CLOSE}"


@dataclass(frozen=True, slots=True)
class ResearchDigest:
    """A read-only research view. It contains no order, order-intent or authority."""

    symbol: str
    trade_date: date
    bar: MarketBar | None
    sources: tuple[QuotedExternalContent, ...] = ()
    suspicious_instruction_detected: bool = False

    @property
    def has_execution_authority(self) -> bool:
        """Research output can never execute; this is a constant False."""
        return False

    def quoted_sources(self) -> tuple[str, ...]:
        return tuple(source.as_quoted_block() for source in self.sources)


class FixtureAgent:
    """Deterministic stub agent: a fixed proposal from a local JSON fixture.

    Parameters
    ----------
    fixture_path:
        Path to a JSON file matching the strict payload schema. It is read once
        at construction; there is no network, model or tool call.
    """

    agent_id: Final[str] = "fixture-agent"
    model_id: Final[str] = "fixture://no-llm"
    prompt_version: Final[str] = "research_agent_v0_1_0"

    def __init__(self, fixture_path: Path | str) -> None:
        path = Path(fixture_path)
        if not path.is_file():
            raise ValidationError("proposal fixture file does not exist", path=str(path))
        self._path: Path = path
        self._payload_text: str = path.read_text(encoding="utf-8")

    @property
    def fixture_path(self) -> Path:
        return self._path

    def propose(self) -> AllocationProposal:
        """Return a freshly validated proposal parsed from the fixture."""
        return parse_proposal_payload(self._payload_text)


class ReadOnlyResearchAgent:
    """Read-only research boundary: no execution, risk, ledger or secret access.

    The constructor refuses a data source that exposes mutation/execution
    methods, so a wiring mistake fails closed instead of silently granting the
    agent write power.
    """

    agent_id: Final[str] = "read-only-research-agent"

    def __init__(self, market_data: ReadOnlyMarketData) -> None:
        exposed = [
            name
            for name in _FORBIDDEN_DATA_CAPABILITIES
            if callable(getattr(market_data, name, None))
        ]
        if exposed:
            raise SecurityViolationError(
                "agent data source must be read-only; it exposes mutation/execution capabilities",
                capabilities=sorted(exposed),
            )
        self._market_data: ReadOnlyMarketData = market_data

    def read_bar(self, symbol: str, trade_date: date) -> MarketBar | None:
        """Read one daily bar (read-only)."""
        return self._market_data.get_daily_bar(symbol, trade_date)

    def read_bars(self, symbol: str, start: date, end: date) -> tuple[MarketBar, ...]:
        """Read a daily-bar range (read-only)."""
        return self._market_data.get_daily_bars(symbol, start, end)

    def quote_untrusted(self, text: str, *, source_id: str) -> QuotedExternalContent:
        """Wrap external text as untrusted quoted data, never as instructions."""
        truncated = len(text) > MAX_EXTERNAL_TEXT_CHARS
        bounded = text[:MAX_EXTERNAL_TEXT_CHARS]
        return QuotedExternalContent(
            source_id=source_id,
            text=bounded,
            truncated=truncated,
            suspicious_instruction=bool(find_prohibited_content(bounded)),
        )

    def digest(
        self,
        *,
        symbol: str,
        trade_date: date,
        external_texts: Sequence[tuple[str, str]] = (),
    ) -> ResearchDigest:
        """Collect a read-only research digest for one symbol/date."""
        bar = self._market_data.get_daily_bar(symbol, trade_date)
        sources = tuple(
            self.quote_untrusted(text, source_id=source_id) for source_id, text in external_texts
        )
        return ResearchDigest(
            symbol=symbol,
            trade_date=trade_date,
            bar=bar,
            sources=sources,
            suspicious_instruction_detected=any(
                source.suspicious_instruction for source in sources
            ),
        )


def load_fixture_payload(fixture_path: Path | str) -> dict[str, object]:
    """Load a JSON fixture object without validating it (test helper)."""
    path = Path(fixture_path)
    if not path.is_file():
        raise ValidationError("fixture file does not exist", path=str(path))
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SecurityViolationError("fixture is not valid JSON", error=str(exc)) from exc
    if not isinstance(parsed, dict):
        raise SecurityViolationError("fixture must contain a JSON object")
    return parsed
