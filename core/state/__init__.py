"""生命周期状态机层。

提供 5 阶段状态机、阶段内子状态、跨阶段流转与回滚、快照持久化。

核心导出：
- LifecycleStage: 5 个生命周期阶段枚举
- StageStatus: 阶段内子状态枚举
- Snapshot: 单次快照
- SnapshotStore: 快照存储（JSON 文件，按阶段分目录）
- ProjectSession: 单个科研项目的状态会话
- TransitionValidator: 状态流转合法性校验
"""
from core.state.lifecycle import LifecycleStage, StageStatus
from core.state.snapshot import Snapshot, SnapshotStore
from core.state.session import ProjectSession
from core.state.transition import (
    TransitionValidator,
    TransitionError,
    TransitionResult,
)

__all__ = [
    "LifecycleStage",
    "StageStatus",
    "Snapshot",
    "SnapshotStore",
    "ProjectSession",
    "TransitionValidator",
    "TransitionError",
    "TransitionResult",
]
