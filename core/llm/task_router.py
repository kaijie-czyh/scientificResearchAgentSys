"""任务路由配置。

从 tasks.yaml 加载，按 task_type 查询 provider/model/参数。

tasks.yaml 结构见 config/tasks.yaml。

路由原则：
- 每个 task_type 对应一组 provider+model+参数
- 敏感任务（含 "private"）路由到本地 provider
- 嵌入模型独立配置
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


class TaskNotFoundError(KeyError):
    """未找到对应 task_type 的配置。"""


@dataclass(frozen=True)
class TaskConfig:
    """单个任务的 LLM 配置。"""

    task_type: str
    provider: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 2048
    description: str = ""


@dataclass(frozen=True)
class EmbeddingConfig:
    """嵌入模型配置。"""

    provider: str
    model: str
    dim: int = 1536


class TaskRouter:
    """任务路由器。无状态，可全局复用。"""

    def __init__(self, tasks: dict[str, TaskConfig], embedding: EmbeddingConfig):
        self._tasks = tasks
        self._embedding = embedding

    @classmethod
    def load(cls, config_path: Path) -> "TaskRouter":
        """从 YAML 加载。"""
        if not config_path.exists():
            raise FileNotFoundError(f"任务配置文件不存在: {config_path}")
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        tasks: dict[str, TaskConfig] = {}
        for task_type, cfg in (data.get("tasks") or {}).items():
            tasks[task_type] = TaskConfig(
                task_type=task_type,
                provider=cfg["provider"],
                model=cfg["model"],
                temperature=cfg.get("temperature", 0.2),
                max_tokens=cfg.get("max_tokens", 2048),
                description=cfg.get("description", ""),
            )

        emb_data = data.get("embedding") or {}
        embedding = EmbeddingConfig(
            provider=emb_data.get("provider", "openai"),
            model=emb_data.get("model", "text-embedding-3-small"),
            dim=emb_data.get("dim", 1536),
        )

        return cls(tasks=tasks, embedding=embedding)

    def get(self, task_type: str) -> TaskConfig:
        """查询 task 配置。未找到则抛 TaskNotFoundError。"""
        if task_type not in self._tasks:
            raise TaskNotFoundError(
                f"未在 tasks.yaml 中找到 task_type={task_type}，"
                f"已配置 tasks={list(self._tasks.keys())}"
            )
        return self._tasks[task_type]

    def embedding_config(self) -> EmbeddingConfig:
        return self._embedding

    def list_tasks(self) -> list[str]:
        return list(self._tasks.keys())
