"""Retriever 检索器测试。

覆盖：
- search_papers: 向量检索 + 关联实体拼装
- trace_artifact_provenance: Artifact 溯源链查询
- get_idea_claims / get_claim_evidence / get_experiment_claims
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from core.knowledge.retriever import RetrievalResult, Retriever
from core.knowledge.schema import (
    Artifact,
    ArtifactType,
    Claim,
    ClaimStatus,
    EntityType,
    Experiment,
    ExperimentStatus,
    Idea,
    Paper,
    PaperChunk,
    Relation,
    RelationType,
)
from core.knowledge.store import KnowledgeStore
from core.knowledge.vector_store import (
    ChromaVectorStore,
    VectorRecord,
    VectorStore,
)

# 检测 chromadb
chromadb_available = True
try:
    import chromadb  # noqa: F401
except ImportError:
    chromadb_available = False


class FakeVectorStore(VectorStore):
    """内存版 VectorStore，避免依赖 chromadb。

    维护一个 list[VectorRecord]，query 时按点积排序返回 top_k。
    """

    def __init__(self):
        self._records: list[VectorRecord] = []

    def add(self, records: list[VectorRecord]) -> None:
        # upsert 语义：相同 chunk_id 覆盖
        existing_ids = {r.chunk_id for r in self._records}
        for r in records:
            if r.chunk_id in existing_ids:
                self._records = [
                    r if r.chunk_id == rec.chunk_id else rec for rec in self._records
                ]
            else:
                self._records.append(r)

    def query(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        paper_id_filter: Optional[str] = None,
    ):
        # 简单按点积相似度排序
        candidates = self._records
        if paper_id_filter is not None:
            candidates = [r for r in candidates if r.paper_id == paper_id_filter]

        def dot(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b))

        scored = sorted(
            candidates,
            key=lambda r: dot(query_embedding, r.embedding),
            reverse=True,
        )
        return [
            type(
                "R",
                (),
                {
                    "chunk_id": r.chunk_id,
                    "paper_id": r.paper_id,
                    "text": r.text,
                    "score": 1.0,
                    "metadata": dict(r.metadata),
                },
            )()
            for r in scored[:top_k]
        ]

    def delete_by_paper(self, paper_id: str) -> int:
        before = len(self._records)
        self._records = [r for r in self._records if r.paper_id != paper_id]
        return before - len(self._records)

    def count(self) -> int:
        return len(self._records)


def _seed_papers_and_chunks(store: KnowledgeStore) -> dict[str, str]:
    """写入 2 篇 Paper + 各 2 个 chunk，返回 ID 字典。"""
    paper1 = Paper(paper_id="p1", title="Paper One", year=2024, abstract="摘要1")
    paper2 = Paper(paper_id="p2", title="Paper Two", year=2023, abstract="摘要2")
    store.save_paper(paper1)
    store.save_paper(paper2)

    chunks = [
        PaperChunk(chunk_id="chk1", paper_id="p1", chunk_index=0, text="chunk 1 of p1"),
        PaperChunk(chunk_id="chk2", paper_id="p1", chunk_index=1, text="chunk 2 of p1"),
        PaperChunk(chunk_id="chk3", paper_id="p2", chunk_index=0, text="chunk 3 of p2"),
        PaperChunk(chunk_id="chk4", paper_id="p2", chunk_index=1, text="chunk 4 of p2"),
    ]
    store.save_paper_chunks(chunks)
    return {"p1": "p1", "p2": "p2", "chk1": "chk1", "chk3": "chk3"}


# ===== search_papers =====


def test_search_papers_returns_chunks_papers_and_scores(
    knowledge_store: KnowledgeStore, fake_embedding_fn
):
    """search_papers 应返回 chunks / papers / scores。"""
    _seed_papers_and_chunks(knowledge_store)
    vector_store = FakeVectorStore()
    # 写入向量
    vector_store.add(
        [
            VectorRecord(
                chunk_id="chk1",
                paper_id="p1",
                text="chunk 1",
                embedding=fake_embedding_fn(["q"])[0],
                metadata={},
            ),
            VectorRecord(
                chunk_id="chk3",
                paper_id="p2",
                text="chunk 3",
                embedding=fake_embedding_fn(["q"])[0],
                metadata={},
            ),
        ]
    )

    retriever = Retriever(
        store=knowledge_store,
        vector_store=vector_store,
        embedding_fn=fake_embedding_fn,
    )

    result = retriever.search_papers(query="测试查询", top_k=5)

    assert isinstance(result, RetrievalResult)
    assert result.query == "测试查询"
    assert len(result.paper_chunks) >= 1
    assert len(result.papers) >= 1
    assert len(result.scores) == len(result.paper_chunks)


def test_search_papers_with_paper_id_filter(
    knowledge_store: KnowledgeStore, fake_embedding_fn
):
    """search_papers 加 paper_id_filter 应只返回该 paper 的 chunks。"""
    _seed_papers_and_chunks(knowledge_store)
    vector_store = FakeVectorStore()
    vector_store.add(
        [
            VectorRecord(
                chunk_id="chk1",
                paper_id="p1",
                text="chunk 1",
                embedding=[0.1] * 8,
                metadata={},
            ),
            VectorRecord(
                chunk_id="chk3",
                paper_id="p2",
                text="chunk 3",
                embedding=[0.1] * 8,
                metadata={},
            ),
        ]
    )

    retriever = Retriever(
        store=knowledge_store,
        vector_store=vector_store,
        embedding_fn=fake_embedding_fn,
    )

    result = retriever.search_papers(query="q", top_k=5, paper_id_filter="p1")
    assert all(c.paper_id == "p1" for c in result.paper_chunks)


# ===== trace_artifact_provenance =====


def test_trace_artifact_provenance_returns_full_chain(
    knowledge_store: KnowledgeStore,
):
    """trace_artifact_provenance 应返回 artifact / claims / experiments / papers。"""
    # 构造数据：Paper → Claim（带 evidence_refs） → Artifact（cites_claim_ids）
    paper = Paper(paper_id="p1", title="Source Paper", year=2024)
    knowledge_store.save_paper(paper)

    experiment = Experiment(
        experiment_id="e1",
        name="验证实验",
        status=ExperimentStatus.COMPLETED,
        verifies_claim_ids=["c1"],
    )
    knowledge_store.save_experiment(experiment)

    claim = Claim(
        claim_id="c1",
        statement="一个被验证的论断",
        status=ClaimStatus.VERIFIED,
        evidence_refs=[
            {"type": "paper", "id": "p1"},
            {"type": "experiment", "id": "e1"},
        ],
    )
    knowledge_store.save_claim(claim)

    artifact = Artifact(
        artifact_id="a1",
        artifact_group="g1",
        version=1,
        artifact_type=ArtifactType.PAPER_DRAFT,
        title="Paper Draft",
        cites_claim_ids=["c1"],
        cites_experiment_ids=["e1"],
    )
    knowledge_store.save_artifact(artifact)

    # 用真实向量库或 fake；此处用 fake 即可
    retriever = Retriever(
        store=knowledge_store,
        vector_store=FakeVectorStore(),
        embedding_fn=lambda texts: [[0.0] * 8 for _ in texts],
    )

    chain = retriever.trace_artifact_provenance("a1")

    assert chain["artifact"].artifact_id == "a1"
    assert any(c.claim_id == "c1" for c in chain["claims"])
    assert any(e.experiment_id == "e1" for e in chain["experiments"])
    assert any(p.paper_id == "p1" for p in chain["papers"])


def test_trace_artifact_provenance_handles_missing_claim(
    knowledge_store: KnowledgeStore,
):
    """Artifact 引用了不存在的 Claim，trace 不应抛异常，只是 claims 列表少一条。"""
    artifact = Artifact(
        artifact_id="a_missing",
        artifact_group="g_missing",
        version=1,
        artifact_type=ArtifactType.PAPER_DRAFT,
        title="Paper Draft",
        cites_claim_ids=["nonexistent"],
    )
    knowledge_store.save_artifact(artifact)

    retriever = Retriever(
        store=knowledge_store,
        vector_store=FakeVectorStore(),
        embedding_fn=lambda texts: [[0.0] * 8 for _ in texts],
    )

    chain = retriever.trace_artifact_provenance("a_missing")
    assert chain["claims"] == []


# ===== get_idea_claims =====


def test_get_idea_claims_returns_claims_derived_from_idea(
    knowledge_store: KnowledgeStore,
):
    """get_idea_claims 应返回 source_idea_id 匹配的所有 Claim。"""
    knowledge_store.save_idea(Idea(idea_id="i1", text="思路1"))
    knowledge_store.save_idea(Idea(idea_id="i2", text="思路2"))

    knowledge_store.save_claim(
        Claim(
            claim_id="c1",
            statement="论断1",
            status=ClaimStatus.DRAFT,
            source_idea_id="i1",
        )
    )
    knowledge_store.save_claim(
        Claim(
            claim_id="c2",
            statement="论断2",
            status=ClaimStatus.DRAFT,
            source_idea_id="i1",
        )
    )
    knowledge_store.save_claim(
        Claim(
            claim_id="c3",
            statement="论断3",
            status=ClaimStatus.DRAFT,
            source_idea_id="i2",
        )
    )

    retriever = Retriever(
        store=knowledge_store,
        vector_store=FakeVectorStore(),
        embedding_fn=lambda texts: [[0.0] * 8 for _ in texts],
    )

    claims = retriever.get_idea_claims("i1")
    assert {c.claim_id for c in claims} == {"c1", "c2"}


# ===== get_claim_evidence =====


def test_get_claim_evidence_returns_papers_and_experiments(
    knowledge_store: KnowledgeStore,
):
    """get_claim_evidence 应返回 evidence_refs 引用的 Paper + Experiment。"""
    knowledge_store.save_paper(Paper(paper_id="p1", title="Paper 1"))
    knowledge_store.save_paper(Paper(paper_id="p2", title="Paper 2"))
    knowledge_store.save_experiment(
        Experiment(experiment_id="e1", name="实验1")
    )

    knowledge_store.save_claim(
        Claim(
            claim_id="c1",
            statement="论断",
            status=ClaimStatus.VERIFIED,
            evidence_refs=[
                {"type": "paper", "id": "p1"},
                {"type": "paper", "id": "p2"},
                {"type": "experiment", "id": "e1"},
            ],
        )
    )

    retriever = Retriever(
        store=knowledge_store,
        vector_store=FakeVectorStore(),
        embedding_fn=lambda texts: [[0.0] * 8 for _ in texts],
    )

    evidence = retriever.get_claim_evidence("c1")
    assert evidence["claim"].claim_id == "c1"
    assert len(evidence["papers"]) == 2
    assert len(evidence["experiments"]) == 1


# ===== get_experiment_claims =====


def test_get_experiment_claims_returns_claims_verified_by_experiment(
    knowledge_store: KnowledgeStore,
):
    """get_experiment_claims 应返回 evidence_refs 中 type=experiment 且 id 匹配的 Claim。"""
    knowledge_store.save_experiment(
        Experiment(experiment_id="e1", name="实验1")
    )

    knowledge_store.save_claim(
        Claim(
            claim_id="c1",
            statement="论断1",
            status=ClaimStatus.VERIFIED,
            evidence_refs=[{"type": "experiment", "id": "e1"}],
        )
    )
    knowledge_store.save_claim(
        Claim(
            claim_id="c2",
            statement="论断2",
            status=ClaimStatus.VERIFIED,
            evidence_refs=[{"type": "experiment", "id": "e_other"}],
        )
    )

    retriever = Retriever(
        store=knowledge_store,
        vector_store=FakeVectorStore(),
        embedding_fn=lambda texts: [[0.0] * 8 for _ in texts],
    )

    claims = retriever.get_experiment_claims("e1")
    assert {c.claim_id for c in claims} == {"c1"}
