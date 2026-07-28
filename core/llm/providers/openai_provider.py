"""OpenAI 兼容 Provider。

支持：
- OpenAI 官方 API（GPT 系列）
- 兼容 OpenAI 协议的本地部署模型（vLLM, Ollama, LM Studio 等）

结构化输出实现：
- 优先使用 response_format=json_schema（GPT-4o 及兼容服务支持）
- 退化为 response_format=json_object + prompt 引导
- 最终用 Pydantic 校验，失败则抛 LLMError
"""
from __future__ import annotations

import json
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from core.llm.base import (
    EmbeddingRequest,
    EmbeddingResponse,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    StructuredOutputRequest,
)

T = TypeVar("T", bound=BaseModel)


class OpenAIProvider(LLMProvider):
    """OpenAI 兼容 Provider。"""

    provider_name = "openai"

    def __init__(
        self,
        api_key: Optional[str],
        base_url: Optional[str] = None,
        timeout: int = 120,
        max_retries: int = 3,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "未安装 openai 包，请运行: pip install openai"
            ) from e

        kwargs: dict[str, Any] = {"timeout": timeout, "max_retries": max_retries}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self._base_url = base_url

    def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        messages = self._build_messages(request)
        try:
            resp = self._client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                stop=request.stop,
            )
        except Exception as e:
            raise LLMError(f"OpenAI complete 调用失败: {e}") from e

        text = resp.choices[0].message.content or ""
        usage = resp.usage
        return LLMResponse(
            text=text,
            model=model,
            provider=self.provider_name,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            total_tokens=getattr(usage, "total_tokens", 0) if usage else 0,
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    def embed(self, request: EmbeddingRequest, model: str) -> EmbeddingResponse:
        try:
            resp = self._client.embeddings.create(
                model=model,
                input=request.texts,
            )
        except Exception as e:
            raise LLMError(f"OpenAI embed 调用失败: {e}") from e

        embeddings = [d.embedding for d in resp.data]
        usage = resp.usage
        return EmbeddingResponse(
            embeddings=embeddings,
            model=model,
            provider=self.provider_name,
            total_tokens=getattr(usage, "total_tokens", 0) if usage else 0,
        )

    def structured_output(
        self, request: StructuredOutputRequest, model: str
    ) -> BaseModel:
        schema = request.output_schema
        schema_json = schema.model_json_schema()

        messages = self._build_messages_for_structured(request, schema)

        try:
            resp = self._client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            raise LLMError(f"OpenAI structured_output 调用失败: {e}") from e

        text = resp.choices[0].message.content or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"结构化输出返回非合法 JSON: {e}\n原始返回:\n{text[:500]}"
            ) from e

        try:
            return schema.model_validate(data)
        except ValidationError as e:
            raise LLMError(
                f"结构化输出不符合 schema: {e}\n原始返回:\n{text[:500]}"
            ) from e

    def _build_messages_for_structured(
        self,
        request: StructuredOutputRequest,
        schema: Type[BaseModel],
    ) -> list[dict[str, str]]:
        """构造结构化输出的 messages。

        把 JSON Schema 注入 system prompt，要求返回符合 schema 的 JSON。
        """
        schema_str = json.dumps(
            schema.model_json_schema(), ensure_ascii=False, indent=2
        )
        system = (
            request.system or ""
        ) + (
            "\n\n你必须返回符合以下 JSON Schema 的 JSON 对象，且仅返回 JSON（不要包裹 markdown 代码块）：\n"
            f"{schema_str}"
        )

        if request.messages is not None:
            msgs = list(request.messages)
        else:
            msgs = [{"role": "user", "content": request.prompt or ""}]

        return [{"role": "system", "content": system}] + msgs
