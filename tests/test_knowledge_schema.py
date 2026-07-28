"""知识库实体 schema 校验测试。

重点覆盖 Claim 硬约束：
- DRAFT 状态可无 evidence_refs
- 非 DRAFT 状态必须有 evidence_refs
- evidence_refs 每条必须有 type 与 id 字段，type 必须是 paper/experiment
"""
from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

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


# ===== Paper =====


def test_paper_creation_with_required_fields():
    """Paper 应能用必填字段构造。"""
    paper = Paper(
        paper_id="p1",
        title="Test Paper",
    )
    assert paper.paper_id == "p1"
    assert paper.title == "Test Paper"
    # 默认值
    assert paper.authors == []
    assert paper.year is None
    assert paper.source_stage == "research"
    assert paper.metadata == {}


def test_paper_creation_with_all_fields():
    """Paper 应能携带所有可选字段。"""
    paper = Paper(
        paper_id="p2",
        title="Full Paper",
        authors=["Alice", "Bob"],
        year=2024,
        venue="ICML",
        arxiv_id="2401.00001",
        doi="10.1000/xyz",
        abstract="摘要",
        url="https://example.com",
        pdf_path="/tmp/paper.pdf",
        metadata={"key": "value"},
    )
    assert paper.authors == ["Alice", "Bob"]
    assert paper.year == 2024
    assert paper.venue == "ICML"
    assert paper.metadata == {"key": "value"}


# ===== PaperChunk =====


def test_paper_chunk_creation():
    """PaperChunk 应能构造。"""
    chunk = PaperChunk(
        chunk_id="c1",
        paper_id="p1",
        chunk_index=0,
        text="chunk content",
        page=1,
    )
    assert chunk.chunk_id == "c1"
    assert chunk.paper_id == "p1"
    assert chunk.chunk_index == 0
    assert chunk.page == 1


# ===== Idea =====


def test_idea_creation_with_defaults():
    """Idea 默认 status=draft，constraints/source_paper_ids 为空列表。"""
    idea = Idea(idea_id="i1", text="一个新思路")
    assert idea.idea_id == "i1"
    assert idea.text == "一个新思路"
    assert idea.status == "draft"
    assert idea.constraints == []
    assert idea.source_paper_ids == []
    assert idea.source_stage == "ideation"


# ===== Claim 硬约束 =====


def test_claim_draft_allows_empty_evidence_refs():
    """DRAFT 状态的 Claim 可无 evidence_refs。"""
    claim = Claim(
        claim_id="c1",
        statement="一个论断",
        status=ClaimStatus.DRAFT,
    )
    assert claim.status == ClaimStatus.DRAFT
    assert claim.evidence_refs == []


def test_claim_non_draft_requires_evidence_refs():
    """非 DRAFT 状态必须提供 evidence_refs，否则抛 ValidationError。"""
    with pytest.raises(ValidationError):
        Claim(
            claim_id="c2",
            statement="一个论断",
            status=ClaimStatus.EVIDENCE_LINKED,
            evidence_refs=[],
        )


def test_claim_verified_requires_evidence_refs():
    """VERIFIED 状态必须提供 evidence_refs。"""
    with pytest.raises(ValidationError):
        Claim(
            claim_id="c3",
            statement="一个论断",
            status=ClaimStatus.VERIFIED,
            evidence_refs=[],
        )


def test_claim_evidence_ref_must_have_type_and_id():
    """evidence_ref 缺 type 或 id 应抛 ValidationError。"""
    # 缺 id
    with pytest.raises(ValidationError):
        Claim(
            claim_id="c4",
            statement="x",
            status=ClaimStatus.EVIDENCE_LINKED,
            evidence_refs=[{"type": "paper"}],  # 缺 id
        )
    # 缺 type
    with pytest.raises(ValidationError):
        Claim(
            claim_id="c5",
            statement="x",
            status=ClaimStatus.EVIDENCE_LINKED,
            evidence_refs=[{"id": "p1"}],  # 缺 type
        )


def test_claim_evidence_ref_type_must_be_paper_or_experiment():
    """evidence_ref.type 必须是 paper/experiment，否则抛 ValidationError。"""
    with pytest.raises(ValidationError):
        Claim(
            claim_id="c6",
            statement="x",
            status=ClaimStatus.EVIDENCE_LINKED,
            evidence_refs=[{"type": "dataset", "id": "d1"}],
        )


def test_claim_with_valid_evidence_refs_constructs_successfully():
    """合法 evidence_refs 应能正常构造。"""
    claim = Claim(
        claim_id="c7",
        statement="一个论断",
        status=ClaimStatus.VERIFIED,
        evidence_refs=[
            {"type": "paper", "id": "p1", "chunk_id": "chk1"},
            {"type": "experiment", "id": "e1"},
        ],
    )
    assert claim.status == ClaimStatus.VERIFIED
    assert len(claim.evidence_refs) == 2


def test_claim_default_status_is_draft():
    """Claim 默认 status 应为 DRAFT。"""
    claim = Claim(claim_id="c8", statement="x")
    assert claim.status == ClaimStatus.DRAFT


# ===== Experiment =====


def test_experiment_creation_with_defaults():
    """Experiment 默认 status=PLANNED。"""
    exp = Experiment(experiment_id="e1", name="实验1")
    assert exp.experiment_id == "e1"
    assert exp.name == "实验1"
    assert exp.status == ExperimentStatus.PLANNED
    assert exp.verifies_claim_ids == []
    assert exp.config == {}
    assert exp.source_stage == "experiment"


def test_experiment_all_statuses_constructable():
    """Experiment 各状态都应能构造。"""
    for status in ExperimentStatus:
        exp = Experiment(
            experiment_id=f"e_{status.value}",
            name=f"实验-{status.value}",
            status=status,
        )
        assert exp.status == status


# ===== Artifact =====


def test_artifact_creation_with_required_fields():
    """Artifact 应能用必填字段构造。"""
    art = Artifact(
        artifact_id="a1",
        artifact_group="g1",
        version=1,
        artifact_type=ArtifactType.METHOD_DOC,
        title="方法文档",
    )
    assert art.artifact_id == "a1"
    assert art.artifact_group == "g1"
    assert art.version == 1
    assert art.artifact_type == ArtifactType.METHOD_DOC
    assert art.cites_claim_ids == []
    assert art.parent_version_id is None


def test_artifact_type_enum_has_expected_members():
    """ArtifactType 应包含 6 种产出物类型。"""
    members = list(ArtifactType)
    assert ArtifactType.METHOD_DOC in members
    assert ArtifactType.FORMULA in members
    assert ArtifactType.DIAGRAM in members
    assert ArtifactType.EXPERIMENT_RESULT in members
    assert ArtifactType.PAPER_DRAFT in members
    assert ArtifactType.REVIEW_NOTE in members


# ===== Relation =====


def test_relation_creation():
    """Relation 应能构造并带元数据。"""
    rel = Relation(
        relation_id="r1",
        relation_type=RelationType.CLAIM_CITES_PAPER,
        source_id="c1",
        source_type=EntityType.CLAIM,
        target_id="p1",
        target_type=EntityType.PAPER,
        metadata={"page": 5},
    )
    assert rel.relation_type == RelationType.CLAIM_CITES_PAPER
    assert rel.source_type == EntityType.CLAIM
    assert rel.target_type == EntityType.PAPER
    assert rel.metadata == {"page": 5}


# ===== 枚举完整性 =====


def test_entity_type_has_5_members():
    """EntityType 应有 5 个成员。"""
    assert len(list(EntityType)) == 5
    assert EntityType.PAPER in EntityType.__members__.values()


def test_relation_type_has_7_members():
    """RelationType 应有 7 个成员。"""
    expected = {
        "IDEA_DERIVED_FROM_PAPER",
        "IDEA_DERIVES_CLAIM",
        "CLAIM_CITES_PAPER",
        "CLAIM_VERIFIED_BY_EXPERIMENT",
        "ARTIFACT_CITES_CLAIM",
        "ARTIFACT_CITES_EXPERIMENT",
        "IDEA_RELATED_TO_IDEA",
    }
    assert expected.issubset(set(RelationType.__members__.keys()))


def test_claim_status_has_5_members():
    """ClaimStatus 应有 5 个成员。"""
    assert len(list(ClaimStatus)) == 5
    assert ClaimStatus.DRAFT in ClaimStatus.__members__.values()
    assert ClaimStatus.VERIFIED in ClaimStatus.__members__.values()
