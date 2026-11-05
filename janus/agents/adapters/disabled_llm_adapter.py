"""Disabled live-LLM adapter shell (``config/providers.yaml``: ``llm.enabled: false``).

Phase 0 makes **no live model calls** (spec s1.3, s9, P0-G). The composition root
still needs an ``LLMProvider``-shaped object so the wiring is explicit; this
adapter supplies it and fails closed on every use.

Security properties:

* it never reads, stores or logs an API key -- credentials live outside the
  repository and are never passed to an agent;
* it exposes no tool/execution surface;
* constructing it with ``enabled=True`` is refused, so a configuration mistake
  cannot silently switch on a live model.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml

from janus.domain.common.errors import ConfigurationError, UnsupportedEntityError

__all__ = [
    "DISABLED_LLM_REASON",
    "DisabledLLMAdapter",
    "load_providers_config_file",
]

DISABLED_LLM_REASON: Final[str] = (
    "LLM calls are disabled in Phase 0 (config/providers.yaml sets llm.enabled=false); "
    "the deterministic FixtureAgent stub is used instead"
)


def load_providers_config_file(path: Path | str) -> Mapping[str, Any]:
    """Read a providers YAML file and return its ``llm`` section as a mapping.

    Only structure is inspected; environment-variable *names* are never resolved
    and no secret value is read (spec s9.6).
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigurationError("providers configuration file is missing", path=str(config_path))
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            "providers configuration is not valid YAML", path=str(config_path), error=str(exc)
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigurationError(
            "providers configuration root must be a mapping", path=str(config_path)
        )
    llm = raw.get("llm", {})
    if not isinstance(llm, Mapping):
        raise ConfigurationError(
            "providers configuration 'llm' section must be a mapping",
            path=str(config_path),
        )
    return llm


class DisabledLLMAdapter:
    """``LLMProvider``-shaped object that refuses every completion request."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        provider: str = "deepseek_compatible",
        model_id: str = "deepseek-chat",
        prompt_version: str = "research_agent_v0_1_0",
        execution_tools_enabled: bool = False,
    ) -> None:
        if enabled:
            raise ConfigurationError(
                "live LLM calls are not implemented in Phase 0; "
                "config/providers.yaml must keep llm.enabled=false",
                provider=provider,
            )
        if execution_tools_enabled:
            raise UnsupportedEntityError(
                "Phase 0 forbids granting execution tools to a model",
                provider=provider,
            )
        self._provider: str = provider
        self._model_id: str = model_id
        self._prompt_version: str = prompt_version

    # -- LLMProvider surface --------------------------------------------
    @property
    def provider_id(self) -> str:
        return self._provider

    @property
    def model_id(self) -> str:
        """Configured model identifier (never called in Phase 0)."""
        return self._model_id

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    @property
    def enabled(self) -> bool:
        return False

    def complete(self, *, system_prompt: str, user_content: str) -> str:
        """Always refuse: Phase 0 has no live model dependency."""
        raise UnsupportedEntityError(
            DISABLED_LLM_REASON,
            provider=self._provider,
            model_id=self._model_id,
        )

    # -- construction helpers -------------------------------------------
    @classmethod
    def from_llm_config_mapping(cls, llm_config: Mapping[str, Any] | None) -> DisabledLLMAdapter:
        """Build from the ``llm:`` mapping of ``config/providers.yaml``."""
        config = dict(llm_config or {})
        return cls(
            enabled=bool(config.get("enabled", False)),
            provider=str(config.get("provider", "deepseek_compatible")),
            model_id=str(config.get("model_id", "deepseek-chat")),
            prompt_version=str(config.get("prompt_version", "research_agent_v0_1_0")),
            execution_tools_enabled=bool(config.get("execution_tools_enabled", False)),
        )

    @classmethod
    def from_config_file(cls, path: Path | str) -> DisabledLLMAdapter:
        """Build from a full ``providers.yaml`` file path."""
        return cls.from_llm_config_mapping(load_providers_config_file(path))
