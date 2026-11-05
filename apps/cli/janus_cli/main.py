"""JANUS Phase 0 command-line interface.

Usage
-----
Run ``janus --help`` (or ``python -m janus_cli --help``). Global options
(``--config-dir``, ``--db-url``, ``--json``) may be given before or after the
command name. Use ``--json`` for machine-readable output; stdout is otherwise a
human-readable summary and errors always go to stderr.

Exit codes
----------
``0`` success, ``2`` invalid usage, ``3`` rejection/validation failure,
``4`` internal or configuration error.

Layering
--------
The CLI is a thin transport: it parses arguments, calls the
:class:`janus.application.container.JanusContainer` facade, and prints DTOs. It
never imports SQLAlchemy, a provider SDK, or an LLM SDK. The composition root is
imported lazily inside :func:`_build_container` so tests can inject a fake
container (``main([...], container=fake)``) without any infrastructure.

Phase 0 is paper-only: there is no brokerage connection, no live trading, and no
command that can add capital or change risk policy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from janus.application.container import (
    AccountSummary,
    AuditEntry,
    CycleResult,
    HealthReport,
    JanusContainer,
    OrderSummary,
    ProposalOutcome,
    RebuildResult,
)

from janus_cli.errors import describe, is_rejection

__all__ = ["EXIT_INTERNAL", "EXIT_OK", "EXIT_REJECTED", "EXIT_USAGE", "main"]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_INTERNAL = 4

_DEFAULT_LIMIT = 50
_DEFAULT_FIXTURE_DAYS = 20


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def _global_parent(*, suppress_defaults: bool) -> argparse.ArgumentParser:
    """Options accepted both before and after the sub-command.

    On sub-parsers the defaults are suppressed so they cannot clobber a value
    already parsed before the command name (``janus --json health``).
    """
    default: Any = argparse.SUPPRESS if suppress_defaults else None
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--config-dir",
        metavar="PATH",
        default=default,
        help="Directory holding the YAML configuration (default: repository config/).",
    )
    parent.add_argument(
        "--db-url",
        metavar="URL",
        default=default,
        help="Database URL override (default: configured SQLite database).",
    )
    parent.add_argument(
        "--json",
        action="store_true",
        default=default,
        help="Emit machine-readable JSON on stdout.",
    )
    return parent


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {parsed}")
    return parsed


def _non_empty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value.strip()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a date in YYYY-MM-DD form, got {value!r}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser (exposed for ``--help`` tests)."""
    parser = argparse.ArgumentParser(
        prog="janus",
        description=(
            "JANUS Phase 0 paper-trading CLI. Paper only: no brokerage, no live "
            "trading, no capital changes."
        ),
        epilog=(
            "Exit codes: 0 success, 2 invalid usage, 3 rejection/validation "
            "failure, 4 internal error."
        ),
        parents=[_global_parent(suppress_defaults=False)],
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return commands.add_parser(
            name, help=help_text, parents=[_global_parent(suppress_defaults=True)]
        )

    add("health", "Show a redacted system health report.")
    add("init-account", "Create the configured fixed-capital paper account (idempotent).")
    add("status", "Show the current paper account and valuation.")

    ingest = add("ingest-fixtures", "Load deterministic fixture bars and instruments.")
    ingest.add_argument("--days", type=_positive_int, default=_DEFAULT_FIXTURE_DAYS, metavar="N")

    propose = add("propose", "Validate a structured allocation proposal from a JSON file.")
    propose.add_argument(
        "--file", required=True, metavar="JSON", help="Path to a proposal JSON object."
    )

    cycle = add("run-cycle", "Run one daily paper-trading cycle.")
    cycle.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Trade date (default: the container clock's today).",
    )

    kill = add("kill-switch", "Pause the account. Never liquidates positions.")
    kill.add_argument("--reason", type=_non_empty, required=True, metavar="TEXT")

    resume = add("resume", "Resume a paused account (administrative, audited).")
    resume.add_argument("--reason", type=_non_empty, required=True, metavar="TEXT")

    add("rebuild", "Rebuild the account from its immutable ledger and compare.")

    audit = add("audit", "List recent decision/audit lineage.")
    audit.add_argument("--limit", type=_positive_int, default=_DEFAULT_LIMIT, metavar="N")

    orders = add("orders", "List recent paper orders.")
    orders.add_argument("--limit", type=_positive_int, default=_DEFAULT_LIMIT, metavar="N")

    return parser


# ---------------------------------------------------------------------------
# Output helpers (stdout only; errors go to stderr)
# ---------------------------------------------------------------------------
def _json_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _print_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))


def _print_health(report: HealthReport) -> None:
    print(f"JANUS health: {'OK' if report.ok else 'DEGRADED'}")
    print(f"checked_at: {report.checked_at.isoformat()}")
    if report.detail:
        print(f"detail: {report.detail}")
    for component in report.components:
        status = "ok" if component.ok else "FAIL"
        suffix = f" ({component.detail})" if component.detail else ""
        print(f"  - {component.name}: {status}{suffix}")


def _print_account(account: AccountSummary) -> None:
    print(f"account: {account.account_id}")
    print(f"mode: {account.mode}  state: {account.state}")
    print(
        f"cash: {account.cash}  nav: {account.nav}  "
        f"cash_weight: {account.cash_weight}  initial_capital: {account.initial_capital}"
    )
    print(f"valuation_date: {account.valuation_date}  as_of: {account.as_of}")
    if account.pause_reason:
        print(f"pause_reason: {account.pause_reason}")
    if not account.positions:
        print("positions: none")
        return
    print("positions:")
    print(
        f"  {'symbol':<10} {'qty':>8} {'avail':>8} {'avg_cost':>12} "
        f"{'market_value':>14} {'weight':>10} {'stale':>5}"
    )
    for position in account.positions:
        print(
            f"  {position.symbol:<10} {position.quantity:>8} "
            f"{position.available_quantity:>8} {position.average_cost:>12} "
            f"{position.market_value:>14} {position.weight:>10} "
            f"{position.price_is_stale!s:>5}"
        )


def _print_proposal_outcome(outcome: ProposalOutcome) -> None:
    print(f"accepted: {outcome.accepted}")
    if outcome.proposal_id:
        print(f"proposal_id: {outcome.proposal_id}")
    if outcome.decision_id:
        print(f"decision_id: {outcome.decision_id}")
    for code in outcome.violations:
        print(f"violation: {code}")
    for message in outcome.messages:
        print(f"message: {message}")
    if outcome.target_weights:
        print("target_weights:")
        for symbol, weight in sorted(outcome.target_weights.items()):
            print(f"  {symbol}: {weight}")


def _print_cycle(result: CycleResult) -> None:
    print(f"trade_date: {result.trade_date}  account_state: {result.account_state}")
    print(
        f"orders: proposed={result.orders_proposed} rejected={result.orders_rejected} "
        f"filled={result.orders_filled} fills={result.fills}"
    )
    print(f"cash_after: {result.cash_after}  nav_after: {result.nav_after}")
    for note in result.notes:
        print(f"note: {note}")


def _print_orders(orders: Sequence[OrderSummary]) -> None:
    if not orders:
        print("orders: none")
        return
    for order in orders:
        print(
            f"{order.order_id} {order.symbol} {order.side} qty={order.quantity} "
            f"filled={order.filled_quantity} status={order.status} "
            f"trade_date={order.target_trade_date}"
        )


def _print_audit(entries: Sequence[AuditEntry]) -> None:
    if not entries:
        print("audit: empty")
        return
    for entry in entries:
        print(
            f"{entry.created_at} {entry.decision_type} outcome={entry.outcome} "
            f"executable={entry.executable} decision_id={entry.decision_id}"
        )


def _print_rebuild(result: RebuildResult) -> None:
    print(f"account: {result.account_id}  matches: {result.matches}")
    print(f"cash: stored={result.stored_cash} rebuilt={result.rebuilt_cash}")
    print(
        f"events: cash_entries={result.cash_entry_count} "
        f"position_events={result.position_event_count} "
        f"realized_pnl={result.realized_pnl}"
    )
    for mismatch in result.mismatches:
        print(f"mismatch: {mismatch}")


# ---------------------------------------------------------------------------
# Command handlers: each is parse -> container call -> print, nothing else.
# ---------------------------------------------------------------------------
def _cmd_health(args: argparse.Namespace, container: JanusContainer) -> int:
    report = container.health()
    if _json_requested(args):
        _print_json(report.as_dict())
    else:
        _print_health(report)
    return EXIT_OK


def _cmd_init_account(args: argparse.Namespace, container: JanusContainer) -> int:
    account = container.initialize_account()
    if _json_requested(args):
        _print_json(account.as_dict())
    else:
        print("paper account initialised")
        _print_account(account)
    return EXIT_OK


def _cmd_status(args: argparse.Namespace, container: JanusContainer) -> int:
    account = container.account_summary()
    if _json_requested(args):
        _print_json(account.as_dict())
    else:
        _print_account(account)
    return EXIT_OK


def _cmd_ingest_fixtures(args: argparse.Namespace, container: JanusContainer) -> int:
    counts = container.ingest_fixture_data(days=int(args.days))
    if _json_requested(args):
        _print_json(counts)
    else:
        print("fixture data ingested")
        for name, count in sorted(counts.items()):
            print(f"  {name}: {count}")
    return EXIT_OK


def _read_proposal_file(path: str) -> dict[str, Any]:
    """Read a proposal JSON object; syntax/shape failures are rejections (exit 3)."""
    text = Path(path).read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("proposal file must contain a JSON object")
    return payload


def _cmd_propose(args: argparse.Namespace, container: JanusContainer) -> int:
    try:
        proposal = _read_proposal_file(str(args.file))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: cannot load proposal file {args.file!r}: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    outcome = container.submit_proposal(proposal)
    if _json_requested(args):
        _print_json(outcome.as_dict())
    else:
        _print_proposal_outcome(outcome)
    if not outcome.accepted:
        for code in outcome.violations:
            print(f"rejected: {code}", file=sys.stderr)
        return EXIT_REJECTED
    return EXIT_OK


def _cmd_run_cycle(args: argparse.Namespace, container: JanusContainer) -> int:
    result = container.run_daily_cycle(trade_date=args.date)
    if _json_requested(args):
        _print_json(result.as_dict())
    else:
        _print_cycle(result)
    return EXIT_OK


def _cmd_kill_switch(args: argparse.Namespace, container: JanusContainer) -> int:
    account = container.kill_switch(str(args.reason))
    if _json_requested(args):
        _print_json(account.as_dict())
    else:
        print(f"kill switch engaged: {args.reason}")
        _print_account(account)
    return EXIT_OK


def _cmd_resume(args: argparse.Namespace, container: JanusContainer) -> int:
    account = container.resume(str(args.reason))
    if _json_requested(args):
        _print_json(account.as_dict())
    else:
        print(f"account resumed: {args.reason}")
        _print_account(account)
    return EXIT_OK


def _cmd_rebuild(args: argparse.Namespace, container: JanusContainer) -> int:
    result = container.rebuild_from_ledger()
    if _json_requested(args):
        _print_json(result.as_dict())
    else:
        _print_rebuild(result)
    if not result.matches:
        print(
            "rejected: account does not rebuild exactly from its ledger",
            file=sys.stderr,
        )
        return EXIT_REJECTED
    return EXIT_OK


def _cmd_audit(args: argparse.Namespace, container: JanusContainer) -> int:
    entries = container.list_audit(limit=int(args.limit))
    if _json_requested(args):
        _print_json({"entries": [entry.as_dict() for entry in entries]})
    else:
        _print_audit(entries)
    return EXIT_OK


def _cmd_orders(args: argparse.Namespace, container: JanusContainer) -> int:
    orders = container.list_orders(limit=int(args.limit))
    if _json_requested(args):
        _print_json({"orders": [order.as_dict() for order in orders]})
    else:
        _print_orders(orders)
    return EXIT_OK


_Handler = Callable[[argparse.Namespace, JanusContainer], int]

_HANDLERS: dict[str, _Handler] = {
    "health": _cmd_health,
    "init-account": _cmd_init_account,
    "status": _cmd_status,
    "ingest-fixtures": _cmd_ingest_fixtures,
    "propose": _cmd_propose,
    "run-cycle": _cmd_run_cycle,
    "kill-switch": _cmd_kill_switch,
    "resume": _cmd_resume,
    "rebuild": _cmd_rebuild,
    "audit": _cmd_audit,
    "orders": _cmd_orders,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _build_container(args: argparse.Namespace) -> JanusContainer:
    """Build the real composition root lazily (tests inject a fake instead)."""
    from janus.application.container import build_container

    return build_container(
        config_dir=getattr(args, "config_dir", None),
        database_url=getattr(args, "db_url", None),
    )


def main(argv: list[str] | None = None, *, container: JanusContainer | None = None) -> int:
    """Run one CLI invocation and return the process exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help / usage errors raise SystemExit(n)
        code = exc.code
        return code if isinstance(code, int) else EXIT_USAGE

    handler = _HANDLERS.get(str(getattr(args, "command", "")))
    if handler is None:  # defensive; argparse guarantees a sub-command
        parser.print_usage(sys.stderr)
        return EXIT_USAGE

    try:
        resolved = container if container is not None else _build_container(args)
        return int(handler(args, resolved))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("error: interrupted", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # transport boundary maps every failure
        print(f"error: {describe(exc)}", file=sys.stderr)
        return EXIT_REJECTED if is_rejection(exc) else EXIT_INTERNAL
