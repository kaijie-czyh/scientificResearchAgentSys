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
    strip_think_tags,
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
        # 剥离推理模型（MiniMax-M3 等）拼进 content 的 <think> 思考链，只留最终答复
        text = strip_think_tags(text)
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
        # 提取 JSON：处理 markdown 代码块包裹、前置说明文字等情况
        text = self._extract_json(text)

        # 检测 MiniMax 把 schema 定义本身当返回的常见错误模式：
        # 返回 {"$defs": ...} / {"$schema": ...} / {"properties": ..., "type": "object"}
        # 这种情况下重新提示 LLM 生成实例数据
        if self._looks_like_schema(text):
            raise LLMError(
                "模型返回了 schema 定义本身而非实例数据，请改用 complete 或调整 prompt"
            )

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

    @staticmethod
    def _looks_like_schema(text: str) -> bool:
        """检测返回是否是 schema 定义而非实例数据。

        MiniMax M3 偶尔会把输入的 JSON Schema 原样返回，常见特征：
        - 含 "$defs" / "$schema" / "$id" 顶层键
        - 含 "properties" + "type": "object" 且无实际业务字段
        """
        if not text:
            return False
        stripped = text.strip()
        if not stripped.startswith("{"):
            return False
        # 顶层 schema 标志键
        for key in ('"$defs"', '"$schema"', '"$id"', '"definitions"'):
            if key in stripped[:200]:
                return True
        return False

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 LLM 返回中提取 JSON 字符串。

        处理以下情况：
        1. 纯 JSON（直接返回）
        2. markdown 代码块包裹（```json ... ``` 或 ``` ... ```）
        3. 前置/后置说明文字 + JSON（提取第一个 { 到最后一个 } 之间的内容）
        """
        stripped = text.strip()

        # 情况 1：纯 JSON
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped

        # 情况 2：markdown 代码块包裹
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 2:
                body_lines = lines[1:]
                if body_lines and body_lines[-1].strip().startswith("```"):
                    body_lines = body_lines[:-1]
                inner = "\n".join(body_lines).strip()
                if inner.startswith("{") and inner.endswith("}"):
                    return inner

        # 情况 3：从文本中提取第一个 { 到最后一个 } 之间的内容
        first_brace = stripped.find("{")
        last_brace = stripped.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            return stripped[first_brace : last_brace + 1]

        # 兜底：返回原文让 json.loads 报错
        return stripped

    def _build_messages_for_structured(
        self,
        request: StructuredOutputRequest,
        schema: Type[BaseModel],
    ) -> list[dict[str, str]]:
        """构造结构化输出的 messages。

        把 JSON Schema 注入 system prompt，要求返回符合 schema 的 JSON 实例。
        关键：明确告诉模型「生成实例数据」而非「复述 schema 定义」，
        避免 MiniMax M3 把 $defs/properties 当返回的常见错误模式。
        """
        schema_str = json.dumps(
            schema.model_json_schema(), ensure_ascii=False, indent=2
        )
        # 用 schema 的字段名作为强提示，告诉模型要填什么
        field_hints: list[str] = []
        for name, info in schema.model_json_schema().get("properties", {}).items():
            desc = info.get("description", "")
            field_hints.append(f'- "{name}": {desc}')
        hints_text = "\n".join(field_hints) if field_hints else ""

        system = (
            request.system or ""
        ) + (
            "\n\n请基于用户请求生成**实例数据**（不是 schema 定义本身），"
            "返回一个符合以下 JSON Schema 的 JSON 对象。\n"
            "硬性要求：\n"
            "1. 仅返回 JSON 对象本身，不要包裹 markdown 代码块，不要任何解释文字\n"
            "2. 不要返回 $defs / $schema / properties / type 等 schema 元字段\n"
            "3. 必须为所有 required 字段填入真实业务数据（字符串/数字/数组等）\n"
            f"\n需要填充的字段：\n{hints_text}\n"
            f"\n参考 JSON Schema：\n{schema_str}"
        )

        if request.messages is not None:
            msgs = list(request.messages)
        else:
            msgs = [{"role": "user", "content": request.prompt or ""}]

        return [{"role": "system", "content": system}] + msgs
