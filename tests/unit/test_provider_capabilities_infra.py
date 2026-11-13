"""Unit tests for provider capability declarations in infrastructure (PR-001 §5).

These tests are offline and deterministic: a capability declaration is pure data.
The invariant under test is that a provider never claims a dataset it cannot
supply, and that the fail-closed gate turns an undeclared capability into
``UnsupportedEntityError`` instead of letting an adapter return an empty
"success" that is indistinguishable from "no data".
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from janus.domain.common.errors import ErrorCode, UnsupportedEntityError, ValidationError
from janus.domain.data_foundation.capabilities import (
    DataCapability,
    ProviderCapabilities,
    capability_change,
)
from janus.infrastructure.market_data import (
    PHASE0_DECLARATION_AT,
    AKShareProvider,
    FixtureProvider,
    TushareProvider,
    require_capability,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_ROOT = REPO_ROOT / "janus" / "domain"
PROVIDER_SDK_MODULES = frozenset({"tushare", "akshare"})
LATER = datetime(2026, 12, 1, tzinfo=UTC)


def _fixture(*, declared_at: datetime | None = None) -> FixtureProvider:
    """A fixture provider with no instruments: capabilities do not need data."""
    return FixtureProvider((), declared_at=declared_at)


# ---------------------------------------------------------------------------
# FixtureProvider declaration
# ---------------------------------------------------------------------------
def test_fixture_provider_declares_daily_bars() -> None:
    capabilities = _fixture().capabilities()

    assert capabilities.provider_id == "fixture"
    assert capabilities.supported == frozenset({DataCapability.DAILY_BARS})
    assert capabilities.declaration_reason == "deterministic offline fixtures"
    assert capabilities.notes
    # No exception: the declared capability is served.
    capabilities.require(DataCapability.DAILY_BARS)


def test_fixture_provider_refuses_undeclared_capabilities() -> None:
    capabilities = _fixture().capabilities()

    with pytest.raises(UnsupportedEntityError) as excinfo:
        capabilities.require(DataCapability.HISTORICAL_MINUTES)

    assert excinfo.value.code is ErrorCode.UNSUPPORTED
    assert excinfo.value.details["capability"] == "HISTORICAL_MINUTES"
    assert excinfo.value.details["supported"] == ["DAILY_BARS"]
    for capability in (
        DataCapability.HISTORICAL_MINUTES,
        DataCapability.REALTIME_QUOTES,
        DataCapability.FINANCIAL_REPORTS,
        DataCapability.CORPORATE_ACTIONS,
        DataCapability.ANNOUNCEMENTS,
    ):
        assert not capabilities.supports(capability)


def test_fixture_capability_declaration_is_deterministic() -> None:
    first = _fixture().capabilities()
    second = _fixture().capabilities()

    assert first.declared_at == PHASE0_DECLARATION_AT
    assert first.declared_at.tzinfo is not None
    # Same provider + same declared_at + same supported set => same declaration id;
    # nothing reads the wall clock, so tests and audits are reproducible.
    assert first.declaration_id == second.declaration_id


def test_fixture_capability_declared_at_can_be_injected() -> None:
    capabilities = _fixture(declared_at=LATER).capabilities()

    assert capabilities.declared_at == LATER
    assert capabilities.declaration_id != _fixture().capabilities().declaration_id


def test_fixture_provider_rejects_naive_declared_at() -> None:
    with pytest.raises(ValidationError):
        _fixture(declared_at=datetime(2026, 11, 5))


# ---------------------------------------------------------------------------
# Disabled provider shells
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("provider_class", "provider_id"),
    [(TushareProvider, "tushare"), (AKShareProvider, "akshare")],
)
def test_disabled_shells_declare_no_capabilities(
    provider_class: type[TushareProvider] | type[AKShareProvider], provider_id: str
) -> None:
    capabilities = provider_class.capabilities()

    assert capabilities.provider_id == provider_id
    assert capabilities.supported == frozenset()
    assert capabilities.declaration_reason == "disabled Phase 0 shell; no data is supplied"
    assert len(capabilities.unsupported()) == len(DataCapability)
    with pytest.raises(UnsupportedEntityError):
        capabilities.require(DataCapability.DAILY_BARS)


@pytest.mark.parametrize("provider_class", [TushareProvider, AKShareProvider])
def test_disabled_shells_still_refuse_construction(
    provider_class: type[TushareProvider] | type[AKShareProvider],
) -> None:
    with pytest.raises(UnsupportedEntityError):
        provider_class()


def test_shell_declarations_are_stable() -> None:
    tushare = TushareProvider.capabilities()
    akshare = AKShareProvider.capabilities()

    assert tushare.declared_at == PHASE0_DECLARATION_AT
    assert akshare.declared_at == PHASE0_DECLARATION_AT
    assert TushareProvider.capabilities().declaration_id == tushare.declaration_id
    assert AKShareProvider.capabilities().declaration_id == akshare.declaration_id


# ---------------------------------------------------------------------------
# Fail-closed gate
# ---------------------------------------------------------------------------
def test_require_capability_delegates_to_declaration() -> None:
    fixture_capabilities = _fixture().capabilities()

    require_capability(fixture_capabilities, DataCapability.DAILY_BARS)

    with pytest.raises(UnsupportedEntityError):
        require_capability(fixture_capabilities, DataCapability.REALTIME_QUOTES)
    with pytest.raises(UnsupportedEntityError):
        require_capability(TushareProvider.capabilities(), DataCapability.DAILY_BARS)


# ---------------------------------------------------------------------------
# Capability-change audit
# ---------------------------------------------------------------------------
def test_capability_change_reports_added_and_removed() -> None:
    previous = ProviderCapabilities(
        provider_id="fixture",
        supported=frozenset({DataCapability.DAILY_BARS, DataCapability.FINANCIAL_REPORTS}),
        declared_at=PHASE0_DECLARATION_AT,
        declaration_reason="initial declaration",
    )
    current = ProviderCapabilities(
        provider_id="fixture",
        supported=frozenset({DataCapability.DAILY_BARS, DataCapability.CORPORATE_ACTIONS}),
        declared_at=LATER,
        declaration_reason="revised declaration",
    )

    change = capability_change(previous, current)

    assert change.provider_id == "fixture"
    assert change.added == (DataCapability.CORPORATE_ACTIONS,)
    assert change.removed == (DataCapability.FINANCIAL_REPORTS,)
    assert change.previous_declared_at == PHASE0_DECLARATION_AT
    assert change.current_declared_at == LATER
    assert change.is_change
    # Deterministic: the same inputs always yield the same change id.
    assert change.change_id == capability_change(previous, current).change_id
    assert capability_change(previous, previous).is_change is False


def test_capability_change_from_no_previous_declaration() -> None:
    first = ProviderCapabilities(
        provider_id="tushare",
        supported=frozenset({DataCapability.DAILY_BARS}),
        declared_at=PHASE0_DECLARATION_AT,
    )

    change = capability_change(None, first)

    assert change.previous_declared_at is None
    assert change.added == (DataCapability.DAILY_BARS,)
    assert change.removed == ()


# ---------------------------------------------------------------------------
# Provider independence of the contract
# ---------------------------------------------------------------------------
def test_capability_contract_is_provider_independent() -> None:
    left = ProviderCapabilities(
        provider_id="fixture",
        supported=frozenset({DataCapability.DAILY_BARS}),
        declared_at=PHASE0_DECLARATION_AT,
    )
    right = ProviderCapabilities(
        provider_id="tushare",
        supported=frozenset({DataCapability.DAILY_BARS}),
        declared_at=PHASE0_DECLARATION_AT,
    )

    # The *contract* is identical regardless of who implements it...
    assert left.supported == right.supported
    assert left.supports(DataCapability.DAILY_BARS) == right.supports(DataCapability.DAILY_BARS)
    # ...while the declarations remain separately identifiable.
    assert left.declaration_id != right.declaration_id


# ---------------------------------------------------------------------------
# Layer guard: no provider SDK may leak into janus/domain
# ---------------------------------------------------------------------------
#: Files that legitimately *mention* a provider name as data or documentation:
#: the ``QuoteSource`` labels are stored as data, the data-foundation package
#: docstring says the package knows no provider, and the proposal validator
#: screens untrusted text for provider token names.
KNOWN_PROVIDER_NAME_MENTION_FILES = frozenset(
    {
        "janus/domain/common/enums.py",
        "janus/domain/data_foundation/__init__.py",
        "janus/domain/strategy/proposal.py",
    }
)


def test_domain_imports_no_provider_sdk_module() -> None:
    """No ``janus/domain`` module may import a provider SDK (ADR-0001).

    The import-graph guard for frameworks also lives in
    ``tests/unit/test_layering.py``; this keeps the provider-SDK case next to the
    capability contract it protects.
    """
    offenders: list[str] = []
    for path in sorted(DOMAIN_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            for module in modules:
                if module.split(".")[0].lower() in PROVIDER_SDK_MODULES:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {module}")
    assert offenders == []


def test_domain_has_no_dynamic_provider_sdk_import() -> None:
    """A string-based ``import_module('tushare')`` would bypass the AST guard."""
    offenders: list[str] = []
    for path in sorted(DOMAIN_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for module in sorted(PROVIDER_SDK_MODULES):
            for token in (f'"{module}"', f"'{module}'"):
                if token in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {token}")
    assert offenders == []


def test_provider_name_mentions_are_confined_to_known_data_and_doc_places() -> None:
    """New provider-name mentions in the domain must be reviewed deliberately.

    The domain already contains three deliberate, non-SDK mentions (see
    ``KNOWN_PROVIDER_NAME_MENTION_FILES``). A *literal* "no mentions anywhere"
    assertion would be false today and would force edits to Lead-owned domain
    files; the real invariant is that no provider SDK is imported or referenced
    as code, and that any further textual mention is a conscious change.
    """
    offenders: list[str] = []
    for path in sorted(DOMAIN_ROOT.rglob("*.py")):
        lowered = path.read_text(encoding="utf-8").lower()
        if not any(module in lowered for module in PROVIDER_SDK_MODULES):
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative not in KNOWN_PROVIDER_NAME_MENTION_FILES:
            offenders.append(relative)
    assert offenders == []
