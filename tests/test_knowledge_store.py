"""KnowledgeStore CRUD 测试。

覆盖 Paper / Idea / Claim / Experiment / Artifact / Relation 的存取与查询。
"""
from __future__ import annotations

import pytest

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
from core.knowledge.store import KnowledgeStore, StoreError


# ===== Paper =====


def test_save_and_get_paper(knowledge_store: KnowledgeStore):
    """save_paper 后 get_paper 应返回等价实体。"""
    paper = Paper(paper_id="p1", title="Test Paper", year=2024)
    knowledge_store.save_paper(paper)

    loaded = knowledge_store.get_paper("p1")
    assert loaded.paper_id == "p1"
    assert loaded.title == "Test Paper"
    assert loaded.year == 2024


def test_get_paper_raises_on_missing(knowledge_store: KnowledgeStore):
    """get_paper 不存在时应抛 StoreError。"""
    with pytest.raises(StoreError):
        knowledge_store.get_paper("nonexistent")


def test_list_papers_returns_all(knowledge_store: KnowledgeStore):
    """list_papers 应返回所有论文。"""
    knowledge_store.save_paper(Paper(paper_id="p1", title="A"))
    knowledge_store.save_paper(Paper(paper_id="p2", title="B"))

    papers = knowledge_store.list_papers()
    assert len(papers) == 2
    titles = {p.title for p in papers}
    assert titles == {"A", "B"}


def test_save_paper_chunks_and_retrieve(knowledge_store: KnowledgeStore):
    """save_paper_chunks 后 get_paper_chunks 应按 chunk_index 排序返回。"""
    chunks = [
        PaperChunk(chunk_id="c1", paper_id="p1", chunk_index=0, text="第一段"),
        PaperChunk(chunk_id="c2", paper_id="p1", chunk_index=1, text="第二段"),
        PaperChunk(chunk_id="c3", paper_id="p1", chunk_index=2, text="第三段"),
    ]
    # 乱序写入
    knowledge_store.save_paper_chunks([chunks[2], chunks[0], chunks[1]])

    loaded = knowledge_store.get_paper_chunks("p1")
    assert len(loaded) == 3
    assert [c.chunk_index for c in loaded] == [0, 1, 2]


def test_get_paper_chunks_returns_empty_for_unknown_paper(
    knowledge_store: KnowledgeStore,
):
    """未知 paper_id 的 chunks 应返回空列表。"""
    assert knowledge_store.get_paper_chunks("unknown") == []


# ===== Idea =====


def test_save_and_get_idea(knowledge_store: KnowledgeStore):
    """save_idea 后 get_idea 应返回等价实体。"""
    idea = Idea(idea_id="i1", text="一个思路", status="validated")
    knowledge_store.save_idea(idea)

    loaded = knowledge_store.get_idea("i1")
    assert loaded.idea_id == "i1"
    assert loaded.text == "一个思路"
    assert loaded.status == "validated"


def test_get_idea_raises_on_missing(knowledge_store: KnowledgeStore):
    """get_idea 不存在时应抛 StoreError。"""
    with pytest.raises(StoreError):
        knowledge_store.get_idea("nonexistent")


def test_list_ideas_with_status_filter(knowledge_store: KnowledgeStore):
    """list_ideas(status=...) 应按 status 过滤。"""
    knowledge_store.save_idea(Idea(idea_id="i1", text="a", status="draft"))
    knowledge_store.save_idea(Idea(idea_id="i2", text="b", status="validated"))
    knowledge_store.save_idea(Idea(idea_id="i3", text="c", status="validated"))

    validated = knowledge_store.list_ideas(status="validated")
    assert len(validated) == 2
    assert all(i.status == "validated" for i in validated)

    all_ideas = knowledge_store.list_ideas()
    assert len(all_ideas) == 3


# ===== Claim =====


def test_save_and_get_claim(knowledge_store: KnowledgeStore):
    """save_claim 后 get_claim 应返回等价实体。"""
    claim = Claim(
        claim_id="c1",
        statement="一个论断",
        status=ClaimStatus.VERIFIED,
        evidence_refs=[{"type": "paper", "id": "p1"}],
    )
    knowledge_store.save_claim(claim)

    loaded = knowledge_store.get_claim("c1")
    assert loaded.claim_id == "c1"
    assert loaded.status == ClaimStatus.VERIFIED
    assert loaded.evidence_refs == [{"type": "paper", "id": "p1"}]


def test_list_claims_with_status_filter(knowledge_store: KnowledgeStore):
    """list_claims(status=...) 应按 status 过滤。"""
    knowledge_store.save_claim(
        Claim(claim_id="c1", statement="a", status=ClaimStatus.DRAFT)
    )
    knowledge_store.save_claim(
        Claim(
            claim_id="c2",
            statement="b",
            status=ClaimStatus.VERIFIED,
            evidence_refs=[{"type": "paper", "id": "p1"}],
        )
    )

    verified = knowledge_store.list_claims(status=ClaimStatus.VERIFIED)
    assert len(verified) == 1
    assert verified[0].claim_id == "c2"

    drafts = knowledge_store.list_claims(status=ClaimStatus.DRAFT)
    assert len(drafts) == 1


def test_claims_without_evidence_returns_only_violations(
    knowledge_store: KnowledgeStore,
):
    """claims_without_evidence 应只返回非 DRAFT 且无证据的 Claim。"""
    # DRAFT 无证据 — 合规，不应出现在结果中
    knowledge_store.save_claim(
        Claim(claim_id="c1", statement="draft", status=ClaimStatus.DRAFT)
    )
    # VERIFIED 有证据 — 合规
    knowledge_store.save_claim(
        Claim(
            claim_id="c2",
            statement="verified",
            status=ClaimStatus.VERIFIED,
            evidence_refs=[{"type": "paper", "id": "p1"}],
        )
    )

    violations = knowledge_store.claims_without_evidence()
    # 没有 DRAFT 也没有非 DRAFT 无证据的，应为空
    assert violations == []


# ===== Experiment =====


def test_save_and_get_experiment(knowledge_store: KnowledgeStore):
    """save_experiment 后 get_experiment 应返回等价实体。"""
    exp = Experiment(
        experiment_id="e1",
        name="实验1",
        status=ExperimentStatus.COMPLETED,
        verifies_claim_ids=["c1"],
    )
    knowledge_store.save_experiment(exp)

    loaded = knowledge_store.get_experiment("e1")
    assert loaded.experiment_id == "e1"
    assert loaded.status == ExperimentStatus.COMPLETED
    assert loaded.verifies_claim_ids == ["c1"]


def test_list_experiments_with_status_filter(knowledge_store: KnowledgeStore):
    """list_experiments(status=...) 应按 status 过滤。"""
    knowledge_store.save_experiment(
        Experiment(experiment_id="e1", name="a", status=ExperimentStatus.PLANNED)
    )
    knowledge_store.save_experiment(
        Experiment(experiment_id="e2", name="b", status=ExperimentStatus.COMPLETED)
    )

    completed = knowledge_store.list_experiments(status="completed")
    assert len(completed) == 1
    assert completed[0].experiment_id == "e2"


# ===== Artifact 版本管理 =====


def test_save_and_get_artifact(knowledge_store: KnowledgeStore):
    """save_artifact 后 get_artifact 应返回等价实体。"""
    art = Artifact(
        artifact_id="a1",
        artifact_group="g1",
        version=1,
        artifact_type=ArtifactType.METHOD_DOC,
        title="方法文档",
    )
    knowledge_store.save_artifact(art)

    loaded = knowledge_store.get_artifact("a1")
    assert loaded.artifact_id == "a1"
    assert loaded.artifact_group == "g1"
    assert loaded.version == 1


def test_list_artifact_versions_sorted_by_version(knowledge_store: KnowledgeStore):
    """list_artifact_versions 应按 version 升序返回。"""
    group = "g_versions"
    # 乱序写入
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a3",
            artifact_group=group,
            version=3,
            artifact_type=ArtifactType.PAPER_DRAFT,
            title="v3",
        )
    )
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a1",
            artifact_group=group,
            version=1,
            artifact_type=ArtifactType.PAPER_DRAFT,
            title="v1",
        )
    )
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a2",
            artifact_group=group,
            version=2,
            artifact_type=ArtifactType.PAPER_DRAFT,
            title="v2",
        )
    )

    versions = knowledge_store.list_artifact_versions(group)
    assert [v.version for v in versions] == [1, 2, 3]


def test_latest_artifact_version_returns_highest(knowledge_store: KnowledgeStore):
    """latest_artifact_version 应返回 version 最大的 Artifact。"""
    group = "g_latest"
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a1",
            artifact_group=group,
            version=1,
            artifact_type=ArtifactType.METHOD_DOC,
            title="v1",
        )
    )
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a2",
            artifact_group=group,
            version=2,
            artifact_type=ArtifactType.METHOD_DOC,
            title="v2",
        )
    )

    latest = knowledge_store.latest_artifact_version(group)
    assert latest is not None
    assert latest.version == 2


def test_latest_artifact_version_returns_none_for_empty_group(
    knowledge_store: KnowledgeStore,
):
    """无任何版本时 latest_artifact_version 应返回 None。"""
    assert knowledge_store.latest_artifact_version("empty_group") is None


def test_next_artifact_version_returns_1_for_empty_group(
    knowledge_store: KnowledgeStore,
):
    """空 group 的 next_artifact_version 应返回 1。"""
    assert knowledge_store.next_artifact_version("empty_group") == 1


def test_next_artifact_version_increments(knowledge_store: KnowledgeStore):
    """已有版本时 next_artifact_version 应返回 latest.version + 1。"""
    group = "g_next"
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a1",
            artifact_group=group,
            version=1,
            artifact_type=ArtifactType.METHOD_DOC,
            title="v1",
        )
    )
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a2",
            artifact_group=group,
            version=2,
            artifact_type=ArtifactType.METHOD_DOC,
            title="v2",
        )
    )

    assert knowledge_store.next_artifact_version(group) == 3


# ===== Relation =====


def test_save_and_list_relations(knowledge_store: KnowledgeStore):
    """save_relation 后 list_relations 应能按各种条件过滤。"""
    rel1 = Relation(
        relation_id="r1",
        relation_type=RelationType.CLAIM_CITES_PAPER,
        source_id="c1",
        source_type=EntityType.CLAIM,
        target_id="p1",
        target_type=EntityType.PAPER,
    )
    rel2 = Relation(
        relation_id="r2",
        relation_type=RelationType.ARTIFACT_CITES_CLAIM,
        source_id="a1",
        source_type=EntityType.ARTIFACT,
        target_id="c1",
        target_type=EntityType.CLAIM,
    )
    knowledge_store.save_relation(rel1)
    knowledge_store.save_relation(rel2)

    # 全部
    all_rels = knowledge_store.list_relations()
    assert len(all_rels) == 2

    # 按 source_id 过滤
    from_c1 = knowledge_store.list_relations(source_id="c1")
    assert len(from_c1) == 1
    assert from_c1[0].relation_id == "r1"

    # 按 target_id 过滤
    to_c1 = knowledge_store.list_relations(target_id="c1")
    assert len(to_c1) == 1
    assert to_c1[0].relation_id == "r2"

    # 按 relation_type 过滤
    cites_paper = knowledge_store.list_relations(
        relation_type=RelationType.CLAIM_CITES_PAPER
    )
    assert len(cites_paper) == 1
    assert cites_paper[0].relation_id == "r1"


# ===== new_id 静态方法 =====


def test_new_id_returns_unique_strings():
    """new_id 应返回唯一的 hex 字符串。"""
    ids = {KnowledgeStore.new_id() for _ in range(100)}
    assert len(ids) == 100  # 全部唯一


def test_new_id_returns_hex_string():
    """new_id 应返回 32 字符的 hex 字符串（uuid4.hex）。"""
    new_id = KnowledgeStore.new_id()
    assert isinstance(new_id, str)
    assert len(new_id) == 32
    # 应是合法的十六进制
    int(new_id, 16)
