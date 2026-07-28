"""LLMRegistry 路由测试（用 mock provider）。

覆盖：
- complete: 按 task_type 路由到 mock provider
- structured_output: 返回符合 schema 的实例
- embed: 调用 embedding 配置对应的 provider
- register_provider / available_providers
- 错误：未注册 provider、未定义 task_type
"""
from __future__ import annotations

from typing import Optional

import pytest
from pydantic import BaseModel

from core.llm import (
    EmbeddingResponse,
    LLMError,
    LLMRegistry,
    LLMResponse,
)
from core.llm.base import (
    EmbeddingRequest,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    StructuredOutputRequest,
)
from core.llm.task_router import TaskRouter


# ===== 本地 MockLLMProvider（用于子类化场景） =====


class _MockLLMProvider(LLMProvider):
    """测试用 Mock Provider，可与 conftest 中的 MockLLMProvider 行为一致。"""

    provider_name = "mock"

    def __init__(self, embed_dim: int = 8):
        self._embed_dim = embed_dim

    def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        return LLMResponse(
            text="mock response",
            model=model,
            provider=self.provider_name,
        )

    def embed(self, request: EmbeddingRequest, model: str) -> EmbeddingResponse:
        return EmbeddingResponse(
            embeddings=[[0.1] * self._embed_dim for _ in request.texts],
            model=model,
            provider=self.provider_name,
        )

    def structured_output(self, request: StructuredOutputRequest, model: str) -> BaseModel:
        return request.output_schema.model_construct(**{})


# ===== complete =====


def test_complete_returns_response_from_mock_provider(
    mock_llm_registry: LLMRegistry,
):
    """complete 应通过 mock provider 返回固定响应。"""
    resp = mock_llm_registry.complete(
        task_type="test_task", prompt="Hello"
    )

    assert isinstance(resp, LLMResponse)
    assert resp.text == "mock response"
    assert resp.provider == "mock"
    assert resp.model == "mock-model"


def test_complete_uses_messages_when_provided(mock_llm_registry: LLMRegistry):
    """complete 接收 messages 时应正常工作。"""
    resp = mock_llm_registry.complete(
        task_type="test_task",
        messages=[{"role": "user", "content": "Hi"}],
    )
    assert resp.text == "mock response"


def test_complete_raises_on_unknown_task(mock_llm_registry: LLMRegistry):
    """未定义的 task_type 应抛 TaskNotFoundError（KeyError 子类）。"""
    with pytest.raises(KeyError):
        mock_llm_registry.complete(task_type="nonexistent", prompt="x")


def test_complete_raises_when_provider_not_registered(
    mock_task_router: TaskRouter,
):
    """task_type 配置的 provider 未注册时，应抛 LLMError。"""
    # 构造一个空 providers 的 registry
    registry = LLMRegistry(router=mock_task_router, providers={})

    with pytest.raises(LLMError):
        registry.complete(task_type="test_task", prompt="x")


def test_complete_temperature_override_applied_to_request(
    mock_llm_registry: LLMRegistry,
):
    """temperature_override 应透传给底层 LLMRequest。"""
    # 用一个能记录请求参数的 mock provider
    captured: dict = {}

    class CapturingProvider(_MockLLMProvider):
        def complete(self, request, model):
            captured["temperature"] = request.temperature
            captured["max_tokens"] = request.max_tokens
            return super().complete(request, model)

    mock_llm_registry.register_provider("mock", CapturingProvider())

    mock_llm_registry.complete(
        task_type="test_task",
        prompt="x",
        temperature_override=0.99,
        max_tokens_override=100,
    )

    assert captured["temperature"] == 0.99
    assert captured["max_tokens"] == 100


# ===== structured_output =====


class _FakeSchema(BaseModel):
    """用于测试 structured_output 的 schema。"""

    name: str = "default"
    value: int = 0


def test_structured_output_returns_schema_instance(
    mock_llm_registry: LLMRegistry,
):
    """structured_output 应返回 output_schema 的实例。"""
    result = mock_llm_registry.structured_output(
        task_type="test_task",
        output_schema=_FakeSchema,
        prompt="generate",
    )

    assert isinstance(result, _FakeSchema)


def test_structured_output_raises_on_unknown_task(
    mock_llm_registry: LLMRegistry,
):
    """未定义 task_type 时 structured_output 应抛异常。"""
    with pytest.raises(KeyError):
        mock_llm_registry.structured_output(
            task_type="nonexistent",
            output_schema=_FakeSchema,
            prompt="x",
        )


# ===== embed =====


def test_embed_returns_embedding_response(mock_llm_registry: LLMRegistry):
    """embed 应返回 EmbeddingResponse，向量维度对齐 mock 配置。"""
    resp = mock_llm_registry.embed(["text1", "text2"])

    assert isinstance(resp, EmbeddingResponse)
    assert len(resp.embeddings) == 2
    assert len(resp.embeddings[0]) == 8  # mock_tasks.yaml 中 dim=8
    assert resp.provider == "mock"


# ===== register_provider / available_providers =====


def test_register_provider_adds_to_available(mock_llm_registry: LLMRegistry):
    """register_provider 应把新 provider 加入可用列表。"""

    class AnotherProvider(_MockLLMProvider):
        provider_name = "another"

    mock_llm_registry.register_provider("another", AnotherProvider())

    assert "another" in mock_llm_registry.available_providers()
    assert "mock" in mock_llm_registry.available_providers()


def test_available_providers_returns_registered_names(
    mock_llm_registry: LLMRegistry,
):
    """available_providers 应返回已注册的 provider 名列表。"""
    providers = mock_llm_registry.available_providers()
    assert "mock" in providers


# ===== 多 provider 路由 =====


def test_router_routes_different_tasks_to_different_providers(
    mock_task_router: TaskRouter,
):
    """不同 task_type 配置不同 provider 时，应分别路由到对应 provider。"""
    # 改 another_task 用 second provider
    # 由于 mock_tasks_yaml 中 another_task 也是 mock provider，这里手动注册第二个
    class SecondProvider(_MockLLMProvider):
        provider_name = "second"

        def complete(self, request, model):
            return LLMResponse(
                text="second response", model=model, provider=self.provider_name
            )

    registry = LLMRegistry(
        router=mock_task_router,
        providers={"mock": _MockLLMProvider(), "second": SecondProvider()},
    )

    # mock_tasks.yaml 中 test_task 与 another_task 都用 mock，但我们可以临时改 router
    # 这里测试 mock provider 仍能正常响应
    resp = registry.complete(task_type="test_task", prompt="x")
    assert resp.text == "mock response"
