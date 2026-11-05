"""Architectural layering guards.

Phase 0 spec §4.2 fixes the dependency direction::

    apps -> application -> domain
    infrastructure -> domain ports/interfaces

The Domain layer must not import FastAPI, SQLAlchemy, YAML loaders, a specific
LLM SDK, a broker SDK, or a data-provider SDK. These tests enforce that
structurally by parsing the import graph, so a convenient import cannot quietly
reintroduce a dependency.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_ROOT = REPO_ROOT / "janus" / "domain"
APPLICATION_ROOT = REPO_ROOT / "janus" / "application"
AGENTS_ROOT = REPO_ROOT / "janus" / "agents"

FORBIDDEN_IN_DOMAIN = {
    "fastapi",
    "starlette",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "yaml",
    "httpx",
    "requests",
    "tushare",
    "akshare",
    "openai",
    "anthropic",
    "transformers",
    "torch",
    "PyQt5",
    "PyQt6",
    "PySide6",
    "tkinter",
}
"""Third-party modules that the framework-free domain layer must not import."""

FORBIDDEN_IN_APPLICATION = {"sqlalchemy", "alembic", "fastapi", "starlette", "uvicorn"}

ALLOWED_DOMAIN_TOP_LEVEL = set(sys.stdlib_module_names) | {"__future__", "janus", "pydantic"}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    """Fully-qualified module names referenced by ``path`` (absolute imports)."""
    modules: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import stays inside the package
                continue
            if node.module:
                modules.add(node.module)
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _top_level(path: Path) -> set[str]:
    return {module.split(".")[0] for module in _imported_modules(path)}


def _domain_files() -> list[Path]:
    return sorted(DOMAIN_ROOT.rglob("*.py"))


def test_domain_has_files() -> None:
    assert _domain_files(), "expected domain modules to exist"


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_imports_no_infrastructure_or_frameworks(path: Path) -> None:
    offending = _top_level(path) & FORBIDDEN_IN_DOMAIN
    assert not offending, f"{path.relative_to(REPO_ROOT)} imports {sorted(offending)}"


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_does_not_import_upwards(path: Path) -> None:
    modules = _imported_modules(path)
    upward = {
        module
        for module in modules
        if module == "janus.application"
        or module.startswith("janus.application.")
        or module == "janus.infrastructure"
        or module.startswith("janus.infrastructure.")
        or module == "janus.agents"
        or module.startswith("janus.agents.")
        or module == "apps"
        or module.startswith("apps.")
    }
    assert not upward, f"{path.relative_to(REPO_ROOT)} imports upwards: {sorted(upward)}"


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_depends_only_on_stdlib_janus_and_pydantic(path: Path) -> None:
    unexpected = _top_level(path) - ALLOWED_DOMAIN_TOP_LEVEL
    assert not unexpected, (
        f"{path.relative_to(REPO_ROOT)} imports unexpected third-party modules: "
        f"{sorted(unexpected)}"
    )


def test_application_services_do_not_import_orm_or_web_frameworks() -> None:
    for path in sorted(APPLICATION_ROOT.rglob("*.py")):
        offending = _top_level(path) & FORBIDDEN_IN_APPLICATION
        assert not offending, f"{path.relative_to(REPO_ROOT)} imports {sorted(offending)}"


def test_agents_package_has_no_execution_capability() -> None:
    """The agent package must not import the ledger, gateway or risk controller."""
    forbidden_prefixes = (
        "janus.domain.ledger",
        "janus.infrastructure.paper_execution",
        "janus.domain.risk.controller",
        "janus.application.services.risk_service",
    )
    for path in sorted(AGENTS_ROOT.rglob("*.py")):
        modules = _imported_modules(path)
        hits = sorted(
            module
            for module in modules
            for prefix in forbidden_prefixes
            if module == prefix or module.startswith(f"{prefix}.")
        )
        assert not hits, f"{path.relative_to(REPO_ROOT)} imports {hits}"


def test_domain_is_importable_without_any_infrastructure_package() -> None:
    """Import every domain module in a fresh interpreter state."""
    import importlib

    for path in _domain_files():
        relative = path.relative_to(REPO_ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        module_name = ".".join(parts)
        importlib.import_module(module_name)
