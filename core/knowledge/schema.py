"""5 种核心实体 + 关系的 schema 定义。

使用 Pydantic v2 建模，便于校验、序列化、生成 JSON Schema 给 Agent 作为 IO 契约。

设计原则：
- 每个实体有稳定 ID（uuid hex）
- 时间戳统一 UTC ISO 格式
- 关系独立存储（不嵌在实体里），便于图查询
- Claim 的 evidence_refs 是硬约束，由 Store 在写入时校验
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ===== 通用类型 =====

EntityId = str  # uuid hex


class EntityType(str, Enum):
    """实体类型枚举。"""

    PAPER = "paper"
    IDEA = "idea"
    CLAIM = "claim"
    EXPERIMENT = "experiment"
    ARTIFACT = "artifact"


class RelationType(str, Enum):
    """关系类型。

    命名规范：源类型_动词_目标类型，便于人类阅读。
    """

    IDEA_DERIVED_FROM_PAPER = "idea_derived_from_paper"        # Idea → Paper
    IDEA_DERIVES_CLAIM = "idea_derives_claim"                  # Idea → Claim
    CLAIM_CITES_PAPER = "claim_cites_paper"                    # Claim → Paper
    CLAIM_VERIFIED_BY_EXPERIMENT = "claim_verified_by_experiment"  # Claim → Experiment
    ARTIFACT_CITES_CLAIM = "artifact_cites_claim"              # Artifact → Claim
    ARTIFACT_CITES_EXPERIMENT = "artifact_cites_experiment"    # Artifact → Experiment
    IDEA_RELATED_TO_IDEA = "idea_related_to_idea"              # Idea → Idea（关联思路）
    # 材料知识（Task 2）：Material → Paper 来源
    MATERIAL_EXTRACTED_FROM_PAPER = "material_extracted_from_paper"  # Material → Paper
    # 材料性能：Material → Property（via MaterialKnowledge）
    MATERIAL_HAS_PROPERTY = "material_has_property"            # Material → MaterialProperty
    MATERIAL_HAS_SYNTHESIS = "material_has_synthesis"          # Material → MaterialSynthesis


# ===== Paper =====

class PaperChunk(BaseModel):
    """论文的一个 chunk（用于向量检索）。"""

    chunk_id: EntityId
    paper_id: EntityId
    chunk_index: int  # 在论文中的顺序
    text: str
    page: Optional[int] = None  # 来源页码（若可知）


class Paper(BaseModel):
    """文献实体。"""

    paper_id: EntityId
    title: str
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    venue: Optional[str] = None  # 会议/期刊
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    abstract: Optional[str] = None
    url: Optional[str] = None
    pdf_path: Optional[str] = None  # 本地 PDF 路径（若下载）
    # chunk 文本不在此处，由 VectorStore 与 PaperChunk 表管理
    # 元数据扩展（自由字段，用于阶段特定标注）
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_stage: str = "research"  # 产生此实体的阶段


# ===== Idea =====

class Idea(BaseModel):
    """思路实体。"""

    idea_id: EntityId
    text: str  # 思路自由文本描述
    # 思路约束（来自与用户探讨）：必须满足的条件
    constraints: list[str] = Field(default_factory=list)
    # 关联的 Paper ID（思路来源）
    source_paper_ids: list[EntityId] = Field(default_factory=list)
    # 关联的 Idea ID（关联思路）
    related_idea_ids: list[EntityId] = Field(default_factory=list)
    status: str = "draft"  # draft / validated / rejected / adopted
    # 验证记录（可行性/新颖性/贡献度的评估结果）
    validation_notes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str = "user"  # user / agent_id
    source_stage: str = "ideation"


# ===== Claim =====

class ClaimStatus(str, Enum):
    """Claim 的状态。

    硬约束：进入 writing 阶段前，所有被引用的 Claim 必须 VERIFIED。
    """

    DRAFT = "draft"                  # 刚从 Idea 派生
    EVIDENCE_LINKED = "evidence_linked"  # 已关联证据（Paper/Experiment）
    VERIFIED = "verified"            # 已被实验验证或证据充分
    REFUTED = "refuted"              # 被实验或证据反驳
    SUPERSEDED = "superseded"        # 被新 Claim 取代


class Claim(BaseModel):
    """观点/论断实体。论文的核心组成单元。

    硬约束：evidence_refs 不能为空（除非 status=DRAFT）。
    每条 evidence_ref 形如 {"type": "paper"/"experiment", "id": "...", "chunk_id"?: "..."}
    """

    claim_id: EntityId
    statement: str  # 论断陈述（一句话可验证）
    source_idea_id: Optional[EntityId] = None  # 由哪个 Idea 派生
    # 证据引用：list[dict]，每条指向 Paper 或 Experiment
    evidence_refs: list[dict[str, str]] = Field(default_factory=list)
    status: ClaimStatus = ClaimStatus.DRAFT
    # Claim 在论文中的角色：contribution / method / assumption / result
    role: str = "contribution"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    verified_at: Optional[datetime] = None
    source_stage: str = "design"

    @model_validator(mode="after")
    def _validate_evidence(self) -> "Claim":
        """模型级校验：所有字段已赋值后执行。

        硬约束：
        1. 非 DRAFT 状态必须有 evidence_refs
        2. 每条 evidence_ref 必须含 type（paper/experiment）和 id
        """
        # 非草稿状态必须有证据
        if self.status != ClaimStatus.DRAFT and not self.evidence_refs:
            raise ValueError(
                f"Claim 状态为 {self.status}，必须有 evidence_refs（不可为空）"
            )
        # 每条证据必须有 type 和 id
        for ref in self.evidence_refs:
            if "type" not in ref or "id" not in ref:
                raise ValueError(
                    f"evidence_ref 必须含 type 与 id 字段，实际={ref}"
                )
            if ref["type"] not in ("paper", "experiment"):
                raise ValueError(
                    f"evidence_ref.type 必须是 paper/experiment，实际={ref['type']}"
                )
        return self


# ===== Experiment =====

class ExperimentStatus(str, Enum):
    """实验状态。"""

    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ANOMALY_DETECTED = "anomaly_detected"


class Experiment(BaseModel):
    """实验实体。"""

    experiment_id: EntityId
    name: str
    # 关联的 Claim（实验验证哪些 Claim）
    verifies_claim_ids: list[EntityId] = Field(default_factory=list)
    # 实验配置（数据集、baseline、超参等）
    config: dict[str, Any] = Field(default_factory=dict)
    status: ExperimentStatus = ExperimentStatus.PLANNED
    # 结果摘要（详细数据走 Artifact）
    result_summary: Optional[str] = None
    # 异常记录（若 status=ANOMALY_DETECTED/FAILED）
    anomaly_notes: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_stage: str = "experiment"


# ===== Artifact =====

class ArtifactType(str, Enum):
    """产出物类型。"""

    METHOD_DOC = "method_doc"          # 方法文档（方案制定阶段）
    FORMULA = "formula"                # 公式（LaTeX）
    DIAGRAM = "diagram"                # 图表（Mermaid/TikZ）
    EXPERIMENT_RESULT = "experiment_result"  # 实验结果（图表/数据）
    PAPER_DRAFT = "paper_draft"        # 论文稿
    REVIEW_NOTE = "review_note"        # 审稿意见


class Artifact(BaseModel):
    """产出物实体。带版本。

    一个 artifact_id 可有多个版本，每个版本是独立的 Artifact 实体。
    版本号采用 semver-lite：v1, v2, ...
    """

    artifact_id: EntityId  # 该版本的唯一 ID
    artifact_group: EntityId  # 同一逻辑产出物的组 ID（不同版本共享）
    version: int  # 版本号（递增）
    artifact_type: ArtifactType
    title: str
    # 内容可以是文本（LaTeX/Markdown）或文件路径引用
    content: Optional[str] = None
    content_path: Optional[str] = None  # 若内容过大，存文件
    # 引用的 Claim / Experiment
    cites_claim_ids: list[EntityId] = Field(default_factory=list)
    cites_experiment_ids: list[EntityId] = Field(default_factory=list)
    # 产生此产出物的阶段与 agent
    source_stage: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # 父版本（若由前版本修订而来）
    parent_version_id: Optional[EntityId] = None


# ===== Relation =====

class Relation(BaseModel):
    """实体间关系。独立存储，便于图查询。"""

    relation_id: EntityId
    relation_type: RelationType
    source_id: EntityId
    source_type: EntityType
    target_id: EntityId
    target_type: EntityType
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # 关系元数据（如引证的具体页码、置信度等）
    metadata: dict[str, Any] = Field(default_factory=dict)


# ===== 材料知识实体（Task 2：材料-性能-合成三元组）=====

class Material(BaseModel):
    """材料实体：从论文中抽取的材料成分/结构。

    满足赛题「知识抽取」要求：材料成分（化学式、元素组成、掺杂比例）、
    晶体结构（空间群、晶格参数、对称性）。
    """

    material_id: EntityId
    # 材料名称/化学式（规范化，如 "CH3NH3PbI3"、"MAPbI3"）
    name: str
    # 化学式（若可解析，如 "Cs0.05FA0.95PbI3"）
    formula: str = ""
    # 晶体结构：空间群 / 晶格参数 / 对称性
    crystal_structure: str = ""
    space_group: str = ""
    lattice_parameters: str = ""  # 自由文本（如 "a=8.85 Å, cubic"）
    symmetry: str = ""
    # 组成描述（元素/掺杂比例，自由文本）
    composition: str = ""
    # 来源论文（证据链溯源）
    paper_id: Optional[EntityId] = None
    paper_title: str = ""
    # 归一化名称（小写去空格，用于跨文献实体链接/去重）
    norm_name: str = ""
    # 结构化抽取置信度（0~1）
    confidence: float = 0.0
    # 抽取来源 chunk 片段（证据）
    source_snippet: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_stage: str = "research"


class MaterialProperty(BaseModel):
    """材料性能实体（性能指标）：ZT、功率因子、热导率等。"""

    property_id: EntityId
    material_id: EntityId  # 归属材料
    property_name: str  # 性能名称（如 "ZT"、"thermal_conductivity"）
    property_name_cn: str = ""  # 中文名（如 "热电优值"）
    value: str = ""  # 数值（含单位，如 "1.05 at 800K"）
    value_num: Optional[float] = None  # 数值部分（若可解析）
    unit: str = ""  # 单位（如 "W/mK"）
    condition: str = ""  # 测试条件（温度/压力等）
    paper_id: Optional[EntityId] = None
    paper_title: str = ""
    confidence: float = 0.0
    source_snippet: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_stage: str = "research"


class MaterialSynthesis(BaseModel):
    """材料合成方法实体（合成条件）：温度、压力、时间、前驱体、工艺步骤。"""

    synthesis_id: EntityId
    material_id: EntityId  # 归属材料
    method: str = ""  # 工艺方法（如 "solid-state reaction"、"CVD"）
    precursors: list[str] = Field(default_factory=list)  # 前驱体
    temperature: str = ""  # 温度条件（如 "500°C for 12h"）
    pressure: str = ""  # 压力条件
    atmosphere: str = ""  # 气氛（如 "Ar"、"N2"）
    duration: str = ""  # 时间
    steps: str = ""  # 工艺步骤描述
    paper_id: Optional[EntityId] = None
    paper_title: str = ""
    confidence: float = 0.0
    source_snippet: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_stage: str = "research"
