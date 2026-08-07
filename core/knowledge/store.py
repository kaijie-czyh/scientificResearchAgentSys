"""知识库存储层。

使用 SQLite + JSON 持久化。每个项目独立 .db 文件。

表结构：
- papers / ideas / claims / experiments / artifacts: 实体表
  （实体 JSON 存于 content 字段，便于 schema 演化）
- paper_chunks: 论文 chunk 表（用于全文检索）
- relations: 关系表（实体间 DAG）

设计权衡：
- 不用 ORM（SQLAlchemy）以减少依赖与魔法，直接用 sqlite3
- 实体字段以 JSON 存储，便于 Pydantic schema 演化
- 关系单独表，便于图查询与拓扑校验
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Optional

from core.knowledge.schema import (
    Artifact,
    ArtifactType,
    Claim,
    ClaimStatus,
    EntityId,
    EntityType,
    Experiment,
    Idea,
    Material,
    MaterialProperty,
    MaterialSynthesis,
    Paper,
    PaperChunk,
    Relation,
    RelationType,
    ResearchGap,
    ResearchConflict,
)


class StoreError(Exception):
    """知识库存储错误。"""


logger = logging.getLogger(__name__)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_chunks (
    chunk_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    page INTEGER,
    FOREIGN KEY (paper_id) REFERENCES papers(paper_id)
);
CREATE INDEX IF NOT EXISTS idx_chunks_paper ON paper_chunks(paper_id);

CREATE TABLE IF NOT EXISTS ideas (
    idea_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    source_idea_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_idea ON claims(source_idea_id);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_status ON experiments(status);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    artifact_group TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    artifact_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_art_group ON artifacts(artifact_group, version);

CREATE TABLE IF NOT EXISTS relations (
    relation_id TEXT PRIMARY KEY,
    relation_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target_id);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(relation_type);

CREATE TABLE IF NOT EXISTS evidence_log (
    log_id TEXT PRIMARY KEY,
    subquery TEXT NOT NULL,
    source TEXT NOT NULL,
    paper_id TEXT,
    title TEXT NOT NULL,
    external_id TEXT,
    offset INTEGER DEFAULT 0,
    evidence_score REAL DEFAULT 0.0,
    snippet TEXT,
    match_type TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evlog_paper ON evidence_log(paper_id);
CREATE INDEX IF NOT EXISTS idx_evlog_source ON evidence_log(source);

-- Task 2：材料知识抽取（材料-性能-合成三元组）
-- 实体以 JSON 存 content，便于 Pydantic schema 演化（与 papers 等一致）
CREATE TABLE IF NOT EXISTS materials (
    material_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_materials_created ON materials(created_at);

CREATE TABLE IF NOT EXISTS material_properties (
    property_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matprop_material ON material_properties(material_id);

CREATE TABLE IF NOT EXISTS material_synthesis (
    synthesis_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matsyn_material ON material_synthesis(material_id);

-- Task 3：研究缺口（Research Gap）识别结果
-- 结构化 Gap 清单（类型/证据链/可操作性/优先级），供 ideation/discovery/报告消费
CREATE TABLE IF NOT EXISTS research_gaps (
    gap_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gaps_created ON research_gaps(created_at);

-- 文献冲突（交叉验证产出落库）
-- 冲突陈述 + 立场证据（support/refute）+ 处置建议，供 Claim 冲突可视化
CREATE TABLE IF NOT EXISTS research_conflicts (
    conflict_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conflicts_created ON research_conflicts(created_at);
"""


class KnowledgeStore:
    """知识库存储。线程安全（每线程独立连接，写操作加锁）。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            # 轻量迁移：旧库 evidence_log 无 match_type 列时补列（不丢数据）
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(evidence_log)").fetchall()
            }
            if "match_type" not in cols:
                conn.execute(
                    "ALTER TABLE evidence_log ADD COLUMN match_type TEXT DEFAULT ''"
                )

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ===== Paper =====

    def save_paper(self, paper: Paper) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO papers (paper_id, content, created_at) VALUES (?, ?, ?)",
                (paper.paper_id, paper.model_dump_json(), paper.created_at.isoformat()),
            )

    def get_paper(self, paper_id: EntityId) -> Paper:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM papers WHERE paper_id = ?", (paper_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Paper 不存在: {paper_id}")
            return Paper.model_validate_json(row["content"])

    def list_papers(self) -> list[Paper]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM papers ORDER BY created_at"
            ).fetchall()
            return [Paper.model_validate_json(r["content"]) for r in rows]

    def find_paper_by_external_id(self, external_id: str) -> Optional[Paper]:
        """按外部 ID（doc_id / arxiv_id / s2 paperId）查找已入库论文，无则返回 None。"""
        key = (external_id or "").strip()
        if not key:
            return None
        with self._connect() as conn:
            rows = conn.execute("SELECT content FROM papers").fetchall()
            for r in rows:
                p = Paper.model_validate_json(r["content"])
                md = p.metadata or {}
                doc = (md.get("doc_id") or "").strip()
                ax = (p.arxiv_id or "").strip()
                if doc == key or ax == key:
                    return p
        return None

    def save_paper_chunks(self, chunks: list[PaperChunk]) -> None:
        if not chunks:
            return
        with self._lock, self._connect() as conn:
            for c in chunks:
                conn.execute(
                    "INSERT OR REPLACE INTO paper_chunks "
                    "(chunk_id, paper_id, chunk_index, text, page) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (c.chunk_id, c.paper_id, c.chunk_index, c.text, c.page),
                )

    def get_paper_chunks(self, paper_id: EntityId) -> list[PaperChunk]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chunk_id, paper_id, chunk_index, text, page "
                "FROM paper_chunks WHERE paper_id = ? ORDER BY chunk_index",
                (paper_id,),
            ).fetchall()
            return [
                PaperChunk(
                    chunk_id=r["chunk_id"],
                    paper_id=r["paper_id"],
                    chunk_index=r["chunk_index"],
                    text=r["text"],
                    page=r["page"],
                )
                for r in rows
            ]

    # ===== Idea =====

    def save_idea(self, idea: Idea) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ideas (idea_id, content, created_at) VALUES (?, ?, ?)",
                (idea.idea_id, idea.model_dump_json(), idea.created_at.isoformat()),
            )

    def get_idea(self, idea_id: EntityId) -> Idea:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM ideas WHERE idea_id = ?", (idea_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Idea 不存在: {idea_id}")
            return Idea.model_validate_json(row["content"])

    def list_ideas(self, status: Optional[str] = None) -> list[Idea]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT content FROM ideas WHERE json_extract(content, '$.status') = ? "
                    "ORDER BY created_at",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT content FROM ideas ORDER BY created_at"
                ).fetchall()
            return [Idea.model_validate_json(r["content"]) for r in rows]

    # ===== Claim =====

    def save_claim(self, claim: Claim) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO claims "
                "(claim_id, content, status, source_idea_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    claim.claim_id,
                    claim.model_dump_json(),
                    claim.status.value,
                    claim.source_idea_id,
                    claim.created_at.isoformat(),
                ),
            )

    def get_claim(self, claim_id: EntityId) -> Claim:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Claim 不存在: {claim_id}")
            return Claim.model_validate_json(row["content"])

    def list_claims(self, status: Optional[ClaimStatus] = None) -> list[Claim]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT content FROM claims WHERE status = ? ORDER BY created_at",
                    (status.value,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT content FROM claims ORDER BY created_at"
                ).fetchall()
            return [Claim.model_validate_json(r["content"]) for r in rows]

    def claims_without_evidence(self) -> list[Claim]:
        """列出所有非 DRAFT 且无证据的 Claim（违反硬约束）。"""
        all_claims = self.list_claims()
        return [
            c for c in all_claims
            if c.status != ClaimStatus.DRAFT and not c.evidence_refs
        ]

    # ===== Experiment =====

    def save_experiment(self, exp: Experiment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiments "
                "(experiment_id, content, status, created_at) VALUES (?, ?, ?, ?)",
                (
                    exp.experiment_id,
                    exp.model_dump_json(),
                    exp.status.value,
                    exp.created_at.isoformat(),
                ),
            )

    def get_experiment(self, exp_id: EntityId) -> Experiment:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM experiments WHERE experiment_id = ?", (exp_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Experiment 不存在: {exp_id}")
            return Experiment.model_validate_json(row["content"])

    def list_experiments(self, status: Optional[str] = None) -> list[Experiment]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT content FROM experiments WHERE status = ? ORDER BY created_at",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT content FROM experiments ORDER BY created_at"
                ).fetchall()
            return [Experiment.model_validate_json(r["content"]) for r in rows]

    # ===== Artifact =====

    def save_artifact(self, artifact: Artifact) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO artifacts "
                "(artifact_id, artifact_group, version, content, created_at, artifact_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    artifact.artifact_id,
                    artifact.artifact_group,
                    artifact.version,
                    artifact.model_dump_json(),
                    artifact.created_at.isoformat(),
                    artifact.artifact_type.value,
                ),
            )

    def get_artifact(self, artifact_id: EntityId) -> Artifact:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Artifact 不存在: {artifact_id}")
            return Artifact.model_validate_json(row["content"])

    def list_artifact_versions(self, group: EntityId) -> list[Artifact]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM artifacts WHERE artifact_group = ? ORDER BY version",
                (group,),
            ).fetchall()
            return [Artifact.model_validate_json(r["content"]) for r in rows]

    def latest_artifact_version(self, group: EntityId) -> Optional[Artifact]:
        versions = self.list_artifact_versions(group)
        return versions[-1] if versions else None

    def next_artifact_version(self, group: EntityId) -> int:
        latest = self.latest_artifact_version(group)
        return (latest.version + 1) if latest else 1

    # ===== Relation =====

    def save_relation(self, relation: Relation) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO relations "
                "(relation_id, relation_type, source_id, source_type, target_id, target_type, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    relation.relation_id,
                    relation.relation_type.value,
                    relation.source_id,
                    relation.source_type.value,
                    relation.target_id,
                    relation.target_type.value,
                    relation.created_at.isoformat(),
                    json.dumps(relation.metadata, ensure_ascii=False),
                ),
            )

    def list_relations(
        self,
        source_id: Optional[EntityId] = None,
        target_id: Optional[EntityId] = None,
        relation_type: Optional[RelationType] = None,
    ) -> list[Relation]:
        sql = "SELECT * FROM relations WHERE 1=1"
        params: list[Any] = []
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        if target_id:
            sql += " AND target_id = ?"
            params.append(target_id)
        if relation_type:
            sql += " AND relation_type = ?"
            params.append(relation_type.value)
        sql += " ORDER BY created_at"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [
                Relation(
                    relation_id=r["relation_id"],
                    relation_type=RelationType(r["relation_type"]),
                    source_id=r["source_id"],
                    source_type=EntityType(r["source_type"]),
                    target_id=r["target_id"],
                    target_type=EntityType(r["target_type"]),
                    created_at=datetime.fromisoformat(r["created_at"]),
                    metadata=json.loads(r["metadata"]) if r["metadata"] else {},
                )
                for r in rows
            ]

    # ===== Material（材料知识抽取：材料-性能-合成三元组）=====

    def save_material(self, material: Material) -> None:
        """保存/更新材料实体。按归一化名查重，重复则合并 paper 来源。"""
        norm = material.norm_name or material.name.strip().lower()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM materials WHERE "
                "json_extract(content, '$.norm_name') = ?",
                (norm,),
            ).fetchall()
            if rows:
                # 已有同名材料：合并 paper_id 来源（跨文献实体链接）
                existing = Material.model_validate_json(rows[0]["content"])
                if material.paper_id and existing.paper_id != material.paper_id:
                    existing.metadata = {
                        **existing.metadata,
                        "source_paper_ids": list(
                            dict.fromkeys(
                                existing.metadata.get("source_paper_ids", [])
                                + [material.paper_id]
                            )
                        ),
                    }
                material = existing
            material.material_id = material.material_id or self.new_id()
            conn.execute(
                "INSERT OR REPLACE INTO materials (material_id, content, created_at) "
                "VALUES (?, ?, ?)",
                (
                    material.material_id,
                    material.model_dump_json(),
                    material.created_at.isoformat(),
                ),
            )

    def get_material(self, material_id: EntityId) -> Optional[Material]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM materials WHERE material_id = ?",
                (material_id,),
            ).fetchone()
            return Material.model_validate_json(row["content"]) if row else None

    def list_materials(self, limit: int = 500) -> list[Material]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM materials ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [Material.model_validate_json(r["content"]) for r in rows]

    def save_material_property(self, prop: MaterialProperty) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO material_properties "
                "(property_id, material_id, content, created_at) VALUES (?, ?, ?, ?)",
                (
                    prop.property_id,
                    prop.material_id,
                    prop.model_dump_json(),
                    prop.created_at.isoformat(),
                ),
            )

    def list_material_properties(
        self, material_id: Optional[EntityId] = None, limit: int = 1000
    ) -> list[MaterialProperty]:
        with self._connect() as conn:
            if material_id:
                rows = conn.execute(
                    "SELECT content FROM material_properties WHERE material_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (material_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT content FROM material_properties "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [MaterialProperty.model_validate_json(r["content"]) for r in rows]

    def save_material_synthesis(self, syn: MaterialSynthesis) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO material_synthesis "
                "(synthesis_id, material_id, content, created_at) VALUES (?, ?, ?, ?)",
                (
                    syn.synthesis_id,
                    syn.material_id,
                    syn.model_dump_json(),
                    syn.created_at.isoformat(),
                ),
            )

    def list_material_synthesis(
        self, material_id: Optional[EntityId] = None, limit: int = 1000
    ) -> list[MaterialSynthesis]:
        with self._connect() as conn:
            if material_id:
                rows = conn.execute(
                    "SELECT content FROM material_synthesis WHERE material_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (material_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT content FROM material_synthesis "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [MaterialSynthesis.model_validate_json(r["content"]) for r in rows]

    def _reset_material_tables(self) -> None:
        """清空材料三表（仅供验证/重跑场景，生产勿用）。"""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM material_synthesis")
            conn.execute("DELETE FROM material_properties")
            conn.execute("DELETE FROM materials")

    def material_stats(self) -> dict:
        """材料知识统计：材料数 / 性能数 / 合成方法数 / 各材料关联度。"""
        with self._connect() as conn:
            n_mat = conn.execute("SELECT COUNT(*) AS c FROM materials").fetchone()["c"]
            n_prop = conn.execute(
                "SELECT COUNT(*) AS c FROM material_properties"
            ).fetchone()["c"]
            n_syn = conn.execute(
                "SELECT COUNT(*) AS c FROM material_synthesis"
            ).fetchone()["c"]
            # 每种材料的三元组完整性（材料+性能+合成）
            full = conn.execute(
                "SELECT m.material_id FROM materials m "
                "WHERE EXISTS (SELECT 1 FROM material_properties p "
                "              WHERE p.material_id = m.material_id) "
                "AND EXISTS (SELECT 1 FROM material_synthesis s "
                "            WHERE s.material_id = m.material_id)"
            ).fetchall()
        return {
            "materials": n_mat,
            "properties": n_prop,
            "synthesis": n_syn,
            "complete_triples": len(full),
        }

    # ===== Research Gap（Task 3：研究缺口识别）=====

    def save_research_gap(self, gap: ResearchGap) -> None:
        """保存一条研究缺口。按 gap_id 幂等覆盖（重跑不产生重复）。"""
        gap.gap_id = gap.gap_id or self.new_id()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO research_gaps "
                "(gap_id, content, created_at) VALUES (?, ?, ?)",
                (
                    gap.gap_id,
                    gap.model_dump_json(),
                    gap.created_at.isoformat(),
                ),
            )

    def save_research_gaps(self, gaps: list[ResearchGap]) -> int:
        """批量保存研究缺口，返回保存条数。"""
        n = 0
        for g in gaps:
            try:
                self.save_research_gap(g)
                n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("Research Gap 落库失败（gap_id=%r）: %s", g.gap_id, e)
        return n

    def list_research_gaps(self, limit: int = 200) -> list[ResearchGap]:
        """列出全部研究缺口（按优先级升序、创建时间降序）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM research_gaps "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        gaps = [ResearchGap.model_validate_json(r["content"]) for r in rows]
        # 优先级 1 最高在前，其次按创建时间
        gaps.sort(key=lambda g: (g.priority, -g.created_at.timestamp()))
        return gaps

    def clear_research_gaps(self) -> None:
        """清空研究缺口表（仅供验证/重跑场景，生产勿用）。"""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM research_gaps")

    def gap_stats(self) -> dict:
        """研究缺口统计：总数 / 按类型分布。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM research_gaps"
            ).fetchall()
        gaps = [ResearchGap.model_validate_json(r["content"]) for r in rows]
        by_type: dict[str, int] = {}
        for g in gaps:
            by_type[g.gap_type] = by_type.get(g.gap_type, 0) + 1
        return {
            "total": len(gaps),
            "by_type": by_type,
        }

    # ===== Research Conflict（文献冲突，交叉验证落库）=====

    def save_research_conflict(self, conflict: ResearchConflict) -> None:
        """保存一条文献冲突（幂等：同 conflict_id 覆盖）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO research_conflicts "
                "(conflict_id, content, created_at) VALUES (?, ?, ?)",
                (
                    conflict.conflict_id,
                    conflict.model_dump_json(),
                    conflict.created_at.isoformat(),
                ),
            )

    def save_research_conflicts(self, conflicts: list[ResearchConflict]) -> int:
        """批量保存文献冲突，返回成功条数。"""
        n = 0
        for c in conflicts:
            try:
                self.save_research_conflict(c)
                n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("Research Conflict 落库失败（conflict_id=%r）: %s", c.conflict_id, e)
        return n

    def list_research_conflicts(self, limit: int = 200) -> list[ResearchConflict]:
        """列出全部文献冲突（按创建时间降序）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM research_conflicts "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ResearchConflict.model_validate_json(r["content"]) for r in rows]

    def clear_research_conflicts(self) -> None:
        """清空文献冲突表（仅供验证/重跑场景，生产勿用）。"""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM research_conflicts")

    def conflict_stats(self) -> dict:
        """文献冲突统计：总数 / 涉及论文数。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT content FROM research_conflicts").fetchall()
        conflicts = [ResearchConflict.model_validate_json(r["content"]) for r in rows]
        papers: set[str] = set()
        for c in conflicts:
            for s in c.sources:
                if s.get("paper_id"):
                    papers.add(s["paper_id"])
        return {"total": len(conflicts), "papers": len(papers)}

    def conflicts_for_claim(self, evidence_refs: list[dict]) -> list[ResearchConflict]:
        """找出与某 Claim 相关的冲突（争议标记）。

        关联规则：conflict.sources 的 paper_id 与 claim.evidence_refs 中
        type=paper 的 id 有交集 → 该 Claim 处于争议中。
        返回按 confidence 降序的冲突列表。
        """
        claim_papers = {
            r.get("id") for r in (evidence_refs or [])
            if r.get("type") == "paper" and r.get("id")
        }
        if not claim_papers:
            return []
        conflicts = self.list_research_conflicts(limit=500)
        hits: list[ResearchConflict] = []
        for c in conflicts:
            src_papers = {s.get("paper_id") for s in c.sources if s.get("paper_id")}
            if src_papers & claim_papers:
                hits.append(c)
        hits.sort(key=lambda c: -c.confidence)
        return hits

    def conflicts_for_paper(self, paper_id: str) -> list[ResearchConflict]:
        """找出与某篇论文相关的全部冲突（论文页溯源用）。"""
        if not paper_id:
            return []
        conflicts = self.list_research_conflicts(limit=500)
        return [
            c for c in conflicts
            if any(s.get("paper_id") == paper_id for s in c.sources)
        ]

    # ===== Evidence Log（检索证据链，可审计轨迹）=====

    def log_evidence(self, entry: dict) -> None:
        """记录一条检索证据链条目（query → source → 命中 → paper 关联）。

        entry 字段：
        - subquery: 触发检索的子问题
        - source: 数据源（sciverse / arxiv / s2）
        - paper_id: 命中论文的入库 ID（未入库可为空，表示检索命中但被筛选剔除）
        - title: 命中论文/证据标题
        - external_id: 外部 ID（sciverse doc_id / arxiv_id / s2 paperId）
        - offset: Sciverse 原文偏移（证据回读定位）
        - evidence_score: Sciverse 证据相关性分数
        - snippet: 证据片段/摘要（截断）
        - match_type: 关联论文的量化依据（doc_id 精确 / arxiv_id 精确 / title 一致 / 空=未关联）
        - created_at: 可选，默认当前 UTC 时间
        """
        log_id = entry.get("log_id") or self.new_id()
        created_at = entry.get("created_at") or datetime.utcnow().isoformat()
        snippet = (entry.get("snippet") or "")[:800]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evidence_log "
                "(log_id, subquery, source, paper_id, title, external_id, offset, "
                " evidence_score, snippet, match_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    log_id,
                    entry.get("subquery", ""),
                    entry.get("source", ""),
                    entry.get("paper_id"),
                    entry.get("title", ""),
                    entry.get("external_id"),
                    int(entry.get("offset", 0) or 0),
                    float(entry.get("evidence_score", 0.0) or 0.0),
                    snippet,
                    entry.get("match_type", ""),
                    created_at,
                ),
            )

    def list_evidence(
        self,
        paper_id: Optional[EntityId] = None,
        limit: int = 200,
    ) -> list[dict]:
        """按论文过滤（或全部）列出证据链条目，最新的在前。"""
        with self._connect() as conn:
            if paper_id:
                rows = conn.execute(
                    "SELECT * FROM evidence_log WHERE paper_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (paper_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM evidence_log ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def evidence_stats(self) -> dict:
        """证据链统计：总量 + 按数据源分布 + 已入库占比。"""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence_log"
            ).fetchone()["c"]
            by_source = {
                r["source"]: r["c"]
                for r in conn.execute(
                    "SELECT source, COUNT(*) AS c FROM evidence_log GROUP BY source"
                ).fetchall()
            }
            linked = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence_log WHERE paper_id IS NOT NULL"
            ).fetchone()["c"]
        return {
            "total": total,
            "by_source": by_source,
            "linked": linked,
        }

    def list_unlinked_evidence(self, limit: int = 200) -> list[dict]:
        """列出未关联论文的证据链条目（检索命中但被筛选/去重剔除的候选）。

        这些条目保留完整检索元数据（title/external_id/snippet/subquery/score），
        前端可展示为「未入库论文」候选，支持用户手动补录入库。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence_log WHERE paper_id IS NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def link_evidence_to_paper(
        self, log_id: str, paper_id: str, match_type: str = "manual import"
    ) -> None:
        """将某条未关联证据回填关联到指定论文（手动补录入库时调用）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE evidence_log SET paper_id = ?, match_type = ? "
                "WHERE log_id = ?",
                (paper_id, match_type, log_id),
            )

    # ===== 工具 =====

    @staticmethod
    def new_id() -> EntityId:
        return uuid.uuid4().hex
