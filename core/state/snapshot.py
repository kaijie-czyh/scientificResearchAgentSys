"""快照与快照存储。

每次状态流转产生一个 Snapshot，落盘为 JSON。
回滚时从快照恢复 ProjectSession 状态。

快照内容：
- snapshot_id（uuid）
- project_id
- transition: 触发该快照的流转
- stage_states: 该时刻所有阶段的状态快照
- artifact_versions: 该时刻各产出物的版本号
- context_ref: 上下文摘要的引用（避免快照过大）

存储布局（每个项目独立目录）：
    projects/{project_id}/snapshots/
        ├── index.json                 # 快照索引（按时间排序）
        └── {snapshot_id}.json         # 单个快照
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.state.lifecycle import LifecycleStage, StageStatus
from core.state.transition import TransitionResult


@dataclass(frozen=True)
class StageState:
    """单个阶段在某时刻的状态。"""

    stage: LifecycleStage
    status: StageStatus
    last_updated: datetime
    # 该阶段最新产出物的版本号（artifact_id -> version）
    artifact_versions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """一次状态流转的快照。"""

    snapshot_id: str
    project_id: str
    transition: TransitionResult
    stage_states: dict[LifecycleStage, StageState]
    created_at: datetime
    # 父快照 ID，构成快照链（用于回滚路径追溯）
    parent_snapshot_id: Optional[str] = None
    # 自由备注（人工触发时填写）
    note: str = ""

    def to_dict(self) -> dict:
        """序列化为可 JSON 化的 dict。"""
        return {
            "snapshot_id": self.snapshot_id,
            "project_id": self.project_id,
            "transition": {
                "from_stage": self.transition.from_stage.value,
                "from_status": self.transition.from_status.value,
                "to_stage": self.transition.to_stage.value,
                "to_status": self.transition.to_status.value,
                "timestamp": self.transition.timestamp.isoformat(),
                "reason": self.transition.reason,
                "triggered_by": self.transition.triggered_by,
            },
            "stage_states": {
                stage.value: {
                    "stage": ss.stage.value,
                    "status": ss.status.value,
                    "last_updated": ss.last_updated.isoformat(),
                    "artifact_versions": ss.artifact_versions,
                }
                for stage, ss in self.stage_states.items()
            },
            "created_at": self.created_at.isoformat(),
            "parent_snapshot_id": self.parent_snapshot_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        """从 dict 反序列化。"""
        t = d["transition"]
        transition = TransitionResult(
            from_stage=LifecycleStage(t["from_stage"]),
            from_status=StageStatus(t["from_status"]),
            to_stage=LifecycleStage(t["to_stage"]),
            to_status=StageStatus(t["to_status"]),
            timestamp=datetime.fromisoformat(t["timestamp"]),
            reason=t["reason"],
            triggered_by=t["triggered_by"],
        )
        stage_states = {
            LifecycleStage(k): StageState(
                stage=LifecycleStage(v["stage"]),
                status=StageStatus(v["status"]),
                last_updated=datetime.fromisoformat(v["last_updated"]),
                artifact_versions=v["artifact_versions"],
            )
            for k, v in d["stage_states"].items()
        }
        return cls(
            snapshot_id=d["snapshot_id"],
            project_id=d["project_id"],
            transition=transition,
            stage_states=stage_states,
            created_at=datetime.fromisoformat(d["created_at"]),
            parent_snapshot_id=d.get("parent_snapshot_id"),
            note=d.get("note", ""),
        )


class SnapshotStore:
    """快照存储。每个项目独立目录。"""

    def __init__(self, snapshots_dir: Path):
        self._dir = snapshots_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _snapshot_path(self, snapshot_id: str) -> Path:
        return self._dir / f"{snapshot_id}.json"

    def _index_path(self) -> Path:
        return self._dir / "index.json"

    def save(self, snapshot: Snapshot) -> None:
        """保存快照。同时更新索引。"""
        # 写单快照文件
        path = self._snapshot_path(snapshot.snapshot_id)
        path.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 更新索引
        index = self._load_index()
        index.append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "created_at": snapshot.created_at.isoformat(),
                "to_stage": snapshot.transition.to_stage.value,
                "to_status": snapshot.transition.to_status.value,
                "reason": snapshot.transition.reason,
                "parent_snapshot_id": snapshot.parent_snapshot_id,
            }
        )
        self._save_index(index)

    def load(self, snapshot_id: str) -> Snapshot:
        """加载单个快照。"""
        path = self._snapshot_path(snapshot_id)
        if not path.exists():
            raise FileNotFoundError(f"快照不存在: {snapshot_id}")
        return Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_snapshots(self) -> list[dict]:
        """列出所有快照索引（按时间升序）。"""
        return self._load_index()

    def latest_snapshot_id(self) -> Optional[str]:
        """获取最新快照 ID。"""
        index = self._load_index()
        return index[-1]["snapshot_id"] if index else None

    def _load_index(self) -> list[dict]:
        path = self._index_path()
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_index(self, index: list[dict]) -> None:
        path = self._index_path()
        path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def make_snapshot(
    project_id: str,
    transition: TransitionResult,
    stage_states: dict[LifecycleStage, StageState],
    parent_snapshot_id: Optional[str] = None,
    note: str = "",
) -> Snapshot:
    """构造快照的工厂函数。"""
    return Snapshot(
        snapshot_id=uuid.uuid4().hex,
        project_id=project_id,
        transition=transition,
        stage_states=stage_states,
        created_at=datetime.utcnow(),
        parent_snapshot_id=parent_snapshot_id,
        note=note,
    )
