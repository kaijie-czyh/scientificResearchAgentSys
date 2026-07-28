"""TransitionValidator 状态流转校验测试。

覆盖：
- validate_status_transition：合法/非法子状态流转
- validate_advance：前进流转
- validate_rollback：回滚流转
- make_transition：构造合法流转 / 非法流转抛异常
"""
from __future__ import annotations

from datetime import datetime

import pytest

from core.state.lifecycle import LifecycleStage, StageStatus
from core.state.transition import (
    TransitionError,
    TransitionResult,
    TransitionValidator,
)


# ===== validate_status_transition 合法路径 =====


def test_validate_status_transition_allows_not_started_to_in_progress():
    """NOT_STARTED → IN_PROGRESS 合法，不应抛异常。"""
    # Act + Assert: 不抛异常即通过
    TransitionValidator.validate_status_transition(
        LifecycleStage.RESEARCH,
        StageStatus.NOT_STARTED,
        StageStatus.IN_PROGRESS,
    )


def test_validate_status_transition_allows_in_progress_to_done():
    """IN_PROGRESS → DONE 合法。"""
    TransitionValidator.validate_status_transition(
        LifecycleStage.RESEARCH,
        StageStatus.IN_PROGRESS,
        StageStatus.DONE,
    )


def test_validate_status_transition_allows_pending_review_to_done():
    """PENDING_REVIEW → DONE 合法（审核通过）。"""
    TransitionValidator.validate_status_transition(
        LifecycleStage.RESEARCH,
        StageStatus.PENDING_REVIEW,
        StageStatus.DONE,
    )


def test_validate_status_transition_allows_blocked_to_in_progress():
    """BLOCKED → IN_PROGRESS 合法（解除阻塞）。"""
    TransitionValidator.validate_status_transition(
        LifecycleStage.RESEARCH,
        StageStatus.BLOCKED,
        StageStatus.IN_PROGRESS,
    )


# ===== validate_status_transition 非法路径 =====


def test_validate_status_transition_rejects_not_started_to_done():
    """NOT_STARTED → DONE 非法（跨子状态）。"""
    with pytest.raises(TransitionError):
        TransitionValidator.validate_status_transition(
            LifecycleStage.RESEARCH,
            StageStatus.NOT_STARTED,
            StageStatus.DONE,
        )


def test_validate_status_transition_rejects_done_to_in_progress():
    """DONE → IN_PROGRESS 非法（终态不可流转）。"""
    with pytest.raises(TransitionError):
        TransitionValidator.validate_status_transition(
            LifecycleStage.RESEARCH,
            StageStatus.DONE,
            StageStatus.IN_PROGRESS,
        )


def test_validate_status_transition_rejects_not_started_to_not_started():
    """NOT_STARTED → NOT_STARTED 非法（自流转不在合法集合中）。"""
    with pytest.raises(TransitionError):
        TransitionValidator.validate_status_transition(
            LifecycleStage.RESEARCH,
            StageStatus.NOT_STARTED,
            StageStatus.NOT_STARTED,
        )


def test_validate_status_transition_rejects_done_to_blocked():
    """DONE → BLOCKED 非法（终态）。"""
    with pytest.raises(TransitionError):
        TransitionValidator.validate_status_transition(
            LifecycleStage.RESEARCH,
            StageStatus.DONE,
            StageStatus.BLOCKED,
        )


# ===== validate_advance =====


def test_validate_advance_allows_done_to_next_stage():
    """DONE → next_stage 合法。"""
    TransitionValidator.validate_advance(
        LifecycleStage.RESEARCH,
        StageStatus.DONE,
        LifecycleStage.IDEATION,
    )


def test_validate_advance_rejects_non_done_current():
    """当前阶段非 DONE 时前进应抛异常。"""
    with pytest.raises(TransitionError):
        TransitionValidator.validate_advance(
            LifecycleStage.RESEARCH,
            StageStatus.IN_PROGRESS,
            LifecycleStage.IDEATION,
        )


def test_validate_advance_rejects_skipping_stage():
    """跨阶段前进（跳过下一阶段）应抛异常。"""
    with pytest.raises(TransitionError):
        TransitionValidator.validate_advance(
            LifecycleStage.RESEARCH,
            StageStatus.DONE,
            LifecycleStage.DESIGN,  # 跳过 ideation
        )


def test_validate_advance_rejects_advancing_from_last_stage():
    """从最后阶段前进应抛异常（无 next_stage）。"""
    with pytest.raises(TransitionError):
        TransitionValidator.validate_advance(
            LifecycleStage.WRITING,
            StageStatus.DONE,
            LifecycleStage.WRITING,  # 无下一阶段
        )


# ===== validate_rollback =====


def test_validate_rollback_allows_to_earlier_stage():
    """回滚到前序阶段合法。"""
    TransitionValidator.validate_rollback(
        LifecycleStage.WRITING,
        LifecycleStage.RESEARCH,
    )
    TransitionValidator.validate_rollback(
        LifecycleStage.EXPERIMENT,
        LifecycleStage.IDEATION,
    )


def test_validate_rollback_rejects_to_same_stage():
    """回滚到同阶段非法。"""
    with pytest.raises(TransitionError):
        TransitionValidator.validate_rollback(
            LifecycleStage.WRITING,
            LifecycleStage.WRITING,
        )


def test_validate_rollback_rejects_to_later_stage():
    """回滚到后序阶段非法。"""
    with pytest.raises(TransitionError):
        TransitionValidator.validate_rollback(
            LifecycleStage.RESEARCH,
            LifecycleStage.WRITING,
        )


# ===== make_transition =====


def test_make_transition_returns_transition_result_for_legal_intra_stage():
    """合法阶段内流转应返回 TransitionResult。"""
    # Act
    result = TransitionValidator.make_transition(
        from_stage=LifecycleStage.RESEARCH,
        from_status=StageStatus.NOT_STARTED,
        to_stage=LifecycleStage.RESEARCH,
        to_status=StageStatus.IN_PROGRESS,
        reason="启动调研",
        triggered_by="user",
    )

    # Assert
    assert isinstance(result, TransitionResult)
    assert result.from_stage == LifecycleStage.RESEARCH
    assert result.from_status == StageStatus.NOT_STARTED
    assert result.to_stage == LifecycleStage.RESEARCH
    assert result.to_status == StageStatus.IN_PROGRESS
    assert result.reason == "启动调研"
    assert result.triggered_by == "user"
    assert isinstance(result.timestamp, datetime)


def test_make_transition_uses_default_timestamp_when_none():
    """timestamp 为 None 时应自动填充当前时间。"""
    result = TransitionValidator.make_transition(
        from_stage=LifecycleStage.RESEARCH,
        from_status=StageStatus.NOT_STARTED,
        to_stage=LifecycleStage.RESEARCH,
        to_status=StageStatus.IN_PROGRESS,
        reason="启动",
        triggered_by="user",
        timestamp=None,
    )
    assert result.timestamp is not None


def test_make_transition_allows_cross_stage_advance():
    """跨阶段前进流转应成功（DONE → next_stage, IN_PROGRESS）。"""
    result = TransitionValidator.make_transition(
        from_stage=LifecycleStage.RESEARCH,
        from_status=StageStatus.DONE,
        to_stage=LifecycleStage.IDEATION,
        to_status=StageStatus.IN_PROGRESS,
        reason="前进到 ideation",
        triggered_by="user",
    )
    assert result.to_stage == LifecycleStage.IDEATION
    assert result.to_status == StageStatus.IN_PROGRESS


def test_make_transition_allows_cross_stage_rollback():
    """跨阶段回滚流转应成功。"""
    result = TransitionValidator.make_transition(
        from_stage=LifecycleStage.EXPERIMENT,
        from_status=StageStatus.IN_PROGRESS,
        to_stage=LifecycleStage.DESIGN,
        to_status=StageStatus.IN_PROGRESS,
        reason="回滚到 design",
        triggered_by="user",
    )
    assert result.to_stage == LifecycleStage.DESIGN


def test_make_transition_rejects_cross_stage_to_invalid_status():
    """跨阶段流转后 to_status 必须是 NOT_STARTED / IN_PROGRESS，否则抛异常。"""
    with pytest.raises(TransitionError):
        TransitionValidator.make_transition(
            from_stage=LifecycleStage.RESEARCH,
            from_status=StageStatus.DONE,
            to_stage=LifecycleStage.IDEATION,
            to_status=StageStatus.DONE,  # 非法：跨阶段后不能直接到 DONE
            reason="跳过",
            triggered_by="user",
        )


def test_make_transition_rejects_illegal_intra_stage():
    """非法阶段内流转应抛 TransitionError。"""
    with pytest.raises(TransitionError):
        TransitionValidator.make_transition(
            from_stage=LifecycleStage.RESEARCH,
            from_status=StageStatus.NOT_STARTED,
            to_stage=LifecycleStage.RESEARCH,
            to_status=StageStatus.DONE,  # NOT_STARTED 不能直接到 DONE
            reason="跳过",
            triggered_by="user",
        )


def test_make_transition_rejects_illegal_advance():
    """非法前进（未 DONE）应抛异常。"""
    with pytest.raises(TransitionError):
        TransitionValidator.make_transition(
            from_stage=LifecycleStage.RESEARCH,
            from_status=StageStatus.IN_PROGRESS,
            to_stage=LifecycleStage.IDEATION,
            to_status=StageStatus.IN_PROGRESS,
            reason="未完成就前进",
            triggered_by="user",
        )
