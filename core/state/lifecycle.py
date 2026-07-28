"""生命周期阶段与子状态定义。

5 阶段：调研 → 思路探讨 → 方案制定 → 实验运行 → 论文写作
可回滚：任意阶段可回退到前序阶段。

阶段内子状态：
    not_started → in_progress → pending_review → done
                       ↓              ↓
                   blocked        blocked (review 不通过)

设计原则：
- 阶段流转与子状态流转分离，避免组合爆炸
- 每次流转产生快照，便于回滚与审计
"""
from __future__ import annotations

from enum import Enum


class LifecycleStage(str, Enum):
    """5 个生命周期阶段。值用字符串便于序列化。"""

    RESEARCH = "research"        # 调研
    IDEATION = "ideation"        # 思路探讨
    DESIGN = "design"            # 方案制定
    EXPERIMENT = "experiment"    # 实验运行
    WRITING = "writing"          # 论文写作

    @classmethod
    def ordered(cls) -> list["LifecycleStage"]:
        """按生命周期顺序返回所有阶段。"""
        return [cls.RESEARCH, cls.IDEATION, cls.DESIGN, cls.EXPERIMENT, cls.WRITING]

    def index(self) -> int:
        """该阶段在生命周期中的序号（0-based）。"""
        return self.ordered().index(self)

    def can_rollback_to(self, target: "LifecycleStage") -> bool:
        """是否能从当前阶段回滚到 target 阶段。

        规则：只能回滚到前序阶段（index 更小），不允许"跳到未来"。
        前进流转由 TransitionValidator 单独管控。
        """
        return target.index() < self.index()

    def next_stage(self) -> "LifecycleStage | None":
        """下一个阶段，最后一个阶段返回 None。"""
        ordered = self.ordered()
        idx = self.index()
        return ordered[idx + 1] if idx + 1 < len(ordered) else None

    def prev_stage(self) -> "LifecycleStage | None":
        """上一个阶段，第一个阶段返回 None。"""
        ordered = self.ordered()
        idx = self.index()
        return ordered[idx - 1] if idx - 1 >= 0 else None


class StageStatus(str, Enum):
    """阶段内子状态。"""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"  # 等待人工审核
    DONE = "done"
    BLOCKED = "blocked"  # 阻塞（异常或审核不通过）


# 合法的子状态流转
LEGAL_STATUS_TRANSITIONS: dict[StageStatus, set[StageStatus]] = {
    StageStatus.NOT_STARTED: {StageStatus.IN_PROGRESS},
    StageStatus.IN_PROGRESS: {StageStatus.PENDING_REVIEW, StageStatus.BLOCKED, StageStatus.DONE},
    StageStatus.PENDING_REVIEW: {StageStatus.DONE, StageStatus.BLOCKED, StageStatus.IN_PROGRESS},
    StageStatus.BLOCKED: {StageStatus.IN_PROGRESS},  # 解除阻塞后重新进行
    StageStatus.DONE: set(),  # 终态，子状态层面不再流转
}
