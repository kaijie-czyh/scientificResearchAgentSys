"""LLM Provider 抽象基类与请求/响应模型。

设计原则：
- 接口与具体 provider 解耦，stages 中 Agent 只认 task_type
- 所有 provider 必须实现 complete / embed / structured_output
- 多轮对话通过 messages 传递，complete 是 chat 的特例
- 结构化输出基于 Pydantic 模型，provider 负责保证返回符合 schema
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel


T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """LLM 调用错误。"""


@dataclass
class LLMRequest:
    """LLM 调用请求。"""

    # 单轮用 prompt，多轮用 messages
    prompt: Optional[str] = None
    messages: Optional[list[dict[str, str]]] = None
    system: Optional[str] = None  # 系统提示
    temperature: float = 0.2
    max_tokens: int = 2048
    stop: Optional[list[str]] = None
    # 透传给 provider 的额外参数
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.prompt is None and self.messages is None:
            raise LLMError("LLMRequest 必须提供 prompt 或 messages")


@dataclass
class LLMResponse:
    """LLM 调用响应。"""

    text: str
    model: str  # 实际使用的模型名
    provider: str  # provider 名
    # token 用量（用于成本追踪）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # 原始响应（调试用）
    raw: Any = None


@dataclass
class EmbeddingRequest:
    """嵌入请求。"""

    texts: list[str]
    # 透传参数
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmbeddingResponse:
    """嵌入响应。"""

    embeddings: list[list[float]]
    model: str
    provider: str
    total_tokens: int = 0


@dataclass
class StructuredOutputRequest:
    """结构化输出请求。

    provider 应保证返回符合 schema 的对象。
    实现方式因 provider 而异：
    - OpenAI: response_format=json_schema + Pydantic 解析
    - Anthropic: tool use + Pydantic 解析
    - Local: prompt 引导 + Pydantic 解析（容错）
    """

    output_schema: Type[BaseModel]  # 期望输出的 Pydantic 模型类
    prompt: Optional[str] = None
    messages: Optional[list[dict[str, str]]] = None
    system: Optional[str] = None
    temperature: float = 0.0  # 结构化输出默认低温
    max_tokens: int = 4096
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.prompt is None and self.messages is None:
            raise LLMError("StructuredOutputRequest 必须提供 prompt 或 messages")


class LLMProvider(abc.ABC):
    """LLM Provider 抽象基类。

    所有 provider 必须实现 complete / embed / structured_output。
    chat 通过 messages 传给 complete 实现。
    """

    provider_name: str = "abstract"

    @abc.abstractmethod
    def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        """文本补全/对话。"""

    @abc.abstractmethod
    def embed(self, request: EmbeddingRequest, model: str) -> EmbeddingResponse:
        """文本嵌入。"""

    @abc.abstractmethod
    def structured_output(
        self, request: StructuredOutputRequest, model: str
    ) -> BaseModel:
        """结构化输出，返回符合 output_schema 的 Pydantic 实例。"""

    # 工具方法

    def _build_messages(self, request: LLMRequest) -> list[dict[str, str]]:
        """把 LLMRequest 转为 messages 列表。"""
        if request.messages is not None:
            msgs = list(request.messages)
        else:
            msgs = [{"role": "user", "content": request.prompt or ""}]
        if request.system:
            msgs = [{"role": "system", "content": request.system}] + msgs
        return msgs
