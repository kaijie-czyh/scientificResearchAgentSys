"""统一知识库层。

5 种核心实体贯穿全生命周期：
- Paper: 文献（产生于调研阶段）
- Idea: 思路（产生于思路探讨阶段）
- Claim: 观点/论断（产生于方案制定阶段，必有引证）
- Experiment: 实验（产生于实验运行阶段）
- Artifact: 产出物（任意阶段，带版本）

关系（DAG）：
- Idea 引用 Paper（思路来源）
- Claim 引证 Paper / Experiment（证据链）
- Artifact 引用 Claim / Experiment（产出依据）
- Claim 由 Idea 派生

硬约束：Claim 进入 writing 阶段前必须有 evidence_refs。
"""
from core.knowledge.schema import (
    EntityId,
    Paper,
    PaperChunk,
    Idea,
    Claim,
    ClaimStatus,
    Experiment,
    ExperimentStatus,
    Artifact,
    ArtifactType,
    EntityType,
    Relation,
    RelationType,
    Material,
    MaterialProperty,
    MaterialSynthesis,
)
from core.knowledge.store import KnowledgeStore, StoreError
from core.knowledge.vector_store import VectorStore, ChromaVectorStore
from core.knowledge.retriever import Retriever, RetrievalResult

__all__ = [
    "EntityId",
    "Paper",
    "PaperChunk",
    "Idea",
    "Claim",
    "ClaimStatus",
    "Experiment",
    "ExperimentStatus",
    "Artifact",
    "ArtifactType",
    "EntityType",
    "Relation",
    "RelationType",
    "Material",
    "MaterialProperty",
    "MaterialSynthesis",
    "KnowledgeStore",
    "StoreError",
    "VectorStore",
    "ChromaVectorStore",
    "Retriever",
    "RetrievalResult",
]
