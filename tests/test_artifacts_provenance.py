"""ProvenanceValidator 溯源链测试。

覆盖：
- build_chain: 完整链构建
- find_broken_links: 断链检测
- find_unverified_claims: 未验证 Claim 检测
- validate_for_writing: 通过 / 断链 / 未验证 Claim / 未完成 Experiment 都测
- ProvenanceChain.to_dict
"""
from __future__ import annotations

import pytest

from core.artifacts.provenance import (
    ProvenanceChain,
    ProvenanceError,
    ProvenanceNode,
    ProvenanceValidator,
)
from core.knowledge.schema import (
    Artifact,
    ArtifactType,
    Claim,
    ClaimStatus,
    EntityType,
    Experiment,
    ExperimentStatus,
    Paper,
)
from core.knowledge.store import KnowledgeStore


# ===== 辅助构造 =====


def _seed_full_chain(store: KnowledgeStore) -> str:
    """构造完整溯源链：Artifact → Claim → Paper / Experiment，全部合规。

    返回 artifact_id。
    """
    paper = Paper(paper_id="p1", title="Source Paper", year=2024)
    store.save_paper(paper)

    experiment = Experiment(
        experiment_id="e1",
        name="验证实验",
        status=ExperimentStatus.COMPLETED,
        verifies_claim_ids=["c1"],
    )
    store.save_experiment(experiment)

    claim = Claim(
        claim_id="c1",
        statement="一个被验证的论断",
        status=ClaimStatus.VERIFIED,
        evidence_refs=[
            {"type": "paper", "id": "p1"},
            {"type": "experiment", "id": "e1"},
        ],
    )
    store.save_claim(claim)

    artifact = Artifact(
        artifact_id="a1",
        artifact_group="g1",
        version=1,
        artifact_type=ArtifactType.PAPER_DRAFT,
        title="Paper Draft",
        cites_claim_ids=["c1"],
        cites_experiment_ids=["e1"],
    )
    store.save_artifact(artifact)
    return "a1"


# ===== build_chain =====


def test_build_chain_returns_chain_with_root_artifact(
    knowledge_store: KnowledgeStore,
):
    """build_chain 应返回以 Artifact 为根的 ProvenanceChain。"""
    artifact_id = _seed_full_chain(knowledge_store)
    validator = ProvenanceValidator(store=knowledge_store)

    chain = validator.build_chain(artifact_id)

    assert isinstance(chain, ProvenanceChain)
    assert chain.root_artifact_id == artifact_id
    # 根节点（Artifact）应在 nodes 中
    assert artifact_id in chain.nodes
    assert chain.nodes[artifact_id].entity_type == EntityType.ARTIFACT


def test_build_chain_includes_claim_paper_experiment(
    knowledge_store: KnowledgeStore,
):
    """build_chain 应把 Claim / Paper / Experiment 节点都加入。"""
    artifact_id = _seed_full_chain(knowledge_store)
    validator = ProvenanceValidator(store=knowledge_store)

    chain = validator.build_chain(artifact_id)

    entity_types = {n.entity_type for n in chain.nodes.values()}
    assert EntityType.ARTIFACT in entity_types
    assert EntityType.CLAIM in entity_types
    assert EntityType.PAPER in entity_types
    assert EntityType.EXPERIMENT in entity_types


def test_build_chain_raises_on_missing_artifact(knowledge_store: KnowledgeStore):
    """build_chain 不存在的 artifact_id 应抛 ProvenanceError。"""
    validator = ProvenanceValidator(store=knowledge_store)
    with pytest.raises(ProvenanceError):
        validator.build_chain("nonexistent")


# ===== find_broken_links =====


def test_find_broken_links_returns_empty_for_complete_chain(
    knowledge_store: KnowledgeStore,
):
    """完整链应无断链。"""
    artifact_id = _seed_full_chain(knowledge_store)
    validator = ProvenanceValidator(store=knowledge_store)

    chain = validator.build_chain(artifact_id)
    assert chain.find_broken_links() == []


def test_find_broken_links_detects_missing_claim(
    knowledge_store: KnowledgeStore,
):
    """Artifact 引用了不存在的 Claim，应出现断链。"""
    # 只写 Artifact，引用不存在的 Claim
    artifact = Artifact(
        artifact_id="a_broken",
        artifact_group="g_broken",
        version=1,
        artifact_type=ArtifactType.PAPER_DRAFT,
        title="Broken",
        cites_claim_ids=["missing_claim"],
    )
    knowledge_store.save_artifact(artifact)

    validator = ProvenanceValidator(store=knowledge_store)
    chain = validator.build_chain("a_broken")

    broken = chain.find_broken_links()
    assert len(broken) >= 1
    # 断链的 target_id 应是 missing_claim
    assert any(e.target_id == "missing_claim" for e in broken)


def test_find_broken_links_detects_missing_paper(
    knowledge_store: KnowledgeStore,
):
    """Claim 引用了不存在的 Paper，应出现断链。"""
    # 写 Claim，evidence_refs 指向不存在的 paper
    knowledge_store.save_claim(
        Claim(
            claim_id="c1",
            statement="claim",
            status=ClaimStatus.VERIFIED,
            evidence_refs=[{"type": "paper", "id": "missing_paper"}],
        )
    )
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a1",
            artifact_group="g1",
            version=1,
            artifact_type=ArtifactType.PAPER_DRAFT,
            title="t",
            cites_claim_ids=["c1"],
        )
    )

    validator = ProvenanceValidator(store=knowledge_store)
    chain = validator.build_chain("a1")

    broken = chain.find_broken_links()
    assert any(e.target_id == "missing_paper" for e in broken)


# ===== find_unverified_claims =====


def test_find_unverified_claims_returns_empty_when_all_verified(
    knowledge_store: KnowledgeStore,
):
    """所有 Claim 都 VERIFIED 时应返回空。"""
    artifact_id = _seed_full_chain(knowledge_store)
    validator = ProvenanceValidator(store=knowledge_store)

    chain = validator.build_chain(artifact_id)
    assert chain.find_unverified_claims() == []


def test_find_unverified_claims_detects_draft_claim(
    knowledge_store: KnowledgeStore,
):
    """链中有 DRAFT 状态的 Claim 应被检测出。"""
    # 写一个 DRAFT 的 Claim（DRAFT 可无 evidence_refs）
    knowledge_store.save_claim(
        Claim(
            claim_id="c_draft",
            statement="draft claim",
            status=ClaimStatus.DRAFT,
        )
    )
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a1",
            artifact_group="g1",
            version=1,
            artifact_type=ArtifactType.PAPER_DRAFT,
            title="t",
            cites_claim_ids=["c_draft"],
        )
    )

    validator = ProvenanceValidator(store=knowledge_store)
    chain = validator.build_chain("a1")

    unverified = chain.find_unverified_claims()
    assert len(unverified) >= 1
    assert any(n.entity_id == "c_draft" for n in unverified)


# ===== to_dict =====


def test_chain_to_dict_contains_required_fields(
    knowledge_store: KnowledgeStore,
):
    """to_dict 应含 root_artifact_id / nodes / edges / broken_links / unverified_claims。"""
    artifact_id = _seed_full_chain(knowledge_store)
    validator = ProvenanceValidator(store=knowledge_store)

    chain = validator.build_chain(artifact_id)
    d = chain.to_dict()

    assert d["root_artifact_id"] == artifact_id
    assert "nodes" in d
    assert "edges" in d
    assert "broken_links" in d
    assert "unverified_claims" in d
    assert isinstance(d["nodes"], list)
    assert isinstance(d["edges"], list)


# ===== validate_for_writing =====


def test_validate_for_writing_passes_for_complete_verified_chain(
    knowledge_store: KnowledgeStore,
):
    """完整 + 全 VERIFIED + 全 COMPLETED 的链应通过校验。"""
    artifact_id = _seed_full_chain(knowledge_store)
    validator = ProvenanceValidator(store=knowledge_store)

    # 不抛异常即通过
    validator.validate_for_writing(artifact_id)


def test_validate_for_writing_raises_on_broken_link(
    knowledge_store: KnowledgeStore,
):
    """断链时应抛 ProvenanceError。"""
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a_broken",
            artifact_group="g_broken",
            version=1,
            artifact_type=ArtifactType.PAPER_DRAFT,
            title="t",
            cites_claim_ids=["missing"],
        )
    )
    validator = ProvenanceValidator(store=knowledge_store)

    with pytest.raises(ProvenanceError):
        validator.validate_for_writing("a_broken")


def test_validate_for_writing_raises_on_unverified_claim(
    knowledge_store: KnowledgeStore,
):
    """含未验证 Claim 时应抛 ProvenanceError。"""
    # DRAFT Claim（合规但未验证）
    knowledge_store.save_claim(
        Claim(claim_id="c_draft", statement="x", status=ClaimStatus.DRAFT)
    )
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a1",
            artifact_group="g1",
            version=1,
            artifact_type=ArtifactType.PAPER_DRAFT,
            title="t",
            cites_claim_ids=["c_draft"],
        )
    )

    validator = ProvenanceValidator(store=knowledge_store)
    with pytest.raises(ProvenanceError):
        validator.validate_for_writing("a1")


def test_validate_for_writing_raises_on_uncompleted_experiment(
    knowledge_store: KnowledgeStore,
):
    """含未完成 Experiment 时应抛 ProvenanceError。"""
    # 写一个 RUNNING 的 Experiment
    knowledge_store.save_experiment(
        Experiment(
            experiment_id="e_running",
            name="未完成实验",
            status=ExperimentStatus.RUNNING,
        )
    )
    # 写一个 VERIFIED Claim 引用该实验
    knowledge_store.save_claim(
        Claim(
            claim_id="c1",
            statement="x",
            status=ClaimStatus.VERIFIED,
            evidence_refs=[{"type": "experiment", "id": "e_running"}],
        )
    )
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a1",
            artifact_group="g1",
            version=1,
            artifact_type=ArtifactType.PAPER_DRAFT,
            title="t",
            cites_claim_ids=["c1"],
            cites_experiment_ids=["e_running"],
        )
    )

    validator = ProvenanceValidator(store=knowledge_store)
    with pytest.raises(ProvenanceError):
        validator.validate_for_writing("a1")


def test_validate_for_writing_raises_on_failed_experiment(
    knowledge_store: KnowledgeStore,
):
    """FAILED 状态的 Experiment 也应触发校验失败。"""
    knowledge_store.save_experiment(
        Experiment(
            experiment_id="e_failed",
            name="失败实验",
            status=ExperimentStatus.FAILED,
        )
    )
    knowledge_store.save_claim(
        Claim(
            claim_id="c1",
            statement="x",
            status=ClaimStatus.VERIFIED,
            evidence_refs=[{"type": "experiment", "id": "e_failed"}],
        )
    )
    knowledge_store.save_artifact(
        Artifact(
            artifact_id="a1",
            artifact_group="g1",
            version=1,
            artifact_type=ArtifactType.PAPER_DRAFT,
            title="t",
            cites_claim_ids=["c1"],
        )
    )

    validator = ProvenanceValidator(store=knowledge_store)
    with pytest.raises(ProvenanceError):
        validator.validate_for_writing("a1")
