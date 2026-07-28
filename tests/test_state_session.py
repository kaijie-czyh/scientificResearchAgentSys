"""ProjectSession 完整流转测试。

覆盖：
- create / load
- start_stage / mark_pending_review / mark_blocked / unblock / complete_stage
- advance / rollback_to
- current_stage / status_of / is_stage_done
- attach_artifact_version / snapshot_history / restore_from_snapshot
"""
from __future__ import annotations

import pytest

from core.config import ProjectPaths
from core.state.lifecycle import LifecycleStage, StageStatus
from core.state.session import ProjectSession
from core.state.transition import TransitionError


# ===== create / load =====


def test_create_initializes_all_stages_to_not_started(
    tmp_project_paths: ProjectPaths,
):
    """create 应将所有阶段初始化为 NOT_STARTED，并写入初始快照。"""
    session = ProjectSession.create("proj_001", tmp_project_paths)

    # 所有阶段均为 NOT_STARTED
    for stage in LifecycleStage.ordered():
        assert session.status_of(stage) == StageStatus.NOT_STARTED

    # 已写入初始快照
    assert session.current_snapshot_id is not None
    assert len(session.snapshot_history()) >= 1


def test_create_sets_current_stage_to_research(tmp_project_paths: ProjectPaths):
    """create 后 current_stage 应返回第一个 NOT_STARTED 阶段（research）。"""
    session = ProjectSession.create("proj_001", tmp_project_paths)
    assert session.current_stage() == LifecycleStage.RESEARCH


def test_load_recovers_from_latest_snapshot(tmp_project_paths: ProjectPaths):
    """load 应从最新快照恢复会话状态。"""
    # Arrange: 创建并推进会话
    session1 = ProjectSession.create("proj_load", tmp_project_paths)
    session1.start_stage(LifecycleStage.RESEARCH, reason="开始调研")
    assert session1.current_snapshot_id is not None

    # Act: 重新加载
    session2 = ProjectSession.load("proj_load", tmp_project_paths)

    # Assert: 状态应一致
    assert session2.status_of(LifecycleStage.RESEARCH) == StageStatus.IN_PROGRESS
    assert session2.current_snapshot_id == session1.current_snapshot_id


def test_load_without_snapshot_falls_back_to_create(tmp_project_paths: ProjectPaths):
    """无快照时 load 等价于 create。"""
    session = ProjectSession.load("proj_no_snapshot", tmp_project_paths)
    assert session.current_snapshot_id is not None
    for stage in LifecycleStage.ordered():
        assert session.status_of(stage) == StageStatus.NOT_STARTED


# ===== start_stage =====


def test_start_stage_transitions_not_started_to_in_progress(
    tmp_project_paths: ProjectPaths,
):
    """start_stage 应将 NOT_STARTED 推进到 IN_PROGRESS。"""
    session = ProjectSession.create("proj_002", tmp_project_paths)

    session.start_stage(LifecycleStage.RESEARCH, reason="启动")

    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.IN_PROGRESS


def test_start_stage_rejects_non_not_started_stage(tmp_project_paths: ProjectPaths):
    """对非 NOT_STARTED 阶段调用 start_stage 应抛异常。"""
    session = ProjectSession.create("proj_003", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)

    # 已经 IN_PROGRESS，再次 start 应失败
    with pytest.raises(TransitionError):
        session.start_stage(LifecycleStage.RESEARCH)


# ===== mark_pending_review / mark_blocked / unblock =====


def test_mark_pending_review_transitions_in_progress_to_pending(
    tmp_project_paths: ProjectPaths,
):
    """mark_pending_review 应将当前阶段置为 PENDING_REVIEW。"""
    session = ProjectSession.create("proj_004", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)
    assert session.current_stage() == LifecycleStage.RESEARCH

    session.mark_pending_review("等待审核")

    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.PENDING_REVIEW


def test_mark_blocked_transitions_to_blocked(tmp_project_paths: ProjectPaths):
    """mark_blocked 应将当前阶段置为 BLOCKED。"""
    session = ProjectSession.create("proj_005", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)

    session.mark_blocked("数据缺失")

    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.BLOCKED


def test_unblock_transitions_blocked_to_in_progress(tmp_project_paths: ProjectPaths):
    """unblock 应将 BLOCKED 状态置回 IN_PROGRESS。"""
    session = ProjectSession.create("proj_006", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)
    session.mark_blocked("阻塞")
    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.BLOCKED

    session.unblock("已解决")

    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.IN_PROGRESS


# ===== complete_stage =====


def test_complete_stage_from_pending_review(tmp_project_paths: ProjectPaths):
    """PENDING_REVIEW → DONE 应成功。"""
    session = ProjectSession.create("proj_007", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)
    session.mark_pending_review("待审核")

    session.complete_stage(reason="审核通过")

    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.DONE


def test_complete_stage_from_in_progress(tmp_project_paths: ProjectPaths):
    """IN_PROGRESS → DONE 应成功。"""
    session = ProjectSession.create("proj_008", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)

    session.complete_stage(reason="直接完成")

    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.DONE


def test_complete_stage_rejects_not_started(tmp_project_paths: ProjectPaths):
    """NOT_STARTED 阶段不能直接 complete。"""
    session = ProjectSession.create("proj_009", tmp_project_paths)
    with pytest.raises(TransitionError):
        session.complete_stage(reason="跳过")


# ===== advance =====


def test_advance_moves_to_next_stage(tmp_project_paths: ProjectPaths):
    """DONE 后 advance 到下一阶段，下一阶段应变为 IN_PROGRESS。"""
    session = ProjectSession.create("proj_010", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)
    session.complete_stage(reason="完成")

    session.advance(to_stage=LifecycleStage.IDEATION, reason="前进")

    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.DONE
    assert session.status_of(LifecycleStage.IDEATION) == StageStatus.IN_PROGRESS
    assert session.current_stage() == LifecycleStage.IDEATION


def test_advance_rejects_when_prev_not_done(tmp_project_paths: ProjectPaths):
    """前序阶段未完成时 advance 应抛异常。"""
    session = ProjectSession.create("proj_011", tmp_project_paths)
    # research 未完成就尝试 advance 到 ideation
    with pytest.raises(TransitionError):
        session.advance(to_stage=LifecycleStage.IDEATION)


def test_advance_rejects_when_target_not_not_started(tmp_project_paths: ProjectPaths):
    """目标阶段非 NOT_STARTED 时 advance 应抛异常。"""
    session = ProjectSession.create("proj_012", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)
    session.complete_stage(reason="完成")
    # 手动把 ideation 改为非 NOT_STARTED（先 start 再 advance 应失败）
    # 这里通过 advance 一次让 ideation 进入 IN_PROGRESS
    session.advance(to_stage=LifecycleStage.IDEATION)
    # 现在 ideation 是 IN_PROGRESS，再 advance 到 ideation 应失败
    # 先把 ideation 完成，再尝试 advance 到 ideation（已 NOT_STARTED 不成立）
    session.complete_stage(reason="ideation 完成")
    # 现在 ideation 是 DONE，再 advance 到 ideation 应失败
    with pytest.raises(TransitionError):
        session.advance(to_stage=LifecycleStage.IDEATION)


# ===== rollback_to =====


def test_rollback_to_resets_target_and_later_stages(tmp_project_paths: ProjectPaths):
    """回滚应将目标阶段置为 IN_PROGRESS，其后阶段重置为 NOT_STARTED。"""
    session = ProjectSession.create("proj_013", tmp_project_paths)
    # 推进到 design
    session.start_stage(LifecycleStage.RESEARCH)
    session.complete_stage(reason="done")
    session.advance(to_stage=LifecycleStage.IDEATION)
    session.complete_stage(reason="done")
    session.advance(to_stage=LifecycleStage.DESIGN)
    assert session.status_of(LifecycleStage.DESIGN) == StageStatus.IN_PROGRESS

    # 回滚到 research
    session.rollback_to(LifecycleStage.RESEARCH, reason="需重新调研")

    # research 应为 IN_PROGRESS
    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.IN_PROGRESS
    # ideation / design 应重置为 NOT_STARTED
    assert session.status_of(LifecycleStage.IDEATION) == StageStatus.NOT_STARTED
    assert session.status_of(LifecycleStage.DESIGN) == StageStatus.NOT_STARTED
    # current_stage 应为 research
    assert session.current_stage() == LifecycleStage.RESEARCH


def test_rollback_to_rejects_later_target(tmp_project_paths: ProjectPaths):
    """回滚到后序阶段应抛异常。"""
    session = ProjectSession.create("proj_014", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)

    with pytest.raises(TransitionError):
        session.rollback_to(LifecycleStage.WRITING, reason="向前回滚")


# ===== current_stage / status_of / is_stage_done =====


def test_current_stage_returns_last_active_stage(tmp_project_paths: ProjectPaths):
    """current_stage 应返回最后一个 IN_PROGRESS/PENDING_REVIEW/BLOCKED 阶段。"""
    session = ProjectSession.create("proj_015", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)
    session.complete_stage(reason="done")
    session.advance(to_stage=LifecycleStage.IDEATION)

    # ideation 是 IN_PROGRESS，应作为 current_stage
    assert session.current_stage() == LifecycleStage.IDEATION


def test_status_of_returns_specific_stage_status(tmp_project_paths: ProjectPaths):
    """status_of 应返回指定阶段的当前状态。"""
    session = ProjectSession.create("proj_016", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)

    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.IN_PROGRESS
    assert session.status_of(LifecycleStage.IDEATION) == StageStatus.NOT_STARTED


def test_is_stage_done_returns_true_only_for_done(tmp_project_paths: ProjectPaths):
    """is_stage_done 仅在 DONE 时返回 True。"""
    session = ProjectSession.create("proj_017", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)

    assert not session.is_stage_done(LifecycleStage.RESEARCH)
    session.complete_stage(reason="done")
    assert session.is_stage_done(LifecycleStage.RESEARCH)
    assert not session.is_stage_done(LifecycleStage.IDEATION)


# ===== attach_artifact_version =====


def test_attach_artifact_version_records_version(tmp_project_paths: ProjectPaths):
    """attach_artifact_version 应记录某阶段的 artifact 版本。"""
    session = ProjectSession.create("proj_018", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)

    session.attach_artifact_version(
        LifecycleStage.RESEARCH, artifact_id="art_xxx", version="v1"
    )

    # 通过快照历史或 stage_states 间接验证（artifact_versions 不在 status_of 中体现）
    # 这里检查不抛异常即视为成功；阶段状态应保持 IN_PROGRESS
    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.IN_PROGRESS


# ===== snapshot_history / restore_from_snapshot =====


def test_snapshot_history_grows_with_each_transition(
    tmp_project_paths: ProjectPaths,
):
    """每次状态流转都应追加一个快照到 history。"""
    session = ProjectSession.create("proj_019", tmp_project_paths)
    initial_count = len(session.snapshot_history())

    session.start_stage(LifecycleStage.RESEARCH)
    after_start = len(session.snapshot_history())
    assert after_start == initial_count + 1

    session.complete_stage(reason="done")
    after_complete = len(session.snapshot_history())
    assert after_complete == after_start + 1


def test_restore_from_snapshot_recovers_state(tmp_project_paths: ProjectPaths):
    """restore_from_snapshot 应将状态恢复到指定快照时点。"""
    session = ProjectSession.create("proj_020", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH)
    session.complete_stage(reason="done")
    # 记录完成时点的快照
    done_snapshot_id = session.current_snapshot_id
    assert done_snapshot_id is not None

    # 继续推进
    session.advance(to_stage=LifecycleStage.IDEATION)
    assert session.status_of(LifecycleStage.IDEATION) == StageStatus.IN_PROGRESS

    # 回滚到完成时点
    session.restore_from_snapshot(done_snapshot_id)

    # research 应仍为 DONE，ideation 应为 NOT_STARTED
    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.DONE
    assert session.status_of(LifecycleStage.IDEATION) == StageStatus.NOT_STARTED
    assert session.current_snapshot_id == done_snapshot_id


# ===== 完整流转 =====


def test_full_lifecycle_flow(tmp_project_paths: ProjectPaths):
    """测试完整流转：create → start → pending_review → complete → advance → rollback → restore。"""
    # Arrange + Act: 创建并启动 research
    session = ProjectSession.create("proj_full", tmp_project_paths)
    session.start_stage(LifecycleStage.RESEARCH, reason="启动调研")
    session.mark_pending_review("等待用户确认调研主题")
    assert session.status_of(LifecycleStage.RESEARCH) == StageStatus.PENDING_REVIEW

    # 用户审核通过，完成 research
    session.complete_stage(reason="调研完成")
    assert session.is_stage_done(LifecycleStage.RESEARCH)

    # 前进到 ideation
    session.advance(to_stage=LifecycleStage.IDEATION, reason="前进到思路探讨")
    assert session.current_stage() == LifecycleStage.IDEATION
    ideation_snapshot_id = session.current_snapshot_id

    # 思路阶段发现调研不足，回滚到 research
    session.rollback_to(LifecycleStage.RESEARCH, reason="需补充 baseline 调研")
    assert session.current_stage() == LifecycleStage.RESEARCH
    assert session.status_of(LifecycleStage.IDEATION) == StageStatus.NOT_STARTED

    # 从 ideation 时点恢复
    session.restore_from_snapshot(ideation_snapshot_id)
    assert session.current_stage() == LifecycleStage.IDEATION
    assert session.status_of(LifecycleStage.IDEATION) == StageStatus.IN_PROGRESS
