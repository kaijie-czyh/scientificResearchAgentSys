"""阶段共享常量。

集中定义：
- 系统依赖注入键：LLMRegistry / KnowledgeStore / ArtifactManager / ProvenanceValidator
  各 stage 节点统一从 context 取这些依赖（由外部框架注入），避免节点内部重复初始化。
- 跨阶段流转的域键：每个阶段产出物 ID 通过这些键在节点间传递。
- StageCheckpoint：带显式 schema 声明的检查点节点基类。

约定：ContextKey[T] 是泛型数据类，用 ``ContextKey[类型]("name")`` 构造实例。
"""
from __future__ import annotations

from core.artifacts import ArtifactManager, ProvenanceValidator
from core.knowledge import KnowledgeStore
from core.llm import LLMRegistry
from core.orchestration.context import ContextKey
from core.orchestration.node import CheckpointNode, NodeInput, NodeOutput

# ===== 系统依赖注入键 =====
# 实际运行时由编排框架在执行前注入到 ExecutionContext
LLM_REGISTRY = ContextKey[LLMRegistry]("system.llm_registry")
KNOWLEDGE_STORE = ContextKey[KnowledgeStore]("system.knowledge_store")
ARTIFACT_MANAGER = ContextKey[ArtifactManager]("system.artifact_manager")
PROVENANCE_VALIDATOR = ContextKey[ProvenanceValidator]("system.provenance_validator")

# 全局开关：dry_run=True 时不执行真实 LLM 调用，用占位数据返回
DRY_RUN = ContextKey[bool]("system.dry_run")

# 项目根目录与项目目录（供 ExperimentRunTool 写入并执行实验代码）
# 类型为 pathlib.Path，运行时由 Pipeline 注入
PROJECT_ROOT = ContextKey[object]("system.project_root")
PROJECT_DIR = ContextKey[object]("system.project_dir")


# ===== research 阶段域键 =====
RESEARCH_TOPIC = ContextKey[str]("research.topic")
RESEARCH_KEYWORDS = ContextKey[list[str]]("research.keywords")
RESEARCH_QUERY_STRATEGY = ContextKey[str]("research.query_strategy")
RESEARCH_TOPIC_CONFIRMED = ContextKey[bool]("research.topic_confirmed")
# 子问题分解（借鉴 GPT-Researcher）：把主题拆为 5-10 个子问题用于并行检索
RESEARCH_SUBQUERIES = ContextKey[list[str]]("research.subqueries")
RESEARCH_PAPER_METAS = ContextKey[list[dict]]("research.paper_metas")
# 相关性筛选后（借鉴 PaperQA filter）：保留高相关性候选
RESEARCH_FILTERED_PAPER_METAS = ContextKey[list[dict]]("research.filtered_paper_metas")
RESEARCH_PAPER_IDS = ContextKey[list[str]]("research.paper_ids")
# 检索证据链（审计轨迹）：[{subquery, source, title, external_id, offset,
# evidence_score, snippet, paper_id}]，Sciverse 调用记录天然构成可审计证据链
RESEARCH_EVIDENCE_CHAIN = ContextKey[list[dict]]("research.evidence_chain")
# 交叉验证报告（借鉴 GPT-Researcher）：多源冲突时的可信度评分与处置
RESEARCH_CROSS_VALIDATION_REPORT = ContextKey[dict]("research.cross_validation_report")
# 材料知识抽取（Task 2）：{materials: [...], properties: [...], synthesis: [...]}
# 从入库论文摘要中抽取「材料-性能-合成」三元组，供 ideation/design 复用
RESEARCH_MATERIAL_KNOWLEDGE = ContextKey[dict]("research.material_knowledge")
# 研究缺口清单（Task 3）：[{gap_id, gap_type, statement, detail, evidence,
# related_materials, actionability, priority, source, suggested_actions, subquery}]
# 由 ResearchGapIdentifyAgent 在 cross_validate 之后生成，
# 供 ideation（思路生成）/ discovery（假设种子 gap_ref）/ 调研报告消费
RESEARCH_GAP_REPORT = ContextKey[list[dict]]("research.gap_report")


# ===== ideation 阶段域键 =====
IDEATION_IDEA_IDS = ContextKey[list[str]]("ideation.idea_ids")
IDEATION_DISCUSSION_NOTES = ContextKey[str]("ideation.discussion_notes")
IDEATION_VALIDATED_IDEA_IDS = ContextKey[list[str]]("ideation.validated_idea_ids")
IDEATION_DRAFT_CLAIM_IDS = ContextKey[list[str]]("ideation.draft_claim_ids")


# ===== design 阶段域键 =====
# 原子概念分解（借鉴 AI-Researcher）：方法拆为原子概念，建立公式↔代码双向映射
DESIGN_ATOM_CONCEPTS = ContextKey[list[dict]]("design.atom_concepts")
# 公式↔代码映射表：[{concept, formula_latex, code_stub, status}]
DESIGN_FORMULA_CODE_MAP = ContextKey[list[dict]]("design.formula_code_map")
DESIGN_METHOD_CONTENT = ContextKey[str]("design.method_content")
DESIGN_METHOD_ARTIFACT_ID = ContextKey[str]("design.method_artifact_id")
DESIGN_CLAIM_IDS = ContextKey[list[str]]("design.claim_ids")


# ===== experiment 阶段域键 =====
EXPERIMENT_CONFIGS = ContextKey[list[dict]]("experiment.configs")
# 导师-学生迭代（借鉴 AI-Researcher）：Code Agent 生成 + Advisor Agent 审查
EXPERIMENT_CODE = ContextKey[dict]("experiment.code")  # {path, content, language}
EXPERIMENT_REVIEW_NOTES = ContextKey[list[dict]]("experiment.review_notes")  # 多轮审查记录
EXPERIMENT_IDS = ContextKey[list[str]]("experiment.experiment_ids")
EXPERIMENT_ANOMALY_REPORT = ContextKey[str]("experiment.anomaly_report")
EXPERIMENT_RESULT_ARTIFACT_IDS = ContextKey[list[str]]("experiment.result_artifact_ids")
# 实验成败评估：{success, verified_claim_ids, refuted_claim_ids, recommendation, summary}
# 决定是否进入 writing 阶段（实验失败是常态，不应强行写论文）
EXPERIMENT_OUTCOME = ContextKey[dict]("experiment.outcome")


# ===== writing 阶段域键 =====
WRITING_STYLE_PROFILE = ContextKey[str]("writing.style_profile")
# 层级式生成（借鉴 AI-Researcher）：大纲→填充→校对三阶段
WRITING_OUTLINE = ContextKey[dict]("writing.outline")  # {sections: [{title, claim_ids, key_points}]}
WRITING_SECTIONS = ContextKey[list[dict]]("writing.sections")  # [{title, content, word_count}]
WRITING_DRAFT_CONTENT = ContextKey[str]("writing.draft_content")
WRITING_REVIEW_NOTES = ContextKey[str]("writing.review_notes")
WRITING_PAPER_DRAFT_ARTIFACT_ID = ContextKey[str]("writing.paper_draft_artifact_id")


# ===== topic_discovery 阶段域键（方向推荐：研究趋势发现）=====
# 用户研究兴趣输入
TOPIC_DISCOVERY_INTEREST = ContextKey[str]("topic_discovery.interest")
# 关键词年度频率数据：{keyword: {year: count}}
TOPIC_DISCOVERY_TRENDS = ContextKey[dict]("topic_discovery.trends")
# 趋势分析结果：{emerging: [...], stable: [...], saturated: [...], all_keywords: [...]}
TOPIC_DISCOVERY_ANALYSIS = ContextKey[dict]("topic_discovery.analysis")
# LLM 推荐的研究主题列表：[{topic, rationale, innovation_point, recommended_materials, trend_data}]
TOPIC_DISCOVERY_RECOMMENDATIONS = ContextKey[list[dict]]("topic_discovery.recommendations")
# 用户选择的推荐主题（写入 RESEARCH_TOPIC，接入原有 research 流程）
TOPIC_DISCOVERY_SELECTED_TOPIC = ContextKey[str]("topic_discovery.selected_topic")


# ===== discovery 阶段域键（路线 A：构效关系发现）=====
# 候选构效关系假设（搜索种子）：[{hypothesis, variables, target_property, rationale, gap_ref}]
DISCOVERY_HYPOTHESES = ContextKey[list[dict]]("discovery.hypotheses")
# 搜索空间定义：{variables:[{name, range, unit, type}], target_property, constraints, literature_points:[...]}
DISCOVERY_SEARCH_SPACE = ContextKey[dict]("discovery.search_space")
# LLM 引导搜索产出的候选构效关系：[{config, predicted_property, plausibility, mechanism, novelty}]
DISCOVERY_CANDIDATES = ContextKey[list[dict]]("discovery.candidates")
# 验证后的构效关系发现：[{relationship, evidence_refs, novelty, mechanism, confidence}]
DISCOVERY_RELATIONSHIPS = ContextKey[list[dict]]("discovery.relationships")
# 构效关系发现报告 Artifact ID
DISCOVERY_REPORT_ARTIFACT_ID = ContextKey[str]("discovery.report_artifact_id")


class StageCheckpoint(CheckpointNode):
    """阶段检查点节点。

    在关键决策点前放置，执行时对 context 做快照，便于失败回滚。
    补充显式 schema 声明以满足验证导向要求。
    """

    input_schema = NodeInput
    output_schema = NodeOutput
    output_keys: dict = {}
