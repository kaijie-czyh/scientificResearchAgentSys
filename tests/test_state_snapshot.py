"""Snapshot / SnapshotStore / make_snapshot 测试。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from core.state.lifecycle import LifecycleStage, StageStatus
from core.state.snapshot import (
    Snapshot,
    SnapshotStore,
    StageState,
    make_snapshot,
)
from core.state.transition import TransitionResult


# ===== 辅助构造 =====


def _make_test_snapshot(
    project_id: str = "proj_test",
    parent_snapshot_id: str | None = None,
    note: str = "测试快照",
) -> Snapshot:
    """构造一个用于测试的快照。"""
    transition = TransitionResult(
        from_stage=LifecycleStage.RESEARCH,
        from_status=StageStatus.NOT_STARTED,
        to_stage=LifecycleStage.RESEARCH,
        to_status=StageStatus.IN_PROGRESS,
        timestamp=datetime.utcnow(),
        reason="启动调研",
        triggered_by="user",
    )
    stage_states = {
        LifecycleStage.RESEARCH: StageState(
            stage=LifecycleStage.RESEARCH,
            status=StageStatus.IN_PROGRESS,
            last_updated=datetime.utcnow(),
            artifact_versions={"art_1": "v1"},
        ),
        LifecycleStage.IDEATION: StageState(
            stage=LifecycleStage.IDEATION,
            status=StageStatus.NOT_STARTED,
            last_updated=datetime.utcnow(),
        ),
    }
    return make_snapshot(
        project_id=project_id,
        transition=transition,
        stage_states=stage_states,
        parent_snapshot_id=parent_snapshot_id,
        note=note,
    )


# ===== Snapshot 序列化 =====


def test_snapshot_to_dict_contains_required_fields():
    """to_dict 应包含 snapshot_id / project_id / transition / stage_states 等字段。"""
    # Arrange
    snapshot = _make_test_snapshot(project_id="proj_1", note="初始")

    # Act
    d = snapshot.to_dict()

    # Assert
    assert d["snapshot_id"] == snapshot.snapshot_id
    assert d["project_id"] == "proj_1"
    assert d["parent_snapshot_id"] is None
    assert d["note"] == "初始"
    assert "transition" in d
    assert "stage_states" in d
    assert "created_at" in d


def test_snapshot_to_dict_serializes_transition_and_stage_states():
    """to_dict 应把 transition 与 stage_states 序列化为可 JSON 化的结构。"""
    snapshot = _make_test_snapshot()

    d = snapshot.to_dict()

    # transition 字段
    t = d["transition"]
    assert t["from_stage"] == "research"
    assert t["from_status"] == "not_started"
    assert t["to_stage"] == "research"
    assert t["to_status"] == "in_progress"
    assert t["reason"] == "启动调研"
    assert t["triggered_by"] == "user"

    # stage_states 字段
    assert "research" in d["stage_states"]
    assert "ideation" in d["stage_states"]
    research_state = d["stage_states"]["research"]
    assert research_state["status"] == "in_progress"
    assert research_state["artifact_versions"] == {"art_1": "v1"}


def test_snapshot_round_trip_preserves_all_fields():
    """to_dict → from_dict 应保留所有字段。"""
    # Arrange
    original = _make_test_snapshot(
        project_id="proj_round",
        parent_snapshot_id="parent_001",
        note="round-trip 测试",
    )

    # Act
    restored = Snapshot.from_dict(original.to_dict())

    # Assert
    assert restored.snapshot_id == original.snapshot_id
    assert restored.project_id == "proj_round"
    assert restored.parent_snapshot_id == "parent_001"
    assert restored.note == "round-trip 测试"
    assert restored.transition.from_stage == original.transition.from_stage
    assert restored.transition.to_status == original.transition.to_status
    assert restored.transition.reason == original.transition.reason
    # stage_states 应保留所有阶段
    assert set(restored.stage_states.keys()) == set(original.stage_states.keys())
    research_state = restored.stage_states[LifecycleStage.RESEARCH]
    assert research_state.status == StageStatus.IN_PROGRESS
    assert research_state.artifact_versions == {"art_1": "v1"}


def test_snapshot_from_dict_handles_missing_note_field():
    """from_dict 应容忍缺失的 note 字段（默认空串）。"""
    snapshot = _make_test_snapshot()
    d = snapshot.to_dict()
    d.pop("note")

    restored = Snapshot.from_dict(d)
    assert restored.note == ""


# ===== make_snapshot 工厂 =====


def test_make_snapshot_generates_unique_snapshot_id():
    """make_snapshot 每次应生成不同的 snapshot_id。"""
    transition = TransitionResult(
        from_stage=LifecycleStage.RESEARCH,
        from_status=StageStatus.NOT_STARTED,
        to_stage=LifecycleStage.RESEARCH,
        to_status=StageStatus.IN_PROGRESS,
        timestamp=datetime.utcnow(),
        reason="测试",
        triggered_by="user",
    )
    s1 = make_snapshot(
        project_id="p1",
        transition=transition,
        stage_states={},
    )
    s2 = make_snapshot(
        project_id="p1",
        transition=transition,
        stage_states={},
    )
    assert s1.snapshot_id != s2.snapshot_id


def test_make_snapshot_default_parent_and_note():
    """make_snapshot 默认 parent_snapshot_id=None / note=''。"""
    transition = TransitionResult(
        from_stage=LifecycleStage.RESEARCH,
        from_status=StageStatus.NOT_STARTED,
        to_stage=LifecycleStage.RESEARCH,
        to_status=StageStatus.IN_PROGRESS,
        timestamp=datetime.utcnow(),
        reason="x",
        triggered_by="user",
    )
    snapshot = make_snapshot(
        project_id="p1",
        transition=transition,
        stage_states={},
    )
    assert snapshot.parent_snapshot_id is None
    assert snapshot.note == ""


# ===== SnapshotStore 持久化 =====


def test_snapshot_store_creates_dir_on_init(tmp_path: Path):
    """SnapshotStore 初始化时应创建目录。"""
    target = tmp_path / "snapshots"
    assert not target.exists()

    SnapshotStore(snapshots_dir=target)

    assert target.exists()


def test_snapshot_store_save_and_load_round_trip(tmp_path: Path):
    """save 后 load 应返回等价快照。"""
    store = SnapshotStore(snapshots_dir=tmp_path / "snapshots")
    snapshot = _make_test_snapshot(project_id="proj_save")

    store.save(snapshot)

    loaded = store.load(snapshot.snapshot_id)
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert loaded.project_id == "proj_save"
    assert loaded.transition.to_status == snapshot.transition.to_status


def test_snapshot_store_load_raises_on_missing_snapshot(tmp_path: Path):
    """load 不存在的快照应抛 FileNotFoundError。"""
    store = SnapshotStore(snapshots_dir=tmp_path / "snapshots")
    with pytest.raises(FileNotFoundError):
        store.load("nonexistent_snapshot_id")


def test_snapshot_store_list_snapshots_returns_empty_initially(tmp_path: Path):
    """新 store 的 list_snapshots 应返回空列表。"""
    store = SnapshotStore(snapshots_dir=tmp_path / "snapshots")
    assert store.list_snapshots() == []


def test_snapshot_store_list_snapshots_returns_sorted_records(tmp_path: Path):
    """保存多个快照后，list_snapshots 应按时间升序返回索引记录。"""
    store = SnapshotStore(snapshots_dir=tmp_path / "snapshots")

    s1 = _make_test_snapshot(note="first")
    s2 = _make_test_snapshot(note="second", parent_snapshot_id=s1.snapshot_id)
    s3 = _make_test_snapshot(note="third", parent_snapshot_id=s2.snapshot_id)
    store.save(s1)
    store.save(s2)
    store.save(s3)

    index = store.list_snapshots()
    assert len(index) == 3
    assert index[0]["snapshot_id"] == s1.snapshot_id
    assert index[1]["snapshot_id"] == s2.snapshot_id
    assert index[2]["snapshot_id"] == s3.snapshot_id
    # 每条索引记录应含必要字段
    assert "created_at" in index[0]
    assert "to_stage" in index[0]
    assert "to_status" in index[0]
    assert "reason" in index[0]


def test_snapshot_store_latest_snapshot_id_returns_last_saved(tmp_path: Path):
    """latest_snapshot_id 应返回最近保存的快照 ID。"""
    store = SnapshotStore(snapshots_dir=tmp_path / "snapshots")

    # 初始为空
    assert store.latest_snapshot_id() is None

    s1 = _make_test_snapshot()
    store.save(s1)
    assert store.latest_snapshot_id() == s1.snapshot_id

    s2 = _make_test_snapshot()
    store.save(s2)
    assert store.latest_snapshot_id() == s2.snapshot_id


def test_snapshot_store_persists_index_across_instances(tmp_path: Path):
    """重新打开同一目录的 SnapshotStore 应能读到之前的索引。"""
    store1 = SnapshotStore(snapshots_dir=tmp_path / "snapshots")
    s1 = _make_test_snapshot()
    store1.save(s1)

    # 新实例
    store2 = SnapshotStore(snapshots_dir=tmp_path / "snapshots")
    assert store2.latest_snapshot_id() == s1.snapshot_id
    assert len(store2.list_snapshots()) == 1
