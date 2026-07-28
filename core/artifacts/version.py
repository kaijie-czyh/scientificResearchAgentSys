"""产出物版本管理。

ArtifactManager 封装：
- 创建新产出物（含新 group）
- 在已有 group 下创建新版本
- 大型内容存文件（ArtifactContentStore），小内容存数据库
- 版本历史查询

存储布局（每个项目独立目录）：
    projects/{project_id}/artifacts/
        └── {artifact_group}/
            ├── v1.json     # 版本元数据（与数据库冗余，便于人工查阅）
            ├── v1.txt      # 文本内容（若 content 非空）
            └── v1.pdf      # 二进制内容（若 content_path 指向）
"""
from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.knowledge.schema import (
    Artifact,
    ArtifactType,
    EntityId,
)
from core.knowledge.store import KnowledgeStore


class ArtifactVersionError(Exception):
    """产出物版本错误。"""


class ArtifactContentStore:
    """产出物内容文件存储。"""

    def __init__(self, artifacts_dir: Path):
        self._root = artifacts_dir
        self._root.mkdir(parents=True, exist_ok=True)

    def group_dir(self, artifact_group: EntityId) -> Path:
        d = self._root / artifact_group
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_text_content(
        self,
        artifact_group: EntityId,
        version: int,
        content: str,
        suffix: str = ".txt",
    ) -> Path:
        """写入文本内容，返回路径。"""
        path = self.group_dir(artifact_group) / f"v{version}{suffix}"
        path.write_text(content, encoding="utf-8")
        return path

    def write_binary_content(
        self,
        artifact_group: EntityId,
        version: int,
        content: bytes,
        suffix: str = ".bin",
    ) -> Path:
        """写入二进制内容，返回路径。"""
        path = self.group_dir(artifact_group) / f"v{version}{suffix}"
        path.write_bytes(content)
        return path

    def import_file(
        self,
        artifact_group: EntityId,
        version: int,
        src_path: Path,
        suffix: Optional[str] = None,
    ) -> Path:
        """从外部文件导入内容到 artifact 目录。"""
        suffix = suffix or src_path.suffix or ".bin"
        dst = self.group_dir(artifact_group) / f"v{version}{suffix}"
        shutil.copy2(src_path, dst)
        return dst

    def read_text(self, artifact_group: EntityId, version: int, suffix: str = ".txt") -> str:
        path = self.group_dir(artifact_group) / f"v{version}{suffix}"
        return path.read_text(encoding="utf-8")

    def content_path(
        self, artifact_group: EntityId, version: int, suffix: str = ".txt"
    ) -> Path:
        return self.group_dir(artifact_group) / f"v{version}{suffix}"


class ArtifactManager:
    """产出物管理器。

    结合 KnowledgeStore（元数据）与 ArtifactContentStore（内容文件）。
    """

    def __init__(
        self,
        store: KnowledgeStore,
        content_store: ArtifactContentStore,
    ):
        self._store = store
        self._content = content_store

    # ===== 创建 =====

    def create_artifact(
        self,
        artifact_type: ArtifactType,
        title: str,
        content: Optional[str] = None,
        content_path: Optional[Path] = None,
        cites_claim_ids: Optional[list[EntityId]] = None,
        cites_experiment_ids: Optional[list[EntityId]] = None,
        source_stage: str = "",
        created_by: str = "",
    ) -> Artifact:
        """创建新产出物（新 group，v1）。"""
        artifact_group = KnowledgeStore.new_id()
        return self._create_version(
            artifact_group=artifact_group,
            version=1,
            artifact_type=artifact_type,
            title=title,
            content=content,
            content_path=content_path,
            cites_claim_ids=cites_claim_ids or [],
            cites_experiment_ids=cites_experiment_ids or [],
            source_stage=source_stage,
            created_by=created_by,
            parent_version_id=None,
        )

    def new_version(
        self,
        artifact_group: EntityId,
        content: Optional[str] = None,
        content_path: Optional[Path] = None,
        title: Optional[str] = None,
        cites_claim_ids: Optional[list[EntityId]] = None,
        cites_experiment_ids: Optional[list[EntityId]] = None,
        created_by: str = "",
    ) -> Artifact:
        """在已有 group 下创建新版本。

        继承前一版本的 artifact_type 与 source_stage（若未指定）。
        """
        versions = self._store.list_artifact_versions(artifact_group)
        if not versions:
            raise ArtifactVersionError(
                f"artifact_group {artifact_group} 不存在，无法创建新版本"
            )
        latest = versions[-1]
        new_version_num = latest.version + 1

        return self._create_version(
            artifact_group=artifact_group,
            version=new_version_num,
            artifact_type=latest.artifact_type,
            title=title or latest.title,
            content=content,
            content_path=content_path,
            cites_claim_ids=cites_claim_ids
            if cites_claim_ids is not None
            else latest.cites_claim_ids,
            cites_experiment_ids=cites_experiment_ids
            if cites_experiment_ids is not None
            else latest.cites_experiment_ids,
            source_stage=latest.source_stage,
            created_by=created_by,
            parent_version_id=latest.artifact_id,
        )

    def _create_version(
        self,
        artifact_group: EntityId,
        version: int,
        artifact_type: ArtifactType,
        title: str,
        content: Optional[str],
        content_path: Optional[Path],
        cites_claim_ids: list[EntityId],
        cites_experiment_ids: list[EntityId],
        source_stage: str,
        created_by: str,
        parent_version_id: Optional[EntityId],
    ) -> Artifact:
        artifact_id = KnowledgeStore.new_id()

        # 处理内容存储
        final_content: Optional[str] = None
        final_content_path: Optional[str] = None

        if content is not None:
            # 文本内容：小于阈值直接存数据库，否则存文件
            if len(content) < 8192:
                final_content = content
            else:
                # 推断后缀
                if artifact_type == ArtifactType.FORMULA:
                    suffix = ".tex"
                elif artifact_type == ArtifactType.PAPER_DRAFT:
                    suffix = ".md"
                elif artifact_type == ArtifactType.DIAGRAM:
                    suffix = ".mmd"
                else:
                    suffix = ".txt"
                path = self._content.write_text_content(
                    artifact_group, version, content, suffix=suffix
                )
                final_content_path = str(path)

        if content_path is not None:
            # 外部文件导入
            suffix = content_path.suffix or ".bin"
            imported = self._content.import_file(
                artifact_group, version, content_path, suffix=suffix
            )
            final_content_path = str(imported)

        artifact = Artifact(
            artifact_id=artifact_id,
            artifact_group=artifact_group,
            version=version,
            artifact_type=artifact_type,
            title=title,
            content=final_content,
            content_path=final_content_path,
            cites_claim_ids=cites_claim_ids,
            cites_experiment_ids=cites_experiment_ids,
            source_stage=source_stage,
            created_by=created_by,
            created_at=datetime.utcnow(),
            parent_version_id=parent_version_id,
        )
        self._store.save_artifact(artifact)
        return artifact

    # ===== 查询 =====

    def get(self, artifact_id: EntityId) -> Artifact:
        return self._store.get_artifact(artifact_id)

    def list_versions(self, artifact_group: EntityId) -> list[Artifact]:
        return self._store.list_artifact_versions(artifact_group)

    def latest_version(self, artifact_group: EntityId) -> Optional[Artifact]:
        return self._store.latest_artifact_version(artifact_group)

    def read_content(self, artifact: Artifact) -> Optional[str]:
        """读取产出物文本内容。

        优先从 content 字段读，否则从 content_path 文件读。
        """
        if artifact.content is not None:
            return artifact.content
        if artifact.content_path:
            path = Path(artifact.content_path)
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None
