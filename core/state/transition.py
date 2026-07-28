"""状态流转校验。

校验两类流转：
1. 阶段内子状态流转：按 LEGAL_STATUS_TRANSITIONS
2. 跨阶段流转：
   - 前进：当前阶段必须 DONE，目标必须是 next_stage
   - 回滚：目标必须 can_rollback_to（前序任意阶段）

每次合法流转产生 TransitionResult，由 ProjectSession 落盘为快照。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.state.lifecycle import LifecycleStage, StageStatus, LEGAL_STATUS_TRANSITIONS


class TransitionError(Exception):
    """非法状态流转。"""


@dataclass(frozen=True)
class TransitionResult:
    """一次状态流转的结果。"""

    from_stage: LifecycleStage
    from_status: StageStatus
    to_stage: LifecycleStage
    to_status: StageStatus
    timestamp: datetime
    reason: str  # 触发流转的原因（人为/异常/审核通过等）
    triggered_by: str  # 触发者（user / agent_id / system）


class TransitionValidator:
    """状态流转校验器。无状态，可全局复用。"""

    @staticmethod
    def validate_status_transition(
        stage: LifecycleStage,
        current: StageStatus,
        target: StageStatus,
    ) -> None:
        """校验阶段内子状态流转合法性。非法则抛 TransitionError。"""
        legal = LEGAL_STATUS_TRANSITIONS.get(current, set())
        if target not in legal:
            raise TransitionError(
                f"非法子状态流转：阶段={stage.value} {current.value} → {target.value}；"
                f"合法目标={sorted(s.value for s in legal) or '<终态>'}"
            )

    @staticmethod
    def validate_advance(
        current_stage: LifecycleStage,
        current_status: StageStatus,
        target_stage: LifecycleStage,
    ) -> None:
        """校验前进流转：当前阶段必须 DONE，目标必须是 next_stage。"""
        if current_status != StageStatus.DONE:
            raise TransitionError(
                f"前进流转要求当前阶段为 DONE，实际={current_status.value}"
            )
        next_stage = current_stage.next_stage()
        if next_stage is None:
            raise TransitionError(
                f"阶段 {current_stage.value} 已是最后阶段，无法前进"
            )
        if target_stage != next_stage:
            raise TransitionError(
                f"前进流转只能到下一阶段 {next_stage.value}，目标={target_stage.value}"
            )

    @staticmethod
    def validate_rollback(
        current_stage: LifecycleStage,
        target_stage: LifecycleStage,
    ) -> None:
        """校验回滚流转：目标必须是前序阶段。"""
        if not current_stage.can_rollback_to(target_stage):
            raise TransitionError(
                f"回滚只能到前序阶段：当前={current_stage.value}，目标={target_stage.value}"
            )

    @staticmethod
    def make_transition(
        from_stage: LifecycleStage,
        from_status: StageStatus,
        to_stage: LifecycleStage,
        to_status: StageStatus,
        reason: str,
        triggered_by: str,
        timestamp: Optional[datetime] = None,
    ) -> TransitionResult:
        """构造一次合法流转。先校验，再返回 TransitionResult。

        跨阶段流转（from_stage != to_stage）会同时校验前进/回滚规则。
        """
        if timestamp is None:
            timestamp = datetime.utcnow()

        if from_stage == to_stage:
            # 阶段内子状态流转
            TransitionValidator.validate_status_transition(
                from_stage, from_status, to_status
            )
        else:
            # 跨阶段流转：判断前进 or 回滚
            if to_stage.index() > from_stage.index():
                TransitionValidator.validate_advance(
                    from_stage, from_status, to_stage
                )
            else:
                TransitionValidator.validate_rollback(from_stage, to_stage)
            # 跨阶段后，子状态通常从 NOT_STARTED 或 IN_PROGRESS 起步
            if to_status not in (StageStatus.NOT_STARTED, StageStatus.IN_PROGRESS):
                raise TransitionError(
                    f"跨阶段流转后子状态应从 NOT_STARTED/IN_PROGRESS 起步，"
                    f"目标={to_status.value}"
                )

        return TransitionResult(
            from_stage=from_stage,
            from_status=from_status,
            to_stage=to_stage,
            to_status=to_status,
            timestamp=timestamp,
            reason=reason,
            triggered_by=triggered_by,
        )
