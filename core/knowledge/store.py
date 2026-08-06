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
    Paper,
    PaperChunk,
    Relation,
    RelationType,
)


class StoreError(Exception):
    """知识库存储错误。"""


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
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evlog_paper ON evidence_log(paper_id);
CREATE INDEX IF NOT EXISTS idx_evlog_source ON evidence_log(source);
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
        - created_at: 可选，默认当前 UTC 时间
        """
        log_id = entry.get("log_id") or self.new_id()
        created_at = entry.get("created_at") or datetime.utcnow().isoformat()
        snippet = (entry.get("snippet") or "")[:800]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evidence_log "
                "(log_id, subquery, source, paper_id, title, external_id, offset, "
                " evidence_score, snippet, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

    # ===== 工具 =====

    @staticmethod
    def new_id() -> EntityId:
        return uuid.uuid4().hex
