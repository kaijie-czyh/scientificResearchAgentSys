"""知识库检索器。

封装向量检索 + 实体查询的高层接口，供 stages 中 Agent 调用。

核心检索场景：
1. 文献语义检索：自然语言 → Paper chunks（带 paper 元数据）
2. Claim 证据查询：Claim → 关联 Paper/Experiment
3. 溯源链查询：Artifact → Claim → Paper/Experiment
4. 跨实体关联查询：Idea → 派生 Claim → 验证 Experiment
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from core.knowledge.schema import (
    Artifact,
    Claim,
    EntityId,
    Experiment,
    Idea,
    Paper,
    PaperChunk,
    Relation,
    RelationType,
)
from core.knowledge.store import KnowledgeStore
from core.knowledge.vector_store import EmbeddingFn, VectorStore


@dataclass(frozen=True)
class RetrievalResult:
    """检索结果。统一格式，便于 Agent 消费。"""

    query: str
    paper_chunks: list[PaperChunk]  # 命中的文献片段
    papers: list[Paper]             # 对应的文献实体
    scores: list[float]             # 每条命中的相似度分数
    related_claims: list[Claim]     # 关联的 Claim（若 chunk 已被引证）
    related_experiments: list[Experiment]  # 关联的实验


class Retriever:
    """知识库检索器。

    依赖：
    - KnowledgeStore: 实体与关系查询
    - VectorStore: 向量检索
    - EmbeddingFn: 查询文本向量化（由 LLM 适配层注入）
    """

    def __init__(
        self,
        store: KnowledgeStore,
        vector_store: VectorStore,
        embedding_fn: EmbeddingFn,
    ):
        self._store = store
        self._vector_store = vector_store
        self._embedding_fn = embedding_fn

    # ===== 文献语义检索 =====

    def search_papers(
        self,
        query: str,
        top_k: int = 5,
        paper_id_filter: Optional[EntityId] = None,
    ) -> RetrievalResult:
        """语义检索文献 chunk。返回 RetrievalResult（含关联实体）。"""
        # 查询向量化
        embeddings = self._embedding_fn([query])
        query_embedding = embeddings[0]

        # 向量检索
        hits = self._vector_store.query(
            query_embedding=query_embedding,
            top_k=top_k,
            paper_id_filter=paper_id_filter,
        )

        # 拼装 PaperChunk
        chunks: list[PaperChunk] = []
        papers: list[Paper] = []
        scores: list[float] = []
        seen_paper_ids: set[EntityId] = set()

        for hit in hits:
            # 取该 chunk 的完整记录
            paper_chunks = self._store.get_paper_chunks(hit.paper_id)
            target_chunk = next(
                (c for c in paper_chunks if c.chunk_id == hit.chunk_id), None
            )
            if target_chunk is None:
                # chunk 表中无记录（向量库与关系库不一致），构造一个最小 chunk
                target_chunk = PaperChunk(
                    chunk_id=hit.chunk_id,
                    paper_id=hit.paper_id,
                    chunk_index=-1,
                    text=hit.text,
                )
            chunks.append(target_chunk)
            scores.append(hit.score)
            # 加载 Paper（去重）
            if hit.paper_id not in seen_paper_ids:
                try:
                    papers.append(self._store.get_paper(hit.paper_id))
                    seen_paper_ids.add(hit.paper_id)
                except Exception:
                    pass  # Paper 可能已被删除

        # 查找与这些 chunk 关联的 Claim
        related_claims = self._claims_citing_chunks([c.chunk_id for c in chunks])
        # 查找与这些 Claim 关联的 Experiment
        related_experiments = self._experiments_verifying_claims(
            [c.claim_id for c in related_claims]
        )

        return RetrievalResult(
            query=query,
            paper_chunks=chunks,
            papers=papers,
            scores=scores,
            related_claims=related_claims,
            related_experiments=related_experiments,
        )

    # ===== 溯源链查询 =====

    def trace_artifact_provenance(self, artifact_id: EntityId) -> dict[str, Any]:
        """追溯 Artifact 的完整溯源链。

        返回结构：
        {
            "artifact": Artifact,
            "claims": [Claim, ...],
            "experiments": [Experiment, ...],
            "papers": [Paper, ...],  # 所有 Claim 引证的 Paper
        }
        """
        artifact = self._store.get_artifact(artifact_id)

        # 直接引用的 Claim
        claims: list[Claim] = []
        for cid in artifact.cites_claim_ids:
            try:
                claims.append(self._store.get_claim(cid))
            except Exception:
                pass

        # 直接引用的 Experiment
        experiments: list[Experiment] = []
        for eid in artifact.cites_experiment_ids:
            try:
                experiments.append(self._store.get_experiment(eid))
            except Exception:
                pass

        # Claim 引证的 Paper
        papers: list[Paper] = []
        seen_paper_ids: set[EntityId] = set()
        for claim in claims:
            for ref in claim.evidence_refs:
                if ref["type"] == "paper" and ref["id"] not in seen_paper_ids:
                    try:
                        papers.append(self._store.get_paper(ref["id"]))
                        seen_paper_ids.add(ref["id"])
                    except Exception:
                        pass

        return {
            "artifact": artifact,
            "claims": claims,
            "experiments": experiments,
            "papers": papers,
        }

    # ===== 实体查询 =====

    def get_idea_claims(self, idea_id: EntityId) -> list[Claim]:
        """获取某 Idea 派生的所有 Claim。"""
        all_claims = self._store.list_claims()
        return [c for c in all_claims if c.source_idea_id == idea_id]

    def get_claim_evidence(self, claim_id: EntityId) -> dict[str, Any]:
        """获取 Claim 的所有证据（Paper + Experiment）。"""
        claim = self._store.get_claim(claim_id)
        papers: list[Paper] = []
        experiments: list[Experiment] = []
        for ref in claim.evidence_refs:
            try:
                if ref["type"] == "paper":
                    papers.append(self._store.get_paper(ref["id"]))
                elif ref["type"] == "experiment":
                    experiments.append(self._store.get_experiment(ref["id"]))
            except Exception:
                pass
        return {"claim": claim, "papers": papers, "experiments": experiments}

    def get_experiment_claims(self, experiment_id: EntityId) -> list[Claim]:
        """获取某实验验证的所有 Claim。"""
        all_claims = self._store.list_claims()
        return [
            c for c in all_claims
            if any(
                ref["type"] == "experiment" and ref["id"] == experiment_id
                for ref in c.evidence_refs
            )
        ]

    # ===== 内部辅助 =====

    def _claims_citing_chunks(self, chunk_ids: list[EntityId]) -> list[Claim]:
        """查找引证了这些 chunk 的 Claim。

        Claim 的 evidence_refs 中若 type=paper 且 chunk_id 匹配，则视为引证。
        """
        if not chunk_ids:
            return []
        all_claims = self._store.list_claims()
        chunk_id_set = set(chunk_ids)
        result: list[Claim] = []
        for claim in all_claims:
            for ref in claim.evidence_refs:
                if (
                    ref.get("type") == "paper"
                    and ref.get("chunk_id") in chunk_id_set
                ):
                    result.append(claim)
                    break
        return result

    def _experiments_verifying_claims(self, claim_ids: list[EntityId]) -> list[Experiment]:
        """查找验证这些 Claim 的实验。"""
        if not claim_ids:
            return []
        all_exps = self._store.list_experiments()
        claim_id_set = set(claim_ids)
        return [
            e for e in all_exps
            if any(cid in claim_id_set for cid in e.verifies_claim_ids)
        ]
