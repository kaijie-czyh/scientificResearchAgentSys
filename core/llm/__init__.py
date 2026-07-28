"""LLM 适配层。

按 task_type 路由到不同 provider/model，统一接口：
- complete: 单轮补全
- chat: 多轮对话
- embed: 文本嵌入
- structured_output: 结构化输出（基于 JSON Schema / Pydantic）

Provider 实现：
- OpenAIProvider: GPT 系列（含兼容协议的本地模型）
- AnthropicProvider: Claude 系列
- LocalProvider: 兼容 OpenAI 协议的本地部署模型

凭据从环境变量读取，不写入配置文件。
"""
from core.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    StructuredOutputRequest,
    LLMError,
)
from core.llm.task_router import TaskRouter, TaskConfig, TaskNotFoundError
from core.llm.registry import LLMRegistry

__all__ = [
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "StructuredOutputRequest",
    "LLMError",
    "TaskRouter",
    "TaskConfig",
    "TaskNotFoundError",
    "LLMRegistry",
]
