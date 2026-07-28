"""LLM Provider 实现集合。

- openai_provider: 兼容 OpenAI 协议（含本地部署模型）
- anthropic_provider: Claude 系列
- local_provider: 别名，实际复用 openai_provider
"""

from core.llm.providers.openai_provider import OpenAIProvider
from core.llm.providers.anthropic_provider import AnthropicProvider

__all__ = ["OpenAIProvider", "AnthropicProvider"]
