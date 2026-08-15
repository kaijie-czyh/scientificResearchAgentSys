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
    # 结构化指标（accuracy/loss 等，由 experiments/results.json 解析得到）
    metrics: Optional[dict[str, Any]] = None
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


# ===== 证据等级（科研事实可信度分级）=====

class EvidenceLevel(str, Enum):
    """证据等级：所有性质数值与合成参数必须标注来源可信度。

    分级（数据来源强度从高到低）：
    - A：多个实验论文直接验证
    - B：单篇实验论文直接验证
    - C：多个文献间接支持
    - D：理论/数据库预测（如 Materials Project / DFT）
    - E：LLM 推断（非文献原始数据，仅供实验设计参考）
    """

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"


# 证据等级 → 中文标签（前端展示用）
EVIDENCE_LEVEL_LABELS: dict[str, str] = {
    "A": "多篇实验论文直接验证",
    "B": "单篇实验论文直接验证",
    "C": "多个文献间接支持",
    "D": "理论/数据库预测",
    "E": "LLM 推断",
}


class PropertyDataKind(str, Enum):
    """性质数值的数据类型：实验值 / 理论值 / 数据库预测 / LLM 推断。"""

    EXPERIMENTAL = "experimental"    # 实验测量值
    THEORETICAL = "theoretical"      # 理论计算值
    DATABASE = "database"            # 数据库预测值（MP/OQMD/NOMAD）
    INFERRED = "inferred"            # LLM 推断（非原始数据）


# ===== 材料知识实体（Task 2：材料-性能-合成三元组）=====

class Material(BaseModel):
    """材料实体：从论文中抽取的材料成分/结构。

    满足赛题「知识抽取」要求：材料成分（化学式、元素组成、掺杂比例）、
    晶体结构（空间群、晶格参数、对称性）。

    深度分析扩展（多维材料性质画像的基础结构维度）：
    - material_type：材料类型（半导体/热电/钙钛矿/陶瓷/金属/高分子…）
    - crystal_system：晶系（cubic/tetragonal/orthorhombic/hexagonal/monoclinic/triclinic）
    - morphology：单晶/多晶/非晶
    - phase_composition / is_multiphase：相组成与是否多相
    - element_composition / element_ratio：元素组成与比例
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
    # ===== 深度分析扩展：基础结构性质 =====
    material_type: str = ""          # 材料类型（半导体/热电/钙钛矿/陶瓷/金属/高分子…）
    crystal_system: str = ""         # 晶系（cubic/tetragonal/orthorhombic/hexagonal/monoclinic/triclinic）
    morphology: str = ""             # 单晶/多晶/非晶
    phase_composition: str = ""      # 相组成描述
    is_multiphase: bool = False      # 是否存在多相结构
    element_composition: str = ""    # 元素组成（如 "Bi, Te"）
    element_ratio: str = ""          # 元素比例（如 "2:3"）
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
    """材料性能实体（性能指标）：ZT、功率因子、热导率等。

    深度分析扩展（性质 → 机制 → 目标性能）：
    - mechanism：物理机制解释（如"晶格缺陷增强声子散射"）
    - impact_on_target：对目标性能的影响（如"降低晶格热导率，有利于提升 ZT"）
    - evidence_level：证据等级（A/B/C/D/E，见 EvidenceLevel）
    - evidence_count：支撑文献数量
    - data_type：实验值/理论值/数据库/推断（PropertyDataKind）
    - test_temperature：测试温度（如 "300-500 K"）
    - source_type：数据源（paper / materials_project / sciverse / llm_inference）
    """

    property_id: EntityId
    material_id: EntityId  # 归属材料
    property_name: str  # 性能名称（如 "ZT"、"thermal_conductivity"）
    property_name_cn: str = ""  # 中文名（如 "热电优值"）
    value: str = ""  # 数值（含单位，如 "1.05 at 800K"）
    value_num: Optional[float] = None  # 数值部分（若可解析）
    unit: str = ""  # 单位（如 "W/mK"）
    condition: str = ""  # 测试条件（温度/压力等）
    # ===== 深度分析扩展：机制 / 证据 / 数据类型 =====
    mechanism: str = ""            # 物理机制解释
    impact_on_target: str = ""     # 对目标性能的影响
    evidence_level: str = "E"      # 证据等级 A/B/C/D/E
    evidence_count: int = 0        # 支撑文献数量
    data_type: str = ""            # experimental/theoretical/database/inferred
    test_temperature: str = ""     # 测试温度（如 "300-500 K"）
    source_type: str = ""          # paper/materials_project/sciverse/llm_inference
    paper_id: Optional[EntityId] = None
    paper_title: str = ""
    confidence: float = 0.0
    source_snippet: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_stage: str = "research"


class MaterialSynthesis(BaseModel):
    """材料合成方法实体（合成条件）：温度、压力、时间、前驱体、工艺步骤。

    深度分析扩展（从「方法名称」升级为「实验流程 + 路线决策」）：
    - 完整实验参数：前驱体比例/溶剂/pH/升温速率/搅拌/陈化/干燥/煅烧/冷却/后处理/设备/产率…
    - workflow_steps：分步实验流程 [{step, operation, parameter, unit, source, is_literal}]
    - risks：风险清单 [{risk, level, source, evidence}]
    - reproducibility_score / reproducibility_factors：可复现性评分（0~100 + 因素分解）
    - evidence_level / evidence_count：证据等级与文献数

    硬约束：所有参数不得编造，无可靠来源时留空并标记 evidence_level=E（LLM 推断）。
    """

    synthesis_id: EntityId
    material_id: EntityId  # 归属材料
    method: str = ""  # 工艺方法（如 "solid-state reaction"、"CVD"）
    precursors: list[str] = Field(default_factory=list)  # 前驱体
    temperature: str = ""  # 温度条件（如 "500°C for 12h"）
    pressure: str = ""  # 压力条件
    atmosphere: str = ""  # 气氛（如 "Ar"、"N2"）
    duration: str = ""  # 时间
    steps: str = ""  # 工艺步骤描述（自由文本，与 workflow_steps 互补）
    # ===== 深度分析扩展：完整实验参数 =====
    precursor_ratio: str = ""      # 前驱体比例（如 "1:1"、"stoichiometric"）
    solvent: str = ""              # 溶剂
    solvent_ratio: str = ""        # 溶剂比例
    heating_rate: str = ""         # 升温速率（如 "5 °C/min"）
    ph: str = ""                   # pH 值
    stirring: str = ""             # 搅拌条件（如 "600 rpm, 30 min"）
    aging_time: str = ""           # 陈化时间
    drying_temperature: str = ""   # 干燥温度
    calcination_temperature: str = ""  # 煅烧/退火温度
    calcination_time: str = ""     # 煅烧/退火时间
    cooling_method: str = ""       # 冷却方式（自然冷却/淬火/随炉…）
    post_treatment: str = ""       # 后处理
    equipment: list[str] = Field(default_factory=list)  # 设备（如 "管式炉"、"高压釜"）
    yield_: str = ""               # 产率（字段名避免关键字 yield）
    phase_purity: str = ""         # 相纯度
    particle_size: str = ""        # 粒径
    # ===== 深度分析扩展：分步流程 / 风险 / 可复现性 / 证据 =====
    workflow_steps: list[dict[str, Any]] = Field(default_factory=list)
    #   [{step, operation, parameter, unit, source, is_literal}]
    risks: list[dict[str, Any]] = Field(default_factory=list)
    #   [{risk, level(Low/Medium/High), source, evidence}]
    reproducibility_score: Optional[int] = None  # 可复现性评分 0~100
    reproducibility_factors: dict[str, Any] = Field(default_factory=dict)
    #   {param_completeness, precursor_completeness, equipment_completeness,
    #    key_param_clarity, independent_sources, result_consistency}
    evidence_level: str = "E"      # 证据等级 A/B/C/D/E
    evidence_count: int = 0        # 独立支撑文献数
    paper_id: Optional[EntityId] = None
    paper_title: str = ""
    confidence: float = 0.0
    source_snippet: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_stage: str = "research"


class ResearchGap(BaseModel):
    """研究缺口实体（Task 3：Research Gap 识别）。

    由 cross_validate 的 gaps（子问题字符串列表）升级为结构化实体：
    - 类型：contradiction（矛盾结论）/ unexplored（未被探索方向）/
      missing_link（缺失知识连接）
    - 每条 Gap 带证据链（可溯源 paper_id + snippet，满足赛题「文献溯源完整性」）
    - 可操作性评估与优先级排序，供下游 ideation/discovery/报告消费
    """

    gap_id: EntityId
    # 类型：contradiction / unexplored / missing_link
    gap_type: str = "unexplored"
    # 一句话陈述（简明，供下游拼 prompt / 展示）
    statement: str = ""
    # 详细说明（背景、现状、为什么是缺口）
    detail: str = ""
    # 证据链：[{paper_id, title, snippet}]，可溯源
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # 数据库证据链（可选）：[{formula, mp:{...}, oqmd:{...}, nomad:{...}}]，
    # 来自 Materials Project / OQMD / NOMAD 交叉查询，与 evidence 构成双证据链
    db_evidence: list[dict[str, Any]] = Field(default_factory=list)
    # 关联材料（如 ["SnSe", "Mg3Sb2"]）
    related_materials: list[str] = Field(default_factory=list)
    # 可操作性：high / medium / low
    actionability: str = "medium"
    # 优先级 1（最高）~ 5（最低）
    priority: int = 3
    # 来源：llm / data_driven / hybrid
    source: str = "llm"
    # 建议行动（如 补充实验/进一步检索/组合验证）
    suggested_actions: list[str] = Field(default_factory=list)
    # 关联子问题（来源 cross_validate 报告）
    subquery: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ResearchConflict(BaseModel):
    """文献冲突实体（交叉验证产出，Task：冲突可视化）。

    由 CrossValidateAgent 的 conflicts（子问题粒度冲突项）落库：
    - claim：冲突陈述（同一问题下相互矛盾的论断）
    - sources：立场证据 [{paper_id, title, stance}]，stance ∈ support / refute，
      供 Web 展示「争议双方来源」（可点击跳转论文页溯源）
    - resolution：处置建议（采纳来源 / 标记存疑 / 需进一步检索）
    - 通过 conflict 关联的 paper_id 与 Claim.evidence_refs 求交集，
      实现「Claim 处于争议中」的标记
    """

    conflict_id: EntityId
    # 冲突陈述（子问题粒度）
    claim: str = ""
    # 立场证据：[{paper_id, title, stance}]，stance ∈ support / refute
    sources: list[dict[str, Any]] = Field(default_factory=list)
    # 处置建议
    resolution: str = ""
    confidence: float = 0.0
    # 来源子问题
    subquery: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_stage: str = "research"


# ===== 材料深度分析结果模型（Task 2 深度分析扩展）=====
# 这些是「分析层」的输出契约：由规则引擎（确定性）与 LLM 分析节点（语义）
# 双通道产出，API 动态组装返回，不单独落库（避免与三元组实体重复持久化）。

class PropertyMechanism(BaseModel):
    """性质 → 机制 → 目标性能 的关系解释。

    把「Band Gap = xxx」升级为「性质 → 物理机制 → 性能影响」的因果链。
    """

    property: str = ""            # 性质 key（如 thermal_conductivity）
    property_cn: str = ""         # 性质中文名
    value: str = ""               # 数值
    unit: str = ""                # 单位
    mechanism: str = ""           # 物理机制（如"晶格缺陷增强声子散射"）
    impact_on_target: str = ""    # 对目标性能的影响
    evidence_level: str = "E"     # 证据等级 A/B/C/D/E
    evidence: list[str] = Field(default_factory=list)  # 支撑证据（paper_title/snippet）


class TargetDecomposition(BaseModel):
    """目标性能因果拆解。

    如目标 ZT：ZT = S²σT/κ，拆解出 Seebeck/电导率/功率因子/热导率/温度，
    并给出材料优势、瓶颈、最值得优化的变量（结合文献证据）。
    """

    target: str = ""              # 目标性能（如 ZT）
    formula: str = ""             # 目标公式（如 "ZT = S²σT/κ"）
    factors: list[dict[str, Any]] = Field(default_factory=list)
    #   [{factor, factor_cn, value, unit, role}]
    strengths: list[str] = Field(default_factory=list)   # 当前材料优势
    bottlenecks: list[str] = Field(default_factory=list) # 当前材料瓶颈
    optimization_priority: list[dict[str, Any]] = Field(default_factory=list)
    #   [{priority, variable, reason}]
    evidence: list[str] = Field(default_factory=list)


class ComparisonCell(BaseModel):
    """对比矩阵单元格：多文献值时显示范围而非单选。"""

    material: str = ""            # 材料名
    value: str = ""               # 展示值（范围如 "0.8–1.2"）
    unit: str = ""                # 统一后的单位
    source: str = ""              # 数据源
    data_type: str = ""           # experimental/theoretical/database/inferred
    test_temperature: str = ""    # 测试温度
    confidence: float = 0.0       # 置信度
    evidence_level: str = "E"     # 证据等级
    paper_count: int = 0          # 文献数量
    missing: bool = False         # 数据缺失标记（不伪造）


class CandidateRanking(BaseModel):
    """材料候选排序条目（材料选择决策）。"""

    material: str = ""            # 材料名
    formula: str = ""             # 化学式
    composite_score: float = 0.0  # 综合评分 0~100
    dimensions: dict[str, Any] = Field(default_factory=dict)
    #   {target_potential, evidence_strength, structure_match,
    #    synthesis_feasibility, stability, novelty}（各维度评分与权重）
    strengths: list[str] = Field(default_factory=list)  # 优势
    risks: list[str] = Field(default_factory=list)      # 风险
    reason: str = ""              # 推荐理由
    evidence: list[str] = Field(default_factory=list)   # 评分依据（可溯源）


class SynthesisRouteCompare(BaseModel):
    """合成路线对比条目。"""

    method: str = ""              # 方法名
    method_category: str = ""     # 工艺类别
    temperature: str = ""         # 温度
    time: str = ""                # 时间
    equipment: str = ""           # 设备
    cost: str = ""                # 成本（低/中/高）
    phase_purity: str = ""        # 相纯度（高/中/低）
    particle_control: str = ""    # 粒径控制（高/中/低）
    scale_difficulty: str = ""    # 放大难度（低/中/高）
    recommendation_score: float = 0.0  # 推荐度 0~10
    advantages: list[str] = Field(default_factory=list)  # 优势
    risks: list[dict[str, Any]] = Field(default_factory=list)  # 风险 [{risk, level, source}]
    reproducibility_score: Optional[int] = None  # 可复现性评分
    evidence_level: str = "E"     # 证据等级
    evidence: list[str] = Field(default_factory=list)  # 证据


class SynthesisParameterSensitivity(BaseModel):
    """合成参数敏感性分析结果。"""

    high_impact: list[dict[str, Any]] = Field(default_factory=list)
    #   [{parameter, reason, evidence}]
    low_impact: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class MaterialProfile(BaseModel):
    """材料性质画像：聚合一个材料的全部深度分析结果。

    API /materials/{id}/profile 返回此结构，前端「材料画像页」消费。
    """

    material_id: EntityId = ""
    name: str = ""                # 材料名
    formula: str = ""             # 化学式
    category: str = ""            # 材料体系
    # 基础结构
    structure: dict[str, Any] = Field(default_factory=dict)
    # 性质分组：{电子性质: [...], 热学性质: [...], 光学性质: [...], 力学性质: [...], 化学稳定性: [...]}
    properties: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # 性质 → 机制 → 目标性能
    mechanisms: list[dict[str, Any]] = Field(default_factory=list)
    # 目标性能因果拆解
    target_decomposition: dict[str, Any] = Field(default_factory=dict)
    # 材料横向对比（同体系材料对比矩阵）
    comparison: dict[str, Any] = Field(default_factory=dict)
    # 候选排序（材料选择决策）
    ranking: list[dict[str, Any]] = Field(default_factory=list)
    # 合成路线（对比 + 推荐 + 风险 + 可复现性）
    synthesis: dict[str, Any] = Field(default_factory=dict)
    # 性质—合成联合分析（工艺→结构→性质→性能链路）
    joint_analysis: dict[str, Any] = Field(default_factory=dict)
