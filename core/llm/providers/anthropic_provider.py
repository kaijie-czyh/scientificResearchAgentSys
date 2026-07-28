"""Anthropic Claude Provider。

结构化输出实现：
- 通过 tool use 强制 Claude 返回结构化数据
- tool 的 input_schema 即 Pydantic 模型的 JSON Schema
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


class AnthropicProvider(LLMProvider):
    """Anthropic Claude Provider。"""

    provider_name = "anthropic"

    def __init__(
        self,
        api_key: str,
        timeout: int = 120,
        max_retries: int = 3,
    ):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "未安装 anthropic 包，请运行: pip install anthropic"
            ) from e

        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

    def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        # Anthropic 用 system 单独参数
        system = request.system or ""
        if request.messages is not None:
            messages = list(request.messages)
        else:
            messages = [{"role": "user", "content": request.prompt or ""}]

        try:
            resp = self._client.messages.create(
                model=model,
                system=system,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                stop_sequences=request.stop or None,
            )
        except Exception as e:
            raise LLMError(f"Anthropic complete 调用失败: {e}") from e

        # 提取文本
        text_parts: list[str] = []
        for block in resp.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        text = "".join(text_parts)

        usage = resp.usage
        return LLMResponse(
            text=text,
            model=model,
            provider=self.provider_name,
            prompt_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            total_tokens=(
                (getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0))
                if usage else 0
            ),
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    def embed(self, request: EmbeddingRequest, model: str) -> EmbeddingResponse:
        # Anthropic 暂不提供嵌入 API，回退到 OpenAI 兼容协议提示用户
        raise LLMError(
            "Anthropic 暂不提供嵌入 API，请在 tasks.yaml 中将 embedding.provider "
            "设为 openai 或 local"
        )

    def structured_output(
        self, request: StructuredOutputRequest, model: str
    ) -> BaseModel:
        schema = request.output_schema
        schema_json = schema.model_json_schema()

        # 用 tool use 强制结构化输出
        tool_name = "return_structured"
        tool_def = {
            "name": tool_name,
            "description": "返回结构化输出",
            "input_schema": schema_json,
        }

        if request.messages is not None:
            messages = list(request.messages)
        else:
            messages = [{"role": "user", "content": request.prompt or ""}]

        try:
            resp = self._client.messages.create(
                model=model,
                system=request.system or "",
                messages=messages,
                tools=[tool_def],
                tool_choice={"type": "tool", "name": tool_name},
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
        except Exception as e:
            raise LLMError(f"Anthropic structured_output 调用失败: {e}") from e

        # 从 tool_use block 提取
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                try:
                    return schema.model_validate(block.input)
                except ValidationError as e:
                    raise LLMError(
                        f"Anthropic tool_use 返回不符合 schema: {e}\n"
                        f"原始 input: {json.dumps(block.input, ensure_ascii=False)[:500]}"
                    ) from e

        raise LLMError(
            f"Anthropic 未返回 tool_use 块，原始返回: {resp.model_dump() if hasattr(resp, 'model_dump') else resp}"
        )
