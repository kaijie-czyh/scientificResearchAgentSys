"""科研项目会话。

ProjectSession 是单个科研项目的状态入口，封装：
- 当前所有阶段的状态
- 快照存储
- 状态流转操作（前进/回滚/子状态切换）
- 当前活跃阶段与活跃快照

使用范式：
    session = ProjectSession.create("proj_001", paths)
    session.start_stage(LifecycleStage.RESEARCH, triggered_by="user")
    session.mark_pending_review("等待用户确认调研主题")
    session.complete_stage(reason="调研完成")
    session.advance(to_stage=LifecycleStage.IDEATION)
    # 若思路阶段发现调研不足，回滚
    session.rollback_to(LifecycleStage.RESEARCH, reason="需补充 baseline 调研")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.config import ProjectPaths
from core.state.lifecycle import LifecycleStage, StageStatus
from core.state.snapshot import (
    Snapshot,
    SnapshotStore,
    StageState,
    make_snapshot,
)
from core.state.transition import (
    TransitionError,
    TransitionResult,
    TransitionValidator,
)


@dataclass
class ProjectSession:
    """单个科研项目的状态会话。"""

    project_id: str
    paths: ProjectPaths
    stage_states: dict[LifecycleStage, StageState] = field(default_factory=dict)
    current_snapshot_id: Optional[str] = None
    _store: Optional[SnapshotStore] = field(default=None, repr=False)

    @classmethod
    def create(cls, project_id: str, paths: ProjectPaths) -> "ProjectSession":
        """创建新项目会话。所有阶段初始化为 NOT_STARTED。"""
        session = cls(
            project_id=project_id,
            paths=paths,
            stage_states={
                stage: StageState(
                    stage=stage,
                    status=StageStatus.NOT_STARTED,
                    last_updated=datetime.utcnow(),
                )
                for stage in LifecycleStage.ordered()
            },
            _store=SnapshotStore(paths.project_snapshots(project_id)),
        )
        # 写入初始快照
        # 注意：初始化不是真正的状态流转（NOT_STARTED→NOT_STARTED 自流转会被
        # TransitionValidator 拒绝），因此直接构造 TransitionResult，跳过校验。
        init_transition = TransitionResult(
            from_stage=LifecycleStage.RESEARCH,
            from_status=StageStatus.NOT_STARTED,
            to_stage=LifecycleStage.RESEARCH,
            to_status=StageStatus.NOT_STARTED,
            timestamp=datetime.utcnow(),
            reason="项目初始化",
            triggered_by="system",
        )
        session._checkpoint(transition=init_transition, note="项目创建")
        return session

    @classmethod
    def load(cls, project_id: str, paths: ProjectPaths) -> "ProjectSession":
        """从最新快照恢复会话。若无快照则等价于 create。"""
        store = SnapshotStore(paths.project_snapshots(project_id))
        latest_id = store.latest_snapshot_id()
        if latest_id is None:
            return cls.create(project_id, paths)
        snapshot = store.load(latest_id)
        return cls(
            project_id=project_id,
            paths=paths,
            stage_states=dict(snapshot.stage_states),
            current_snapshot_id=snapshot.snapshot_id,
            _store=store,
        )

    # ===== 查询接口 =====

    @property
    def store(self) -> SnapshotStore:
        if self._store is None:
            self._store = SnapshotStore(self.paths.project_snapshots(self.project_id))
        return self._store

    def current_stage(self) -> LifecycleStage:
        """当前活跃阶段：最后一个非 DONE/非 NOT_STARTED 的阶段，
        否则返回第一个 NOT_STARTED 阶段。"""
        ordered = LifecycleStage.ordered()
        # 找最后一个 IN_PROGRESS/PENDING_REVIEW/BLOCKED
        for stage in reversed(ordered):
            status = self.stage_states[stage].status
            if status in (
                StageStatus.IN_PROGRESS,
                StageStatus.PENDING_REVIEW,
                StageStatus.BLOCKED,
            ):
                return stage
        # 否则找第一个 NOT_STARTED
        for stage in ordered:
            if self.stage_states[stage].status == StageStatus.NOT_STARTED:
                return stage
        # 全部 DONE
        return ordered[-1]

    def status_of(self, stage: LifecycleStage) -> StageStatus:
        return self.stage_states[stage].status

    def is_stage_done(self, stage: LifecycleStage) -> bool:
        return self.status_of(stage) == StageStatus.DONE

    # ===== 流转操作 =====

    def start_stage(
        self,
        stage: LifecycleStage,
        triggered_by: str = "user",
        reason: str = "",
    ) -> None:
        """将阶段从 NOT_STARTED 推进到 IN_PROGRESS。"""
        current = self.status_of(stage)
        if current != StageStatus.NOT_STARTED:
            raise TransitionError(
                f"阶段 {stage.value} 当前状态 {current.value}，无法 start"
            )
        self._transition(
            stage=stage,
            target_status=StageStatus.IN_PROGRESS,
            reason=reason or f"启动阶段 {stage.value}",
            triggered_by=triggered_by,
        )

    def mark_pending_review(self, reason: str, triggered_by: str = "agent") -> None:
        """将当前活跃阶段标记为待审核。"""
        stage = self.current_stage()
        self._transition(
            stage=stage,
            target_status=StageStatus.PENDING_REVIEW,
            reason=reason,
            triggered_by=triggered_by,
        )

    def mark_blocked(self, reason: str, triggered_by: str = "agent") -> None:
        """将当前活跃阶段标记为阻塞。"""
        stage = self.current_stage()
        self._transition(
            stage=stage,
            target_status=StageStatus.BLOCKED,
            reason=reason,
            triggered_by=triggered_by,
        )

    def unblock(self, reason: str, triggered_by: str = "user") -> None:
        """解除阻塞，回到 IN_PROGRESS。"""
        stage = self.current_stage()
        self._transition(
            stage=stage,
            target_status=StageStatus.IN_PROGRESS,
            reason=reason,
            triggered_by=triggered_by,
        )

    def complete_stage(self, reason: str, triggered_by: str = "user") -> None:
        """完成当前阶段（要求处于 PENDING_REVIEW 或 IN_PROGRESS）。"""
        stage = self.current_stage()
        current = self.status_of(stage)
        if current not in (StageStatus.PENDING_REVIEW, StageStatus.IN_PROGRESS):
            raise TransitionError(
                f"阶段 {stage.value} 当前状态 {current.value}，无法 complete"
            )
        # PENDING_REVIEW → DONE 需要先经过 IN_PROGRESS？直接放行以简化
        if current == StageStatus.PENDING_REVIEW:
            # 合法路径中 PENDING_REVIEW → DONE 允许
            pass
        self._transition(
            stage=stage,
            target_status=StageStatus.DONE,
            reason=reason,
            triggered_by=triggered_by,
        )

    def advance(
        self,
        to_stage: LifecycleStage,
        reason: str = "",
        triggered_by: str = "user",
    ) -> None:
        """前进到下一阶段。当前阶段必须 DONE。"""
        current_stage = self.current_stage()
        # 找最后一个 DONE 之后的第一个 NOT_STARTED
        if self.status_of(to_stage) != StageStatus.NOT_STARTED:
            raise TransitionError(
                f"目标阶段 {to_stage.value} 当前状态非 NOT_STARTED，无法前进"
            )
        # 当前阶段（前一个）必须 DONE
        prev_stage = LifecycleStage.ordered()[to_stage.index() - 1]
        if not self.is_stage_done(prev_stage):
            raise TransitionError(
                f"前序阶段 {prev_stage.value} 未完成，无法前进到 {to_stage.value}"
            )
        transition = TransitionValidator.make_transition(
            from_stage=prev_stage,
            from_status=self.status_of(prev_stage),
            to_stage=to_stage,
            to_status=StageStatus.IN_PROGRESS,
            reason=reason or f"前进到 {to_stage.value}",
            triggered_by=triggered_by,
        )
        # 应用流转
        self._apply_transition(transition)
        # 立即记录为 IN_PROGRESS（而非 NOT_STARTED），方便直接开始工作
        # 再补一个子状态流转快照
        self._checkpoint(
            transition=TransitionValidator.make_transition(
                from_stage=to_stage,
                from_status=StageStatus.NOT_STARTED,
                to_stage=to_stage,
                to_status=StageStatus.IN_PROGRESS,
                reason=f"进入阶段 {to_stage.value}",
                triggered_by=triggered_by,
            ),
            note=reason,
        )

    def rollback_to(
        self,
        target_stage: LifecycleStage,
        reason: str,
        triggered_by: str = "user",
    ) -> None:
        """回滚到前序阶段。

        回滚语义：
        - 目标阶段及之后所有阶段重置为 NOT_STARTED（清空它们的产出物版本引用）
        - 目标阶段置为 IN_PROGRESS
        - 保留目标阶段之前的所有状态不变
        """
        current_stage = self.current_stage()
        if not current_stage.can_rollback_to(target_stage):
            raise TransitionError(
                f"无法从 {current_stage.value} 回滚到 {target_stage.value}（非前序阶段）"
            )
        # 构造跨阶段回滚流转
        transition = TransitionValidator.make_transition(
            from_stage=current_stage,
            from_status=self.status_of(current_stage),
            to_stage=target_stage,
            to_status=StageStatus.IN_PROGRESS,
            reason=reason,
            triggered_by=triggered_by,
        )
        # 应用：重置目标及之后阶段
        now = datetime.utcnow()
        for stage in LifecycleStage.ordered():
            if stage.index() >= target_stage.index():
                # 目标阶段置 IN_PROGRESS，其后阶段置 NOT_STARTED
                new_status = (
                    StageStatus.IN_PROGRESS
                    if stage == target_stage
                    else StageStatus.NOT_STARTED
                )
                self.stage_states[stage] = StageState(
                    stage=stage,
                    status=new_status,
                    last_updated=now,
                    artifact_versions={} if stage != target_stage
                    else self.stage_states[stage].artifact_versions,
                )
        self._checkpoint(transition=transition, note=reason)

    # ===== 内部实现 =====

    def _transition(
        self,
        stage: LifecycleStage,
        target_status: StageStatus,
        reason: str,
        triggered_by: str,
    ) -> None:
        current_status = self.status_of(stage)
        transition = TransitionValidator.make_transition(
            from_stage=stage,
            from_status=current_status,
            to_stage=stage,
            to_status=target_status,
            reason=reason,
            triggered_by=triggered_by,
        )
        self._apply_transition(transition)
        self._checkpoint(transition=transition, note=reason)

    def _apply_transition(self, transition: TransitionResult) -> None:
        """应用一次流转到内存状态。"""
        now = transition.timestamp
        old = self.stage_states[transition.to_stage]
        self.stage_states[transition.to_stage] = StageState(
            stage=transition.to_stage,
            status=transition.to_status,
            last_updated=now,
            artifact_versions=old.artifact_versions,  # 保留产出物版本引用
        )

    def _checkpoint(
        self,
        transition: TransitionResult,
        note: str = "",
    ) -> None:
        """落盘快照。"""
        snapshot = make_snapshot(
            project_id=self.project_id,
            transition=transition,
            stage_states=dict(self.stage_states),
            parent_snapshot_id=self.current_snapshot_id,
            note=note,
        )
        self.store.save(snapshot)
        self.current_snapshot_id = snapshot.snapshot_id

    # ===== 产出物版本引用 =====

    def attach_artifact_version(
        self,
        stage: LifecycleStage,
        artifact_id: str,
        version: str,
    ) -> None:
        """记录某阶段产出的 artifact 版本。不产生快照（视为阶段内细节）。"""
        old = self.stage_states[stage]
        new_versions = dict(old.artifact_versions)
        new_versions[artifact_id] = version
        self.stage_states[stage] = StageState(
            stage=stage,
            status=old.status,
            last_updated=datetime.utcnow(),
            artifact_versions=new_versions,
        )

    def snapshot_history(self) -> list[dict]:
        """返回快照历史索引。"""
        return self.store.list_snapshots()

    def restore_from_snapshot(self, snapshot_id: str) -> None:
        """从指定快照恢复状态。用于回滚到历史时点。"""
        snapshot = self.store.load(snapshot_id)
        self.stage_states = dict(snapshot.stage_states)
        self.current_snapshot_id = snapshot.snapshot_id
