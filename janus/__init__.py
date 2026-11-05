"""JANUS - autonomous portfolio management system (Phase 0).

Phase 0 is a research and paper-trading system only. It must never connect to a
real brokerage account, submit real orders, move funds, or grant an agent
execution privileges.

Layer dependency rule (Phase 0 spec s4.2):

    apps -> application -> domain
    infrastructure -> domain ports/interfaces

The ``janus.domain`` package must not import Qt, FastAPI, SQLAlchemy, an LLM
SDK, a broker SDK, or a market-data provider SDK.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
