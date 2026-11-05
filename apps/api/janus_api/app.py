"""JANUS Phase 0 HTTP API.

SECURITY
--------
Phase 0 has **no authentication** and must therefore be bound to
``127.0.0.1`` only (``uvicorn janus_api.app:app --host 127.0.0.1``). Do not
expose it on a LAN interface, in a container with a published port, or behind a
public reverse proxy. Authentication/authorisation is a later-phase decision and
requires its own security review before the API leaves localhost.

The surface is deliberately narrow and paper-only:

* there is no endpoint that accepts strategy source code, shell commands, SQL,
  credentials, a ``live`` mode, capital changes, or risk-policy overrides --
  request models use ``extra="forbid"`` so such fields are a 422;
* money is always a decimal string in responses, never a float;
* controlled rejections map to 4xx (409/422/403/404), never 500;
* ``GET /health`` never contains credentials.

The app talks to the system exclusively through
:class:`janus.application.container.JanusContainer`; it never imports
SQLAlchemy, a provider SDK, or an LLM SDK. The concrete composition root is
imported lazily on the first request unless a container was injected
(``create_app(container=fake)``), which is how the contract tests run offline.

Error handling
--------------
Every container call goes through :func:`_run`, which converts a controlled
:class:`~janus.domain.common.errors.JanusError`-shaped failure into an
:class:`ApiError`. ``ApiError`` has a dedicated exception handler, so Starlette
returns the 4xx response *instead of* treating it as a server error (an
``Exception`` handler would send a 500 and then re-raise, which is wrong for a
normal business rejection). Only genuinely unexpected failures reach the
generic 500 handler, whose body is a bare ``{"code": "INTERNAL", ...}``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from janus.application.container import JanusContainer

from janus_api.errors import http_error_for, sanitize_validation_errors
from janus_api.schemas import (
    AccountOut,
    AuditOut,
    CycleOut,
    CycleRunIn,
    HealthOut,
    KillSwitchIn,
    OrderOut,
    ProposalIn,
    ProposalOut,
    RebuildOut,
    ResumeIn,
)

__all__ = ["ApiError", "app", "create_app"]

_DESCRIPTION = """\
JANUS Phase 0 operational API -- **paper trading only**.

No brokerage connection, no live orders, no capital flows, no risk-policy
edits, and no authentication. Bind to `127.0.0.1` only.

All money/weight values are decimal strings. Controlled rejections are 4xx
responses carrying stable violation codes.
"""


class ApiError(Exception):
    """Controlled failure carrying the HTTP status and JSON body to return."""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message", "error")))
        self.status_code: int = status_code
        self.payload: dict[str, Any] = payload


def _run[T](action: Callable[[], T]) -> T:
    """Run a container call, converting a controlled failure to :class:`ApiError`.

    A rejection is a normal, structured outcome: it must become a 4xx response,
    not a server error. Read-only/pure transport exceptions (``HTTPException``)
    pass through untouched.
    """
    try:
        return action()
    except HTTPException:
        raise
    except Exception as exc:
        status_code, payload = http_error_for(exc)
        raise ApiError(status_code, payload) from exc


def create_app(container: JanusContainer | None = None) -> FastAPI:
    """Create the FastAPI application.

    ``container`` is injected by tests; when omitted the composition root
    (``janus.application.container.build_container``) is resolved lazily on the
    first request, so importing this module never opens a database or loads a
    provider credential.
    """
    app = FastAPI(
        title="JANUS Phase 0 API",
        version="0.1.0",
        description=_DESCRIPTION,
        docs_url="/docs",
        redoc_url=None,
    )

    resolved: list[JanusContainer] = []

    def get_container() -> JanusContainer:
        if container is not None:
            return container
        if not resolved:
            from janus.application.container import build_container

            resolved.append(build_container())
        return resolved[0]

    @app.exception_handler(ApiError)
    def _controlled_error(_request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, ApiError):  # pragma: no cover - defensive
            return JSONResponse(status_code=500, content={"code": "INTERNAL"})
        return JSONResponse(status_code=exc.status_code, content=exc.payload)

    @app.exception_handler(RequestValidationError)
    def _invalid_request(_request: Request, exc: Exception) -> JSONResponse:
        # Never echo raw request input back (pydantic errors carry an ``input``).
        errors = exc.errors() if isinstance(exc, RequestValidationError) else []
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION",
                "message": "request did not match the application schema",
                "errors": sanitize_validation_errors(errors),
            },
        )

    @app.exception_handler(Exception)
    def _unhandled_error(_request: Request, exc: Exception) -> JSONResponse:
        # Only unexpected failures reach here: no traceback, no internal detail.
        status_code, payload = http_error_for(exc)
        return JSONResponse(status_code=status_code, content=payload)

    # -- system -----------------------------------------------------------
    @app.get("/health", response_model=HealthOut, tags=["system"])
    def health() -> HealthOut:
        """Process/component health. Never contains credentials."""
        return HealthOut.from_dto(_run(lambda: get_container().health()))

    # -- account ----------------------------------------------------------
    @app.get("/account", response_model=AccountOut, tags=["account"])
    def account(
        account_id: str | None = Query(default=None, min_length=1, max_length=64),
    ) -> AccountOut:
        summary = _run(lambda: get_container().account_summary(account_id=account_id))
        return AccountOut.from_dto(summary)

    @app.post("/admin/init-account", response_model=AccountOut, tags=["admin"])
    def init_account() -> AccountOut:
        """Create the configured fixed-capital paper account (idempotent)."""
        return AccountOut.from_dto(_run(get_container().initialize_account))

    # -- proposals and cycles ---------------------------------------------
    @app.post("/proposals", response_model=ProposalOut, tags=["proposals"])
    def submit_proposal(payload: ProposalIn) -> ProposalOut:
        """Validate a structured allocation proposal.

        A rejected proposal is a 422 carrying its stable violation codes; an
        accepted proposal is recorded but never executed here.
        """
        outcome = _run(lambda: get_container().submit_proposal(payload.model_dump(mode="json")))
        if not outcome.accepted:
            raise HTTPException(
                status_code=422,
                detail={"code": "PROPOSAL_REJECTED", **outcome.as_dict()},
            )
        return ProposalOut.from_dto(outcome)

    @app.post("/cycles/run", response_model=CycleOut, tags=["cycles"])
    def run_cycle(payload: CycleRunIn | None = None) -> CycleOut:
        """Run one daily paper cycle (risk-checked paper orders only)."""
        request = payload if payload is not None else CycleRunIn()
        result = _run(
            lambda: get_container().run_daily_cycle(
                trade_date=request.trade_date, account_id=request.account_id
            )
        )
        return CycleOut.from_dto(result)

    # -- read models ------------------------------------------------------
    @app.get("/orders", response_model=list[OrderOut], tags=["orders"])
    def list_orders(
        limit: int = Query(default=50, ge=1, le=500),
        account_id: str | None = Query(default=None, min_length=1, max_length=64),
    ) -> list[OrderOut]:
        orders = _run(lambda: get_container().list_orders(account_id=account_id, limit=limit))
        return [OrderOut.from_dto(order) for order in orders]

    @app.get("/audit", response_model=list[AuditOut], tags=["audit"])
    def list_audit(
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[AuditOut]:
        entries = _run(lambda: get_container().list_audit(limit=limit))
        return [AuditOut.from_dto(entry) for entry in entries]

    # -- administrative controls (audited; never execute anything) --------
    @app.post("/admin/kill-switch", response_model=AccountOut, tags=["admin"])
    def kill_switch(payload: KillSwitchIn) -> AccountOut:
        """Pause the account. Never liquidates positions."""
        summary = _run(
            lambda: get_container().kill_switch(payload.reason, account_id=payload.account_id)
        )
        return AccountOut.from_dto(summary)

    @app.post("/admin/resume", response_model=AccountOut, tags=["admin"])
    def resume(payload: ResumeIn) -> AccountOut:
        """Resume a paused account (administrative, audited)."""
        summary = _run(
            lambda: get_container().resume(payload.reason, account_id=payload.account_id)
        )
        return AccountOut.from_dto(summary)

    @app.post("/admin/rebuild", response_model=RebuildOut, tags=["admin"])
    def rebuild() -> RebuildOut:
        """Rebuild the account from its immutable ledger and compare."""
        return RebuildOut.from_dto(_run(get_container().rebuild_from_ledger))

    return app


#: Module-level ASGI app for ``uvicorn janus_api.app:app``.
#: The container is resolved lazily on the first request (never at import time).
app = create_app()
