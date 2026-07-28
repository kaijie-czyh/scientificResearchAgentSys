"""TaskRouter 配置加载测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.llm.task_router import EmbeddingConfig, TaskConfig, TaskNotFoundError, TaskRouter


# ===== load =====


def test_load_parses_tasks_and_embedding(mock_tasks_yaml: Path):
    """load 应正确解析 tasks 与 embedding 配置。"""
    router = TaskRouter.load(mock_tasks_yaml)

    # tasks
    tasks = router.list_tasks()
    assert "test_task" in tasks
    assert "another_task" in tasks

    # embedding
    emb = router.embedding_config()
    assert emb.provider == "mock"
    assert emb.model == "mock-embed"
    assert emb.dim == 8


def test_load_raises_on_missing_file(tmp_path: Path):
    """load 不存在的配置文件应抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        TaskRouter.load(tmp_path / "nonexistent.yaml")


def test_load_uses_default_embedding_when_not_specified(tmp_path: Path):
    """配置文件无 embedding 段时，应使用默认 embedding 配置。"""
    content = """
tasks:
  test_task:
    provider: mock
    model: mock-model
"""
    p = tmp_path / "tasks_no_emb.yaml"
    p.write_text(content, encoding="utf-8")

    router = TaskRouter.load(p)
    emb = router.embedding_config()
    # 默认 provider=openai, dim=1536
    assert isinstance(emb, EmbeddingConfig)
    assert emb.dim == 1536


# ===== get =====


def test_get_returns_task_config(mock_task_router: TaskRouter):
    """get 应返回对应 task_type 的 TaskConfig。"""
    cfg = mock_task_router.get("test_task")

    assert isinstance(cfg, TaskConfig)
    assert cfg.task_type == "test_task"
    assert cfg.provider == "mock"
    assert cfg.model == "mock-model"
    assert cfg.temperature == 0.0


def test_get_raises_on_unknown_task(mock_task_router: TaskRouter):
    """get 未定义的 task_type 应抛 TaskNotFoundError。"""
    with pytest.raises(TaskNotFoundError):
        mock_task_router.get("nonexistent_task")


def test_get_uses_default_temperature_when_not_set(tmp_path: Path):
    """配置文件未指定 temperature 时，应使用默认值 0.2。"""
    content = """
tasks:
  no_temp:
    provider: mock
    model: m
"""
    p = tmp_path / "tasks_no_temp.yaml"
    p.write_text(content, encoding="utf-8")

    router = TaskRouter.load(p)
    cfg = router.get("no_temp")
    assert cfg.temperature == 0.2  # 默认值
    assert cfg.max_tokens == 2048  # 默认值


# ===== embedding_config =====


def test_embedding_config_returns_embedding_config(mock_task_router: TaskRouter):
    """embedding_config 应返回 EmbeddingConfig 实例。"""
    emb = mock_task_router.embedding_config()

    assert isinstance(emb, EmbeddingConfig)
    assert emb.provider == "mock"
    assert emb.model == "mock-embed"
    assert emb.dim == 8


# ===== list_tasks =====


def test_list_tasks_returns_all_task_names(mock_task_router: TaskRouter):
    """list_tasks 应返回所有 task_type 名列表。"""
    tasks = mock_task_router.list_tasks()

    assert isinstance(tasks, list)
    assert set(tasks) == {"test_task", "another_task"}
