"""编排上下文。

ExecutionContext 是节点间传递数据的唯一通道。
- 强类型：通过 ContextKey<T> 声明键的类型
- 不可变快照：可对上下文做快照，用于回滚
- 跨节点共享：所有节点共享同一上下文实例
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


@dataclass(frozen=True)
class ContextKey(Generic[T]):
    """上下文键。强类型，避免散落的字符串键。

    使用范式：
        PAPER_IDS = ContextKey[list[str]]("research.paper_ids")
        paper_ids = ctx.get(PAPER_IDS, default=[])
        ctx.set(PAPER_IDS, ["p1", "p2"])
    """

    name: str

    def __str__(self) -> str:
        return self.name


@dataclass
class ExecutionContext:
    """执行上下文。

    内部用 dict[str, Any] 存储，但通过 ContextKey<T> 访问以保留类型信息。
    支持快照与回滚（深拷贝）。
    """

    # 项目级元数据
    project_id: str
    # 当前生命周期阶段
    current_stage: str = ""
    # 当前节点 ID
    current_node_id: str = ""
    # 数据存储
    _data: dict[str, Any] = field(default_factory=dict)
    # 节点执行历史（node_id -> NodeResult 摘要）
    _history: list[dict[str, Any]] = field(default_factory=list)

    # ===== 数据访问 =====

    def get(self, key: ContextKey[T], default: Optional[T] = None) -> T:
        return self._data.get(key.name, default)

    def set(self, key: ContextKey[T], value: T) -> None:
        self._data[key.name] = value

    def has(self, key: ContextKey[T]) -> bool:
        return key.name in self._data

    def delete(self, key: ContextKey[T]) -> None:
        self._data.pop(key.name, None)

    def keys(self) -> list[str]:
        return list(self._data.keys())

    # ===== 历史记录 =====

    def record_node_result(
        self,
        node_id: str,
        node_type: str,
        status: str,
        summary: str,
        timestamp: Optional[datetime] = None,
    ) -> None:
        self._history.append(
            {
                "node_id": node_id,
                "node_type": node_type,
                "status": status,
                "summary": summary,
                "timestamp": (timestamp or datetime.utcnow()).isoformat(),
            }
        )

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    # ===== 快照 =====

    def snapshot(self) -> dict[str, Any]:
        """深拷贝当前数据与历史，用于回滚。

        注意：
        - system.* 前缀的系统依赖（LLMRegistry/KnowledgeStore 等）不可深拷贝
          （持有 sqlite 连接/OpenAI client），用引用即可——它们不需要随快照回滚。
        - checkpoint.* 前缀的旧快照不纳入新快照：
          1. 旧快照内部含 system.* 引用，深拷贝会触发 TypeError
          2. 语义上检查点是临时回滚点，回滚后由 CheckpointNode 重新生成，
             不需要在新快照里保留历史检查点
        """
        # 系统依赖与旧检查点都用引用/跳过，仅深拷贝纯域数据
        sys_data = {k: v for k, v in self._data.items() if k.startswith("system.")}
        domain_data = {
            k: v
            for k, v in self._data.items()
            if not k.startswith("system.") and not k.startswith("checkpoint.")
        }
        return {
            "project_id": self.project_id,
            "current_stage": self.current_stage,
            "current_node_id": self.current_node_id,
            "data": {**sys_data, **copy.deepcopy(domain_data)},
            "history": copy.deepcopy(self._history),
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        """从快照恢复。

        保留当前系统依赖（system.*）与当前检查点（checkpoint.*），
        只恢复域数据与历史。检查点保留当前值便于回滚后重新执行 CheckpointNode。
        """
        self.project_id = snapshot["project_id"]
        self.current_stage = snapshot["current_stage"]
        self.current_node_id = snapshot["current_node_id"]
        # 保留当前系统依赖与检查点，合并快照中的域数据
        current_keep = {
            k: v
            for k, v in self._data.items()
            if k.startswith("system.") or k.startswith("checkpoint.")
        }
        snapshot_domain = {
            k: v
            for k, v in snapshot["data"].items()
            if not k.startswith("system.") and not k.startswith("checkpoint.")
        }
        self._data = {**current_keep, **copy.deepcopy(snapshot_domain)}
        self._history = copy.deepcopy(snapshot["history"])

    # ===== 视图 =====

    def view(self) -> dict[str, Any]:
        """返回可读视图（用于日志与调试）。"""
        return {
            "project_id": self.project_id,
            "current_stage": self.current_stage,
            "current_node_id": self.current_node_id,
            "data_keys": list(self._data.keys()),
            "history_length": len(self._history),
        }
