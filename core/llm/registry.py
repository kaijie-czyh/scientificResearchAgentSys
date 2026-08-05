"""LLM Provider 注册表与统一调用入口。

使用范式：
    registry = LLMRegistry.from_config(get_config())
    # 按 task_type 调用
    resp = registry.complete("paper_metadata_extract", prompt="...")
    obj = registry.structured_output("design_claim_extract", output_schema=..., prompt="...")
    emb = registry.embed(["text1", "text2"])

注册表负责：
- 按 task_type 路由到 provider+model
- 应用 task 配置中的 temperature/max_tokens
- 复用 provider 实例（避免重复初始化客户端）
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from core.config import GlobalConfig, get_config
from core.llm.base import (
    EmbeddingRequest,
    EmbeddingResponse,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    StructuredOutputRequest,
)
from core.llm.task_router import TaskRouter

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class LLMRegistry:
    """LLM 统一调用入口。

    内部维护 provider 实例池（按 provider 名缓存）。
    """

    def __init__(
        self,
        router: TaskRouter,
        providers: dict[str, LLMProvider],
    ):
        self._router = router
        self._providers = providers

    @classmethod
    def from_config(cls, config: Optional[GlobalConfig] = None) -> "LLMRegistry":
        """从全局配置构造。

        懒加载 provider：先注入可用的 provider，缺失凭据的 provider 不注册。
        """
        config = config or get_config()
        router = TaskRouter.load(config.llm.tasks_config_path)

        providers: dict[str, LLMProvider] = {}

        # 尝试注册 OpenAI（含兼容协议的本地模型）
        openai_key = os.environ.get(config.llm.openai_api_key_env)
        local_url = os.environ.get(config.llm.local_model_base_url_env)
        if openai_key or local_url:
            from core.llm.providers.openai_provider import OpenAIProvider

            providers["openai"] = OpenAIProvider(
                api_key=openai_key,
                base_url=None,  # OpenAI 官方
                timeout=config.llm.default_timeout_seconds,
                max_retries=config.llm.max_retries,
            )

        # local provider（兼容 OpenAI 协议的本地服务）
        if local_url:
            from core.llm.providers.openai_provider import OpenAIProvider

            providers["local"] = OpenAIProvider(
                api_key=openai_key or "EMPTY",
                base_url=local_url,
                timeout=config.llm.default_timeout_seconds,
                max_retries=config.llm.max_retries,
            )

        # Anthropic
        anthropic_key = os.environ.get(config.llm.anthropic_api_key_env)
        if anthropic_key:
            from core.llm.providers.anthropic_provider import AnthropicProvider

            providers["anthropic"] = AnthropicProvider(
                api_key=anthropic_key,
                timeout=config.llm.default_timeout_seconds,
                max_retries=config.llm.max_retries,
            )

        # ===== MiniMax / MiMo / DeepSeek =====
        # 三家均兼容 OpenAI 协议，复用 OpenAIProvider，通过 base_url + api_key 区分。
        # 只有 api_key 存在才注册（dry_run 模式下无 key 则跳过，节点用占位数据不依赖 provider）。
        from core.llm.providers.openai_provider import OpenAIProvider as _OAIProvider

        # MiniMax Token Plan（兼容 OpenAI 协议，用 sk-cp key）
        minimax_key = os.environ.get(config.llm.minimax_api_key_env)
        minimax_url = os.environ.get(config.llm.minimax_base_url_env) or config.llm.minimax_base_url_default
        if minimax_key:
            providers["minimax"] = _OAIProvider(
                api_key=minimax_key,
                base_url=minimax_url,
                timeout=config.llm.default_timeout_seconds,
                max_retries=config.llm.max_retries,
            )

        # 小米 MiMo（兼容 OpenAI 协议）
        mimo_key = os.environ.get(config.llm.mimo_api_key_env)
        mimo_url = os.environ.get(config.llm.mimo_base_url_env) or config.llm.mimo_base_url_default
        if mimo_key:
            providers["mimo"] = _OAIProvider(
                api_key=mimo_key,
                base_url=mimo_url,
                timeout=config.llm.default_timeout_seconds,
                max_retries=config.llm.max_retries,
            )

        # DeepSeek（兼容 OpenAI 协议）
        deepseek_key = os.environ.get(config.llm.deepseek_api_key_env)
        deepseek_url = os.environ.get(config.llm.deepseek_base_url_env) or config.llm.deepseek_base_url_default
        if deepseek_key:
            providers["deepseek"] = _OAIProvider(
                api_key=deepseek_key,
                base_url=deepseek_url,
                timeout=config.llm.default_timeout_seconds,
                max_retries=config.llm.max_retries,
            )

        return cls(router=router, providers=providers)

    # ===== 统一调用接口 =====

    def complete(
        self,
        task_type: str,
        prompt: Optional[str] = None,
        messages: Optional[list[dict[str, str]]] = None,
        system: Optional[str] = None,
        temperature_override: Optional[float] = None,
        max_tokens_override: Optional[int] = None,
    ) -> LLMResponse:
        """按 task_type 调用 LLM 补全/对话。"""
        cfg = self._router.get(task_type)
        provider = self._get_provider(cfg.provider)
        request = LLMRequest(
            prompt=prompt,
            messages=messages,
            system=system,
            temperature=temperature_override if temperature_override is not None else cfg.temperature,
            max_tokens=max_tokens_override or cfg.max_tokens,
        )
        return provider.complete(request, model=cfg.model)

    def structured_output(
        self,
        task_type: str,
        output_schema: Type[T],
        prompt: Optional[str] = None,
        messages: Optional[list[dict[str, str]]] = None,
        system: Optional[str] = None,
        temperature_override: Optional[float] = None,
    ) -> T:
        """按 task_type 调用 LLM 结构化输出。返回 Pydantic 实例。

        含一次重试：若首次返回 schema 定义本身（MiniMax M3 常见错误模式），
        自动追加「请返回实例数据，不要返回 schema 定义」强提示重试一次。
        """
        cfg = self._router.get(task_type)
        provider = self._get_provider(cfg.provider)
        request = StructuredOutputRequest(
            output_schema=output_schema,
            prompt=prompt,
            messages=messages,
            system=system,
            temperature=temperature_override if temperature_override is not None else cfg.temperature,
        )
        try:
            result = provider.structured_output(request, model=cfg.model)
            return self._coerce(result, output_schema)
        except LLMError as e:
            err_msg = str(e)
            # 仅当「返回 schema 定义」或「不符合 schema」时重试，其他错误（如网络）直接抛
            if "schema 定义本身" not in err_msg and "不符合 schema" not in err_msg:
                raise
            logger.warning(
                "structured_output 首次失败（%s），追加实例提示重试一次",
                err_msg[:120],
            )
            # 在 prompt 末尾追加强提示
            reminder = (
                "\n\n【重要】请直接返回符合 schema 的实例 JSON 数据，"
                "不要返回 $defs / properties / type 等 schema 元字段。"
                "例如若 schema 要求 {ideas: list[IdeaDraftItem]}，"
                "应返回 {\"ideas\": [{\"text\": \"具体思路描述\", ...}]}。"
            )
            if prompt is not None:
                request = StructuredOutputRequest(
                    output_schema=output_schema,
                    prompt=prompt + reminder,
                    system=system,
                    temperature=request.temperature,
                )
            elif messages is not None:
                msgs = list(messages)
                if msgs and msgs[-1].get("role") == "user":
                    msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"] + reminder}
                else:
                    msgs.append({"role": "user", "content": reminder})
                request = StructuredOutputRequest(
                    output_schema=output_schema,
                    messages=msgs,
                    system=system,
                    temperature=request.temperature,
                )
            result = provider.structured_output(request, model=cfg.model)
            return self._coerce(result, output_schema)

    @staticmethod
    def _coerce(result: Any, output_schema: Type[T]) -> T:
        if not isinstance(result, output_schema):
            if isinstance(result, dict):
                return output_schema.model_validate(result)
            raise LLMError(
                f"Provider 返回类型 {type(result)} 不符合 {output_schema}"
            )
        return result

    def embed(self, texts: list[str]) -> EmbeddingResponse:
        """调用嵌入模型。所有任务共用同一嵌入配置。"""
        emb_cfg = self._router.embedding_config()
        provider = self._get_provider(emb_cfg.provider)
        return provider.embed(EmbeddingRequest(texts=texts), model=emb_cfg.model)

    # ===== 注册管理 =====

    def register_provider(self, name: str, provider: LLMProvider) -> None:
        """手动注册 provider。用于测试或扩展。"""
        self._providers[name] = provider

    def available_providers(self) -> list[str]:
        return list(self._providers.keys())

    def _get_provider(self, name: str) -> LLMProvider:
        if name not in self._providers:
            raise LLMError(
                f"Provider {name} 未注册（可能缺少对应环境变量凭据），"
                f"当前可用 providers={list(self._providers.keys())}"
            )
        return self._providers[name]
