"""LLM provider adapters.

Phase 0 ships only a disabled shell: no live model call is made anywhere in the
tested system. Importing this package does not read credentials or contact a
provider.
"""

from __future__ import annotations

from janus.infrastructure.llm.deepseek_adapter import (
    DEFAULT_BASE_URL,
    DeepSeekAdapter,
    build_deepseek_adapter,
)

__all__ = ["DEFAULT_BASE_URL", "DeepSeekAdapter", "build_deepseek_adapter"]
