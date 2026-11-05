"""DeepSeek-compatible LLM adapter shell.

Phase 0 spec s1.1/s3.3/s13: the LLM sits behind a provider adapter and is
**disabled by default**. There is no live model call anywhere in Phase 0; the
stub agent emits fixture proposals (P0-G). This module exists so the boundary is
defined and testable *without* a network:

* it refuses to construct unless ``config/providers.yaml`` has
  ``llm.enabled: true``;
* it refuses to construct when ``llm.execution_tools_enabled`` is true;
* the API key is read only from the environment variable **named** in
  configuration, is held in a private slot, and is never logged, persisted,
  serialised, or included in ``repr``/errors;
* it implements :class:`janus.application.ports.clock.LLMProvider`, returning raw
  text that the agent layer must still validate against a strict schema.

No Phase 0 test may reach the network through this adapter.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from janus.application.ports.clock import LLMProvider
from janus.domain.common.errors import ConfigurationError, UnsupportedEntityError
from janus.infrastructure.config.loader import LLMConfig, load_app_config

if TYPE_CHECKING:
    import httpx

__all__ = ["DEFAULT_BASE_URL", "DeepSeekAdapter", "build_deepseek_adapter"]

DEFAULT_BASE_URL: Final = "https://api.deepseek.com"
DEFAULT_TIMEOUT_SECONDS: Final = 30.0


class DeepSeekAdapter(LLMProvider):
    """Disabled-by-default DeepSeek-compatible chat-completion adapter.

    Construct through :meth:`from_config` so the ``providers.yaml`` gate and the
    environment-variable indirection are always applied.
    """

    __slots__ = ("_api_key", "_base_url", "_client", "_config", "_timeout_seconds")

    def __init__(
        self,
        config: LLMConfig,
        *,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not config.enabled:
            raise UnsupportedEntityError(
                "DeepSeek LLM is disabled by configuration "
                "(providers.yaml: llm.enabled=false); Phase 0 makes no live "
                "model calls",
                provider=config.provider,
            )
        if config.execution_tools_enabled:
            raise UnsupportedEntityError(
                "Phase 0 forbids granting execution tools to a model",
                provider=config.provider,
            )
        api_key = os.environ.get(config.api_key_env, "").strip()
        if not api_key:
            raise ConfigurationError(
                "LLM is enabled but the configured API-key environment variable "
                f"{config.api_key_env!r} is unset or empty",
                env_var=config.api_key_env,
            )
        self._config = config
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client

    @classmethod
    def from_config(
        cls,
        config_dir: Path | str | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> DeepSeekAdapter:
        """Load ``providers.yaml`` (fail closed) and construct the adapter."""
        app_config = load_app_config(config_dir)
        return cls(app_config.providers.llm, client=client)

    # -- LLMProvider port -------------------------------------------------
    @property
    def provider_id(self) -> str:
        return self._config.provider

    @property
    def model_id(self) -> str:
        return self._config.model_id

    def complete(self, *, system_prompt: str, user_content: str) -> str:
        """Call the chat-completion endpoint and return raw model text.

        Network access only happens when this method is invoked, which Phase 0
        production code does not do (the agent is a deterministic stub). The
        returned text is untrusted and must be schema-validated by the caller.
        """
        import httpx  # local import keeps the module importable without httpx

        payload: dict[str, Any] = {
            "model": self._config.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "temperature": 0.0,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.Client(timeout=self._timeout_seconds)
        try:
            response = client.post(
                f"{self._base_url}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
            data: Any = response.json()
        except httpx.HTTPError as exc:
            raise ConfigurationError(
                "LLM request failed",
                provider=self.provider_id,
                error=type(exc).__name__,
            ) from exc
        finally:
            if owns_client:
                client.close()
        return _extract_content(data, provider_id=self.provider_id)

    def __repr__(self) -> str:  # never include the API key
        return (
            f"DeepSeekAdapter(provider={self.provider_id!r}, model={self.model_id!r}, enabled=True)"
        )


def _extract_content(data: Any, *, provider_id: str) -> str:
    """Pull the text out of a chat-completion payload, failing closed."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ConfigurationError(
            "LLM response did not match the expected chat-completion schema",
            provider=provider_id,
        ) from exc
    if not isinstance(content, str):
        raise ConfigurationError("LLM response content was not text", provider=provider_id)
    return content


def build_deepseek_adapter(config_dir: Path | str | None = None) -> LLMProvider:
    """Build the adapter from configuration, refusing when the LLM is disabled."""
    return DeepSeekAdapter.from_config(config_dir)
