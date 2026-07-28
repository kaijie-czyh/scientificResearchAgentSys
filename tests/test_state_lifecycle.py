"""LifecycleStage / StageStatus 枚举测试。"""
from __future__ import annotations

from core.state.lifecycle import (
    LEGAL_STATUS_TRANSITIONS,
    LifecycleStage,
    StageStatus,
)


# ===== LifecycleStage.ordered / index =====


def test_ordered_returns_5_stages_in_canonical_order():
    """ordered() 应返回 5 个阶段，顺序为 research → ideation → design → experiment → writing。"""
    # Act
    ordered = LifecycleStage.ordered()

    # Assert
    assert ordered == [
        LifecycleStage.RESEARCH,
        LifecycleStage.IDEATION,
        LifecycleStage.DESIGN,
        LifecycleStage.EXPERIMENT,
        LifecycleStage.WRITING,
    ]
    assert len(ordered) == 5


def test_index_returns_zero_based_position():
    """index() 应返回 0-based 序号。"""
    assert LifecycleStage.RESEARCH.index() == 0
    assert LifecycleStage.IDEATION.index() == 1
    assert LifecycleStage.DESIGN.index() == 2
    assert LifecycleStage.EXPERIMENT.index() == 3
    assert LifecycleStage.WRITING.index() == 4


# ===== next_stage / prev_stage =====


def test_next_stage_returns_next_for_non_last_stages():
    """非最后阶段的 next_stage() 应返回下一阶段。"""
    assert LifecycleStage.RESEARCH.next_stage() == LifecycleStage.IDEATION
    assert LifecycleStage.IDEATION.next_stage() == LifecycleStage.DESIGN
    assert LifecycleStage.DESIGN.next_stage() == LifecycleStage.EXPERIMENT
    assert LifecycleStage.EXPERIMENT.next_stage() == LifecycleStage.WRITING


def test_next_stage_returns_none_for_last_stage():
    """最后阶段（writing）的 next_stage() 应返回 None。"""
    assert LifecycleStage.WRITING.next_stage() is None


def test_prev_stage_returns_prev_for_non_first_stages():
    """非第一阶段 prev_stage() 应返回上一阶段。"""
    assert LifecycleStage.IDEATION.prev_stage() == LifecycleStage.RESEARCH
    assert LifecycleStage.DESIGN.prev_stage() == LifecycleStage.IDEATION
    assert LifecycleStage.EXPERIMENT.prev_stage() == LifecycleStage.DESIGN
    assert LifecycleStage.WRITING.prev_stage() == LifecycleStage.EXPERIMENT


def test_prev_stage_returns_none_for_first_stage():
    """第一阶段（research）的 prev_stage() 应返回 None。"""
    assert LifecycleStage.RESEARCH.prev_stage() is None


# ===== can_rollback_to =====


def test_can_rollback_to_returns_true_for_earlier_stages():
    """对前序阶段，can_rollback_to 应返回 True。"""
    assert LifecycleStage.WRITING.can_rollback_to(LifecycleStage.EXPERIMENT)
    assert LifecycleStage.WRITING.can_rollback_to(LifecycleStage.RESEARCH)
    assert LifecycleStage.EXPERIMENT.can_rollback_to(LifecycleStage.IDEATION)


def test_can_rollback_to_returns_false_for_same_or_later_stage():
    """对同阶段或后序阶段，can_rollback_to 应返回 False。"""
    # 同阶段
    assert not LifecycleStage.WRITING.can_rollback_to(LifecycleStage.WRITING)
    assert not LifecycleStage.RESEARCH.can_rollback_to(LifecycleStage.RESEARCH)
    # 后序阶段
    assert not LifecycleStage.RESEARCH.can_rollback_to(LifecycleStage.IDEATION)
    assert not LifecycleStage.IDEATION.can_rollback_to(LifecycleStage.WRITING)


# ===== StageStatus 枚举 =====


def test_stage_status_has_5_members():
    """StageStatus 应有 5 个成员。"""
    members = list(StageStatus)
    assert len(members) == 5
    assert StageStatus.NOT_STARTED in members
    assert StageStatus.IN_PROGRESS in members
    assert StageStatus.PENDING_REVIEW in members
    assert StageStatus.DONE in members
    assert StageStatus.BLOCKED in members


def test_stage_status_string_values_are_lowercase():
    """StageStatus 字符串值应是小写 snake_case。"""
    assert StageStatus.NOT_STARTED.value == "not_started"
    assert StageStatus.IN_PROGRESS.value == "in_progress"
    assert StageStatus.PENDING_REVIEW.value == "pending_review"
    assert StageStatus.DONE.value == "done"
    assert StageStatus.BLOCKED.value == "blocked"


# ===== LEGAL_STATUS_TRANSITIONS 关键路径 =====


def test_legal_status_transitions_contains_expected_paths():
    """LEGAL_STATUS_TRANSITIONS 应包含关键合法流转路径。"""
    # NOT_STARTED 只能去 IN_PROGRESS
    assert StageStatus.IN_PROGRESS in LEGAL_STATUS_TRANSITIONS[StageStatus.NOT_STARTED]
    assert StageStatus.DONE not in LEGAL_STATUS_TRANSITIONS[StageStatus.NOT_STARTED]

    # IN_PROGRESS 可以去 PENDING_REVIEW / BLOCKED / DONE
    in_progress_targets = LEGAL_STATUS_TRANSITIONS[StageStatus.IN_PROGRESS]
    assert StageStatus.PENDING_REVIEW in in_progress_targets
    assert StageStatus.BLOCKED in in_progress_targets
    assert StageStatus.DONE in in_progress_targets

    # BLOCKED 只能回 IN_PROGRESS
    assert LEGAL_STATUS_TRANSITIONS[StageStatus.BLOCKED] == {StageStatus.IN_PROGRESS}

    # DONE 是终态，无可流转目标
    assert LEGAL_STATUS_TRANSITIONS[StageStatus.DONE] == set()
