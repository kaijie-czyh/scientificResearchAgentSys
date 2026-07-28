"""pytest 全局 fixtures。

提供：
- tmp_project_paths: 基于 tmp_path 构造的 ProjectPaths
- knowledge_store: 基于 tmp_path 构造的 KnowledgeStore
- artifact_manager: 基于 knowledge_store 与 tmp_path 构造的 ArtifactManager
- mock_tasks_yaml: 临时最小 tasks.yaml
- mock_llm_provider: 实现 LLMProvider 的 fake 类
- mock_llm_registry: 用 mock_llm_provider 注册的 LLMRegistry
- fake_embedding_fn: 固定向量嵌入函数（用于向量库测试）

注意：所有 fixture 都基于 tmp_path，不污染真实文件系统。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Type, TypeVar

import pytest

# 把项目根目录加入 sys.path，确保 core.* / stages.* 可被直接 import
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.artifacts import ArtifactContentStore, ArtifactManager  # noqa: E402
from core.config import ProjectPaths  # noqa: E402
from core.knowledge import KnowledgeStore  # noqa: E402
from core.llm import (  # noqa: E402
    EmbeddingRequest,
    EmbeddingResponse,
    LLMProvider,
    LLMRegistry,
    LLMRequest,
    LLMResponse,
    StructuredOutputRequest,
)
from core.llm.task_router import EmbeddingConfig, TaskConfig, TaskRouter  # noqa: E402
from pydantic import BaseModel  # noqa: E402


T = TypeVar("T", bound=BaseModel)


# ===== 路径与存储 fixtures =====


@pytest.fixture
def tmp_project_paths(tmp_path: Path) -> ProjectPaths:
    """基于 tmp_path 构造 ProjectPaths，所有项目数据写入临时目录。"""
    paths = ProjectPaths.from_root(tmp_path)
    # 预创建 projects 目录，避免后续 Path 操作出错
    paths.projects.mkdir(parents=True, exist_ok=True)
    return paths


@pytest.fixture
def knowledge_store(tmp_path: Path) -> KnowledgeStore:
    """基于 tmp_path 构造独立的 KnowledgeStore（SQLite 文件在临时目录）。"""
    db_path = tmp_path / "knowledge.db"
    return KnowledgeStore(db_path=db_path)


@pytest.fixture
def artifact_content_store(tmp_path: Path) -> ArtifactContentStore:
    """基于 tmp_path 构造 ArtifactContentStore。"""
    return ArtifactContentStore(artifacts_dir=tmp_path / "artifacts")


@pytest.fixture
def artifact_manager(
    knowledge_store: KnowledgeStore, artifact_content_store: ArtifactContentStore
) -> ArtifactManager:
    """基于 knowledge_store 与临时 content_store 构造 ArtifactManager。"""
    return ArtifactManager(store=knowledge_store, content_store=artifact_content_store)


# ===== LLM mock fixtures =====


class MockLLMProvider(LLMProvider):
    """Mock LLM Provider。

    - complete: 固定返回 "mock response"
    - embed: 返回固定 8 维向量
    - structured_output: 用 model_construct 构造 schema 实例（跳过校验）
    """

    provider_name = "mock"

    def __init__(self, embed_dim: int = 8):
        self._embed_dim = embed_dim

    def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        return LLMResponse(
            text="mock response",
            model=model,
            provider=self.provider_name,
            prompt_tokens=1,
            completion_tokens=2,
            total_tokens=3,
        )

    def embed(self, request: EmbeddingRequest, model: str) -> EmbeddingResponse:
        # 每条文本返回一个固定向量（长度对齐 embed_dim）
        embeddings = [[0.1] * self._embed_dim for _ in request.texts]
        return EmbeddingResponse(
            embeddings=embeddings,
            model=model,
            provider=self.provider_name,
            total_tokens=len(request.texts),
        )

    def structured_output(self, request: StructuredOutputRequest, model: str) -> BaseModel:
        # 用 model_construct 跳过校验，构造一个 schema 实例
        # 子类可覆盖此方法以返回更真实的实例
        schema: Type[BaseModel] = request.output_schema
        return schema.model_construct(**{})


@pytest.fixture
def mock_llm_provider() -> MockLLMProvider:
    """返回 MockLLMProvider 实例。"""
    return MockLLMProvider(embed_dim=8)


@pytest.fixture
def mock_tasks_yaml(tmp_path: Path) -> Path:
    """在 tmp_path 下写一个最小 tasks.yaml，包含 test_task 与 embedding 配置。"""
    content = """
tasks:
  test_task:
    provider: mock
    model: mock-model
    temperature: 0.0
    description: "测试用 task"
  another_task:
    provider: mock
    model: mock-model-2
    temperature: 0.5

embedding:
  provider: mock
  model: mock-embed
  dim: 8
"""
    p = tmp_path / "tasks.yaml"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def mock_task_router(mock_tasks_yaml: Path) -> TaskRouter:
    """基于 mock_tasks_yaml 构造 TaskRouter。"""
    return TaskRouter.load(mock_tasks_yaml)


@pytest.fixture
def mock_llm_registry(
    mock_task_router: TaskRouter, mock_llm_provider: MockLLMProvider
) -> LLMRegistry:
    """用 mock_llm_provider 注册到 LLMRegistry。

    覆盖 mock 与 embedding 两个 provider 名，便于测试任意 task_type 路由。
    """
    registry = LLMRegistry(
        router=mock_task_router,
        providers={"mock": mock_llm_provider},
    )
    return registry


# ===== 向量检索 fixtures =====


@pytest.fixture
def fake_embedding_fn():
    """返回一个固定向量嵌入函数（dim=8），符合 EmbeddingFn 协议。"""

    def _embed(texts: list[str]) -> list[list[float]]:
        # 简单确定性向量：每个字符的 ord 求和 mod 10，再 padding 到 8 维
        out: list[list[float]] = []
        for t in texts:
            base = sum(ord(c) for c in t) % 10
            out.append([base / 10.0] * 8)
        return out

    return _embed
