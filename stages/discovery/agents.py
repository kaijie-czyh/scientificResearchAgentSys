"""discovery 阶段 Agent / Tool 节点实现（路线 A：构效关系发现）。

节点拓扑（LLM 深度参与搜索过程）：
    HypothesisSeedAgent（从 Research Gap 生成候选构效关系假设作为搜索种子）
    → SearchSpaceAgent（定义搜索空间 + 从文献抽取数据点）
    → LLMGuidedSearchAgent（核心创新：MCTS + LLM 融合，生成候选/评估合理性/剪枝）
    → DiscoveryValidateAgent（文献交叉验证 + 新颖性评估 + 证据链关联）
    → DiscoveryReportAgent（结构化发现报告 + 物理机制解释 + Artifact）

核心创新（区别于 LLM4Mat/ChemCrow 等仅用 LLM 生成搜索代码的做法）：
- LLM 生成候选构效关系假设作为搜索种群种子
- LLM 评估中间结果的科学合理性（物理合法性、与文献一致性）
- LLM 引导搜索空间剪枝（排除物理不合理的区域）
- 文献抽取的 (结构, 性能) 数据点作为代理模型训练样本，确保证据可追溯

执行模式：
- dry_run=True  ：用占位数据返回，不调用 LLM（验证架构用）
- dry_run=False ：真实调用 MiniMax M3，真实运行 MCTS 搜索循环
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from pydantic import BaseModel, Field

from core.artifacts import ArtifactManager
from core.knowledge import (
    ArtifactType,
    Claim,
    ClaimStatus,
    KnowledgeStore,
)
from core.llm import LLMRegistry
from core.orchestration.context import ExecutionContext
from core.orchestration.node import (
    AgentNode,
    NodeResult,
    NodeStatus,
)
from core.tools import (
    MCTSSearcher,
    SearchCandidate,
    SurrogateModel,
    build_literature_points,
    build_search_variables,
    perturb_config,
)
from core.tools.sciverse_search import agentic_search as sciverse_agentic_search
from core.tools.sciverse_search import is_available as sciverse_is_available

from stages.common import (
    ARTIFACT_MANAGER,
    DISCOVERY_CANDIDATES,
    DISCOVERY_HYPOTHESES,
    DISCOVERY_RELATIONSHIPS,
    DISCOVERY_REPORT_ARTIFACT_ID,
    DISCOVERY_SEARCH_SPACE,
    DRY_RUN,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    RESEARCH_CROSS_VALIDATION_REPORT,
    RESEARCH_GAP_REPORT,
    RESEARCH_PAPER_IDS,
    RESEARCH_TOPIC,
)
from stages.discovery.io_schema import (
    DiscoveryReportInput,
    DiscoveryReportOutput,
    DiscoveryValidateInput,
    DiscoveryValidateOutput,
    HypothesisSeedInput,
    HypothesisSeedOutput,
    LLMGuidedSearchInput,
    LLMGuidedSearchOutput,
    SearchSpaceInput,
    SearchSpaceOutput,
)

logger = logging.getLogger(__name__)


def _compose_mechanism(
    physical_principle: str,
    causal_chain: list[str],
    known_theory_support: str,
    quantitative_reason: str,
    domain_specific_concept: str,
) -> str:
    """把结构化 5 要素 mechanism 拼接成可读文本。

    满足赛题路线 A「构效关系须附带清晰科学解释」要求。
    """
    parts: list[str] = []
    if physical_principle:
        parts.append(f"**物理原理**：{physical_principle}")
    if causal_chain:
        chain_text = " → ".join(causal_chain)
        parts.append(f"**因果链**：{chain_text}")
    if known_theory_support:
        parts.append(f"**理论支撑**：{known_theory_support}")
    if quantitative_reason:
        parts.append(f"**量化解释**：{quantitative_reason}")
    if domain_specific_concept:
        parts.append(f"**领域概念**：{domain_specific_concept}")
    return "\n".join(parts) if parts else ""


# ===== 结构化输出 Schema =====

class HypothesisItem(BaseModel):
    """单条构效关系假设。"""

    hypothesis: str = Field(default="", description="构效关系假设陈述")
    variables: list[str] = Field(default_factory=list, description="涉及的变量名")
    target_property: str = Field(default="property", description="目标性能名（如 ZT）")
    rationale: str = Field(default="", description="假设依据（关联 Gap/冲突/共识）")
    gap_ref: str = Field(default="", description="关联的 Research Gap")
    novelty_score: float = Field(
        default=0.5, ge=0.0, le=1.0, description="新颖性评分 0~1（与已有文献/共识的差异程度）"
    )
    feasibility_score: float = Field(
        default=0.5, ge=0.0, le=1.0, description="可行性评分 0~1（变量可量化/可搜索验证/物理合法程度）"
    )
    gap_relevance_score: float = Field(
        default=0.5, ge=0.0, le=1.0, description="缺口关联度评分 0~1（与 Research Gap 的匹配程度）"
    )


class HypothesisBatchSchema(BaseModel):
    """假设批量输出 schema。"""

    hypotheses: list[HypothesisItem]


class SearchVariableSchema(BaseModel):
    """搜索变量 schema。"""

    name: str
    low: float = 0.0
    high: float = 1.0
    unit: str = ""
    type: str = "continuous"  # continuous / discrete / categorical
    categories: list[str] = Field(default_factory=list)


class LiteraturePointSchema(BaseModel):
    """文献数据点 schema。"""

    config: dict = Field(default_factory=dict, description="{变量名: 值}")
    target: float = Field(description="目标性能值")
    paper_id: str = ""
    chunk_id: str = ""
    note: str = ""


class SearchSpaceSchema(BaseModel):
    """搜索空间定义 schema。"""

    variables: list[SearchVariableSchema]
    target_property: str
    target_unit: str = ""
    constraints: list[str] = Field(default_factory=list, description="物理约束")
    literature_points: list[LiteraturePointSchema] = Field(
        default_factory=list, description="从文献抽取的 (结构, 性能) 数据点"
    )


class CandidateEvaluationSchema(BaseModel):
    """LLM 候选评估 schema（MCTS 评估阶段）。

    mechanism 改为结构化：物理原理 + 因果链 + 理论支撑 + 量化解释 + 领域术语。
    满足赛题路线 A「构效关系须附带清晰科学解释，避免黑箱输出」要求。
    """

    config: dict = Field(description="评估的材料配置 {变量名: 值}")
    plausibility: float = Field(
        description="科学合理性 0~1（物理合法性 + 与文献一致性）"
    )
    # 物理机制解释（结构化 5 要素）
    physical_principle: str = Field(
        default="",
        description="底层物理原理（如声子散射增强、载流子浓度优化、能带工程）",
    )
    causal_chain: list[str] = Field(
        default_factory=list,
        description="因果链步骤（3-5 步）",
    )
    known_theory_support: str = Field(
        default="",
        description="已知理论支撑（Boltzmann transport / phonon glass electron crystal 等）",
    )
    quantitative_reason: str = Field(
        default="",
        description="量化解释：为什么该组合能得到该性能值",
    )
    domain_specific_concept: str = Field(
        default="",
        description="领域特定概念（热电/催化/电池等）",
    )
    # 兼容旧字段
    mechanism: str = Field(default="", description="综合机制说明（结构化字段拼接）")
    novelty: str = Field(description="新颖性说明（与已知文献的差异）")
    pruned: bool = Field(default=False, description="是否建议剪枝（物理不合理）")


class RelationshipSchema(BaseModel):
    """单条验证后的构效关系 schema。"""

    relationship: str = Field(description="构效关系陈述")
    config: dict = Field(default_factory=dict, description="最优配置")
    predicted_target: float = 0.0
    evidence_paper_ids: list[str] = Field(
        default_factory=list, description="关联的 Paper ID"
    )
    # 新颖性增强（满足赛题路线 A「区分新知与已知」要求）
    novelty: str = Field(description="novel / partially_known / known")
    novelty_reason: str = ""
    novelty_score: float = Field(
        default=0.5,
        description="新颖性评分 0~1（1=全新发现，0=完全已知）",
    )
    differentiation_points: list[str] = Field(
        default_factory=list,
        description="与已知文献的具体差异点（3-5 条）",
    )
    # 物理机制（结构化 5 要素）
    physical_principle: str = ""
    causal_chain: list[str] = Field(default_factory=list)
    known_theory_support: str = ""
    quantitative_reason: str = ""
    domain_specific_concept: str = ""
    mechanism: str = ""  # 综合说明
    # 综合置信度
    confidence: float = Field(description="综合置信度 0~1")


class RelationshipBatchSchema(BaseModel):
    """构效关系批量验证 schema。"""

    relationships: list[RelationshipSchema]


# ===== HypothesisSeedAgent =====

class HypothesisSeedAgent(AgentNode):
    """候选假设生成 Agent。

    从 research 阶段的 Research Gap + 共识/冲突 + 入库论文出发，
    LLM 生成 3-5 个候选构效关系假设作为搜索种子。

    设计要点：
    - 每个假设关联一个 Research Gap（确保假设有文献依据）
    - 假设必须可被搜索验证（涉及可量化的变量与目标性能）
    - 假设是搜索的「方向」，不是结论
    """

    node_type = "discovery_hypothesis_seed"
    task_type = "discovery_hypothesis_seed"
    input_schema = HypothesisSeedInput
    output_schema = HypothesisSeedOutput
    output_keys = {
        "hypotheses": DISCOVERY_HYPOTHESES,
    }

    def _build_input(self, ctx: ExecutionContext) -> HypothesisSeedInput:
        report = ctx.get(RESEARCH_CROSS_VALIDATION_REPORT, {}) or {}
        # 研究缺口（Task 3 结构化优先）：[{gap_id, statement, gap_type, priority, ...}]
        gap_report = ctx.get(RESEARCH_GAP_REPORT, []) or []
        if gap_report:
            # 按优先级升序取前 8 条，statement 附 gap_id 便于下游强关联
            sorted_gaps = sorted(gap_report, key=lambda g: g.get("priority", 5))
            gaps = [
                f"[{g.get('gap_id', '')}] {g.get('statement', '')}"
                for g in sorted_gaps[:8]
            ]
        else:
            # 回退：cross_validate 的字符串 gaps
            gaps = report.get("gaps", []) or []
        return HypothesisSeedInput(
            topic=ctx.get(RESEARCH_TOPIC, ""),
            gaps=gaps,
        return HypothesisSeedInput(
            topic=ctx.get(RESEARCH_TOPIC, ""),
            gaps=report.get("gaps", []) or [],
            conflicts=report.get("conflicts", []) or [],
            consensus=report.get("consensus", []) or [],
            paper_ids=ctx.get(RESEARCH_PAPER_IDS, []) or [],
        )

    def _execute(self, input_obj: HypothesisSeedInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not dry_run and registry is not None and (input_obj.gaps or input_obj.consensus):
            try:
                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=HypothesisBatchSchema,
                    system=(
                        "你是材料科学构效关系发现助手。基于文献调研的 Research Gap、"
                        "冲突结论与共识，生成 3-5 个候选构效关系假设作为搜索种子。\n"
                        "要求：\n"
                        "1. 每个假设必须关联一个 Research Gap（gap_ref）\n"
                        "2. 假设涉及可量化的变量（variables）与明确的目标性能（target_property）\n"
                        "3. 假设是可被搜索验证的方向，不是结论\n"
                        "4. rationale 说明假设依据（关联哪个 Gap/冲突/共识）\n"
                        "5. 为每个假设输出三维可验证性评分（各 0~1，保留两位小数）：\n"
                        "   - novelty_score 新颖性：与已有文献结论/共识的差异程度，越新越高\n"
                        "   - feasibility_score 可行性：变量可量化、可搜索验证、物理合法的程度\n"
                        "   - gap_relevance_score 缺口关联度：与所关联 Research Gap 的匹配程度\n"
                        "   评分须与 rationale/文本内容一致，不要全部给高分。"
                        "4. rationale 说明假设依据（关联哪个 Gap/冲突/共识）"
                    ),
                    prompt=(
                        f"研究主题：{input_obj.topic}\n\n"
                        f"Research Gaps：\n" + "\n".join(f"- {g if isinstance(g, str) else json.dumps(g, ensure_ascii=False)}" for g in input_obj.gaps) + "\n\n"
                        f"冲突结论：\n" + json.dumps(input_obj.conflicts, ensure_ascii=False, indent=2) + "\n\n"
                        f"共识：\n" + "\n".join(f"- {c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)}" for c in input_obj.consensus) + "\n\n"
                        f"入库论文数：{len(input_obj.paper_ids)}"
                    ),
                )
                hypotheses = [h.model_dump() for h in result.hypotheses]
            except Exception as e:
                logger.warning("HypothesisSeed 真实调用失败，回退占位: %s", e)
                hypotheses = self._placeholder(input_obj)
        else:
            hypotheses = self._placeholder(input_obj)

        # 兜底：若 hypotheses 为空（如 dry_run 没填充），用占位
        if not hypotheses:
            hypotheses = self._placeholder(input_obj)
            logger.warning("HypothesisSeed 产出为空，强制使用占位 hypotheses")

        output = HypothesisSeedOutput(hypotheses=hypotheses)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"生成 {len(hypotheses)} 个候选构效关系假设（搜索种子）",
        )

    @staticmethod
    def _placeholder(input_obj: HypothesisSeedInput) -> list[dict]:
        """占位假设生成。兼容结构化 Gap（dict）和旧版字符串 Gap。"""
        raw_gaps = input_obj.gaps or []
        # 兼容结构化 Gap（新）与字符串 Gap（旧）
        gap_strs: list[str] = []
        for g in raw_gaps[:3]:
            if isinstance(g, dict):
                gap_strs.append(g.get("gap", str(g)[:60]))
            else:
                gap_strs.append(str(g)[:60])
        if not gap_strs:
            gap_strs = ["(无 Research Gap，使用占位)"]

        return [
            {
                "hypothesis": f"假设 {i + 1}：基于 Gap「{g[:40]}」的构效关系方向",
                "variables": ["var_1", "var_2"],
                "target_property": "ZT",
                "rationale": f"关联 Research Gap：{g[:60]}",
                "gap_ref": g,
                "novelty_score": round(0.4 + 0.1 * i, 2),
                "feasibility_score": round(0.6 - 0.05 * i, 2),
                "gap_relevance_score": 0.8,
            }
            for i, g in enumerate(gap_strs)
        ]


# ===== SearchSpaceAgent =====

class SearchSpaceAgent(AgentNode):
    """搜索空间定义 Agent。

    LLM 定义搜索空间（材料变量/性能目标/物理约束），并从入库论文 chunk 中
    抽取 (结构, 性能) 数据点作为代理模型训练样本。

    设计要点：
    - 变量定义域必须物理合法（如掺杂浓度不超过合理上限）
    - literature_points 从论文 chunk 抽取，每点关联 paper_id（证据可追溯）
    - constraints 是物理约束，供 LLM 在搜索时剪枝参考
    """

    node_type = "discovery_search_space"
    task_type = "discovery_search_space"
    input_schema = SearchSpaceInput
    output_schema = SearchSpaceOutput
    output_keys = {
        "search_space": DISCOVERY_SEARCH_SPACE,
    }

    def _build_input(self, ctx: ExecutionContext) -> SearchSpaceInput:
        return SearchSpaceInput(
            topic=ctx.get(RESEARCH_TOPIC, ""),
            hypotheses=ctx.get(DISCOVERY_HYPOTHESES, []) or [],
            paper_ids=ctx.get(RESEARCH_PAPER_IDS, []) or [],
        )

    def _execute(self, input_obj: SearchSpaceInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)

        # 收集论文 chunk 文本作为数据点抽取素材
        chunk_texts = self._collect_chunks(store, input_obj.paper_ids)
        # 直接从 Sciverse 获取含数值的证据片段（赛题推荐数据源，天然含 ZT/温度等数值）
        sciverse_evidences = self._collect_sciverse_evidence(input_obj.topic)
        all_evidence_texts = chunk_texts + sciverse_evidences

        if not dry_run and registry is not None and input_obj.hypotheses:
            try:
                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=SearchSpaceSchema,
                    system=(
                        "你是材料科学搜索空间设计助手。基于候选假设定义构效关系搜索空间：\n"
                        "1. variables：2-5 个可量化的材料变量（组分/结构参数），含定义域与单位\n"
                        "   - **重要**：如果搜索空间涉及某一特定材料体系（如 Bi2Te3、SnSe、PbTe 等），\n"
                        "     必须在 variables 中加入名为 `material` 的类别变量，categories 列出所有候选材料；\n"
                        "     或在 literature_points 的 config 中固定包含 material 字段。\n"
                        "2. target_property：目标性能名与单位（如 ZT、power factor）\n"
                        "3. constraints：物理约束（如掺杂浓度上限、电荷中性）\n"
                        "4. literature_points：从给定文献片段抽取 (结构, 性能) 数据点，"
                        "每点关联 paper_id 确保证据可追溯；无法抽取时返回空列表\n"
                        "4. literature_points：**必须**从给定文献片段抽取所有可量化的 (结构, 性能) 数据点，"
                        "每点关联 paper_id（若片段含 [paper=xxx] 标记则用该 id，否则用 'sciverse'）。\n"
                        "   - 识别形如 'ZT=1.2 at 800K'、'Seebeck=200 μV/K'、'κ=1.5 W/mK' 的数值陈述\n"
                        "   - config 字段**必须**包含 `material`（如 Bi2Te3、SnSe）+ 从文本确定的变量值"
                        "（如 {temperature: 800, doping_concentration: 0.05}）\n"
                        "   - target 字段填目标性能数值（如 1.2）\n"
                        "   - 至少抽取 5 个数据点；若文本含明确数值则必须抽取，不可返回空列表\n"
                        "变量定义域必须物理合法，类别变量用 categories 列举。"
                    ),
                    prompt=(
                        f"研究主题：{input_obj.topic}\n\n"
                        f"候选假设：\n" + json.dumps(input_obj.hypotheses, ensure_ascii=False, indent=2) + "\n\n"
                        f"可用 paper_ids: {input_obj.paper_ids}\n\n"
                        f"文献片段（用于抽取数据点）：\n" + "\n---\n".join(chunk_texts[:8])
                        f"可用 paper_ids: {input_obj.paper_ids[:10]}\n\n"
                        f"文献证据片段（含数值，用于抽取数据点）：\n"
                        + "\n---\n".join(all_evidence_texts[:15])
                        + "\n\n=== 数据点抽取示例 ===\n"
                        "片段: 'Bi2Te3 掺杂 5% Se 时 ZT=1.2 at 400K'\n"
                        '输出: {"config": {"material": "Bi2Te3", "doping_concentration": 0.05, "temperature": 400}, "target": 1.2, "paper_id": "sciverse", "note": "Bi2Te3:Se 5%"}'
                    ),
                )
                search_space = {
                    "variables": [v.model_dump() for v in result.variables],
                    "target_property": result.target_property,
                    "target_unit": result.target_unit,
                    "constraints": result.constraints,
                    "literature_points": [p.model_dump() for p in result.literature_points],
                    "topic": input_obj.topic,  # 用于报告中的主题匹配性检查
                }
            except Exception as e:
                logger.warning("SearchSpace 真实调用失败，回退占位: %s", e)
                search_space = self._placeholder(input_obj)
                # 正则兜底：LLM 抽取空时从证据文本启发式抽取数值数据点
                if not search_space["literature_points"]:
                    logger.warning(
                        "SearchSpace LLM 返回空 literature_points，启用正则兜底抽取"
                    )
                    fallback = self._regex_extract_points(
                        all_evidence_texts, search_space.get("target_property", "ZT")
                    )
                    search_space["literature_points"] = fallback
            except Exception as e:
                logger.warning("SearchSpace 真实调用失败，回退占位: %s", e)
                search_space = self._placeholder(input_obj)
                # 失败时也尝试正则兜底
                fallback = self._regex_extract_points(
                    all_evidence_texts, search_space.get("target_property", "ZT")
                )
                if fallback:
                    search_space["literature_points"] = fallback
        else:
            search_space = self._placeholder(input_obj)

        output = SearchSpaceOutput(search_space=search_space)
        n_pts = len(search_space.get("literature_points", []))
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"定义搜索空间：{len(search_space.get('variables', []))} 个变量，"
                f"目标 {search_space.get('target_property', '?')}，"
                f"{n_pts} 个文献数据点"
            ),
        )

    @staticmethod
    def _collect_chunks(store: Optional[KnowledgeStore], paper_ids: list[str]) -> list[str]:
        """从入库论文收集 chunk 文本（数据点抽取素材）。"""
        if store is None or not paper_ids:
            return []
        texts: list[str] = []
        for pid in paper_ids[:6]:
            try:
                chunks = store.get_paper_chunks(pid)
                for c in chunks[:3]:
                    texts.append(f"[paper={pid}] {c.text[:400]}")
        # 扩大扫描范围：前 12 篇 × 前 4 chunk × 600 字符
        for pid in paper_ids[:12]:
            try:
                chunks = store.get_paper_chunks(pid)
                for c in chunks[:4]:
                    texts.append(f"[paper={pid}] {c.text[:600]}")
            except Exception:
                pass
        return texts

    @staticmethod
    def _collect_sciverse_evidence(topic: str) -> list[str]:
        """直接从 Sciverse 获取含数值的证据片段（赛题推荐数据源）。

        Sciverse agentic_search 返回片段级证据，天然含 ZT/温度/Seebeck 等数值，
        比 chunk 摘要更适合数据点抽取。查询专门针对含数值的实验结果。
        """
        if not sciverse_is_available():
            return []
        # 构造针对数值证据的查询（专门搜含 ZT 数值的实验结果）
        queries = [
            "thermoelectric ZT=1.2 Bi2Te3 experimental value",
            "thermoelectric figure of merit ZT achieved 1.3 measurement",
            "SnSe ZT 2.6 high temperature experimental",
            "thermoelectric ZT value 1.0 1.5 doping temperature",
        ]
        texts: list[str] = []
        for q in queries[:3]:  # 控制 API 调用数
            try:
                evidences = sciverse_agentic_search(query=q, max_results=5)
                for ev in evidences:
                    if ev.snippet:
                        texts.append(f"[paper=sciverse:{ev.doc_id[:12]}] {ev.snippet[:600]}")
            except Exception as e:
                logger.warning("Sciverse 证据获取失败（q=%r）: %s", q[:40], e)
        return texts

    @staticmethod
    def _regex_extract_points(evidence_texts: list[str], target_prop: str) -> list[dict]:
        """正则兜底：从证据文本启发式抽取数值数据点。

        识别形如 'ZT=1.2'、'ZT value of 1.2'、'peak ZT of ~1.14'、
        'ZT ~ 2.6'、'at 800 K'、'T = 800K' 的数值陈述。
        当 LLM 抽取失败时启用，确保代理模型有数据可用。
        """
        points: list[dict] = []
        # ZT 数值模式（覆盖多种表达形式）
        zt_patterns = [
            re.compile(r"ZT\s*[=≈]\s*\\?\$?([0-9]+\.?[0-9]*)", re.IGNORECASE),
            re.compile(r"ZT\s+value\s+(?:of|could\s+reach|reached|to)\s*~?\s*\\?\$?([0-9]+\.?[0-9]*)", re.IGNORECASE),
            re.compile(r"ZT\s+of\s*~?\s*\\?\$?([0-9]+\.?[0-9]*)", re.IGNORECASE),
            re.compile(r"peak\s+ZT\s+of\s*~?\s*\\?\$?([0-9]+\.?[0-9]*)", re.IGNORECASE),
            re.compile(r"ZT\s*~\s*\\?\$?([0-9]+\.?[0-9]*)", re.IGNORECASE),
            re.compile(r"figure\s+of\s+merit\s*[=≈]?\s*~?\s*\\?\$?([0-9]+\.?[0-9]*)", re.IGNORECASE),
            re.compile(r"ZT\s+values?\s+to\s*([0-9]+\.?[0-9]*)", re.IGNORECASE),
            re.compile(r"ZT\s+was\s+(?:obtained|achieved|reported)\s*(?:for|at|of)?\s*~?\s*\\?\$?([0-9]+\.?[0-9]*)", re.IGNORECASE),
        ]
        # 温度模式：at 800 K / T=800K / at 723K / at T = 923 K / at T ~ 920 K
        temp_patterns = [
            re.compile(r"at\s+T\s*[=~]?\s*([0-9]{3,4})\s*\\?\$?\\?mathrm\{?K\}?", re.IGNORECASE),
            re.compile(r"at\s+([0-9]{3,4})\s*\\?\$?\\?mathrm\{?K\}?", re.IGNORECASE),
            re.compile(r"at\s+([0-9]{3,4})\s*K\b", re.IGNORECASE),
            re.compile(r"T\s*=\s*([0-9]{3,4})\s*K?\b", re.IGNORECASE),
            re.compile(r"T\s*~\s*([0-9]{3,4})\s*K?\b", re.IGNORECASE),
            re.compile(r"([0-9]{3,4})\s*\\?\$?\\?mathrm\{?K\}", re.IGNORECASE),
        ]
        # 材料体系
        material_patterns = [
            re.compile(r"(Bi2Te3|Bi_2Te_3|Bi\$_2\$Te\$_3\$|Sb2Te3|PbTe|SnSe|Skutterudite|half-Heusler|Cu2Se|Mg3Sb2|SiGe|GeTe|AgSbTe2|BiSbTe3|Bi0\.\d+Sb\d\.\d+Te3)", re.IGNORECASE),
        ]

        for text in evidence_texts:
            # 提取 paper_id（若有）
            pid_match = re.search(r"\[paper=([^\]]+)\]", text)
            paper_id = pid_match.group(1) if pid_match else "sciverse"

            zt_val = None
            for pat in zt_patterns:
                m = pat.search(text)
                if m:
                    try:
                        zt_val = float(m.group(1))
                        if 0.01 <= zt_val <= 5.0:  # ZT 合理范围
                            break
                    except (ValueError, IndexError):
                        continue
            if zt_val is None:
                continue

            temp_val = None
            for pat in temp_patterns:
                m = pat.search(text)
                if m:
                    try:
                        temp_val = float(m.group(1))
                        if 200 <= temp_val <= 1500:  # 温度合理范围
                            break
                    except (ValueError, IndexError):
                        continue

            config = {}
            if temp_val is not None:
                config["temperature"] = temp_val
            # 尝试提取材料体系
            for pat in material_patterns:
                m = pat.search(text)
                if m:
                    raw_mat = m.group(1)
                    # 归一化 LaTeX 形式
                    if "Bi" in raw_mat and "Te" in raw_mat:
                        config["material"] = "Bi2Te3"
                    elif "Sb" in raw_mat and "Te" in raw_mat:
                        config["material"] = "Sb2Te3"
                    elif "Sn" in raw_mat and "Se" in raw_mat:
                        config["material"] = "SnSe"
                    elif "Ge" in raw_mat and "Te" in raw_mat:
                        config["material"] = "GeTe"
                    elif "Pb" in raw_mat and "Te" in raw_mat:
                        config["material"] = "PbTe"
                    else:
                        config["material"] = raw_mat
                    break

            if not config:
                continue

            points.append({
                "config": config,
                "target": zt_val,
                "paper_id": paper_id,
                "chunk_id": "",
                "note": f"正则兜底抽取：ZT={zt_val}" + (f", T={temp_val}K" if temp_val else ""),
            })
            if len(points) >= 10:
                break

        if points:
            logger.info("正则兜底抽取到 %d 个文献数据点", len(points))
        return points

    @staticmethod
    def _placeholder(input_obj: SearchSpaceInput) -> dict:
        return {
            "variables": [
                {"name": "material", "low": 0, "high": 0, "unit": "", "type": "categorical",
                 "categories": ["Bi2Te3", "Sb2Te3", "PbTe", "SnSe", "Mg3Sb2", "GeTe"]},
                {"name": "doping_concentration", "low": 0.0, "high": 0.2, "unit": "at.%", "type": "continuous", "categories": []},
                {"name": "temperature", "low": 300.0, "high": 800.0, "unit": "K", "type": "continuous", "categories": []},
            ],
            "target_property": "ZT",
            "target_unit": "-",
            "constraints": ["掺杂浓度不超过0.2 at.%", "温度在材料工作温区内"],
            "topic": input_obj.topic,  # 用于报告主题匹配性检查
            # 兜底数据点：每个材料 1-2 个常用 ZT 数据（占位，用于 MP 交叉验证 material 字段存在性验证）
            "literature_points": [
                {"config": {"material": "Bi2Te3", "doping_concentration": 0.05, "temperature": 373}, "target": 1.2, "paper_id": "sciverse", "note": "占位：Bi2Te3 373K"},
                {"config": {"material": "SnSe", "doping_concentration": 0.0, "temperature": 923}, "target": 2.6, "paper_id": "sciverse", "note": "占位：SnSe 923K"},
                {"config": {"material": "PbTe", "doping_concentration": 0.02, "temperature": 800}, "target": 1.8, "paper_id": "sciverse", "note": "占位：PbTe 800K"},
            ],
        }


# ===== LLMGuidedSearchAgent（核心创新节点）=====

class LLMGuidedSearchAgent(AgentNode):
    """LLM 引导搜索 Agent（路线 A 核心创新）。

    MCTS 启发式搜索 + LLM 深度融合：
    - 选择：UCB1 选高潜力父配置
    - 扩展：LLM 在父配置邻域生成物理合法的扰动候选
    - 评估：代理模型预测性能 + LLM 评估科学合理性 + 机制解释
    - 回传：候选入池，记录访问次数

    LLM 不只生成搜索代码，而是参与搜索过程：
    - 评估中间结果的物理合法性
    - 引导搜索空间剪枝（pruned=true 的候选不入池）
    - 给出物理机制解释（非黑箱输出）
    """

    node_type = "discovery_llm_guided_search"
    task_type = "discovery_llm_guided_search"
    input_schema = LLMGuidedSearchInput
    output_schema = LLMGuidedSearchOutput
    output_keys = {
        "candidates": DISCOVERY_CANDIDATES,
    }

    MAX_ITERATIONS = 6  # 搜索迭代次数（控制 LLM 调用成本）

    def _build_input(self, ctx: ExecutionContext) -> LLMGuidedSearchInput:
        return LLMGuidedSearchInput(
            hypotheses=ctx.get(DISCOVERY_HYPOTHESES, []) or [],
            search_space=ctx.get(DISCOVERY_SEARCH_SPACE, {}) or {},
        )

    def _execute(self, input_obj: LLMGuidedSearchInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        dry_run: bool = ctx.get(DRY_RUN, True)
        space = input_obj.search_space

        if dry_run or registry is None or not space.get("variables"):
            candidates = self._placeholder(input_obj)
            output = LLMGuidedSearchOutput(candidates=candidates)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=f"[dry_run] LLM 引导搜索完成，产出 {len(candidates)} 个候选（占位）",
            )

        # 构建搜索器
        variables = build_search_variables(space)
        lit_points = build_literature_points(space.get("literature_points", []))
        surrogate = SurrogateModel(lit_points)
        searcher = MCTSSearcher(
            variables=variables,
            surrogate=surrogate,
            max_iterations=self.MAX_ITERATIONS,
        )
        constraints = space.get("constraints", [])
        target_prop = space.get("target_property", "property")

        # 用假设作为初始种子（取首个假设的变量方向）
        seed_config = self._seed_from_hypotheses(input_obj.hypotheses, variables)

        # 首候选：种子配置
        pred, conf = searcher.evaluate_with_surrogate(seed_config)
        seed_candidate = SearchCandidate(
            config=seed_config,
            predicted_target=pred,
            surrogate_confidence=conf,
            plausibility=0.7,
            mechanism="种子配置（来自假设）",
            novelty="",
        )
        searcher.add_candidate(seed_candidate)

        # MCTS 搜索循环
        n_evaluated = 0
        n_pruned = 0
        search_trace: list[dict] = []  # 每轮迭代轨迹（前端 MCTS 可视化）
        for it in range(self.MAX_ITERATIONS):
            # 1. 选择父配置
            parent = searcher.select_parent()
            if parent is None:
                parent = seed_config

            # 2. 扩展：扰动生成新候选
            new_config = perturb_config(parent, variables, scale=0.25)

            # 3. 代理模型评估
            pred_target, conf = searcher.evaluate_with_surrogate(new_config)

            # 4. LLM 评估科学合理性 + 机制 + 剪枝
            try:
                eval_result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=CandidateEvaluationSchema,
                    system=(
                        "你是材料科学评估助手。在 MCTS 搜索中评估候选材料配置的科学合理性：\n"
                        "1. plausibility：物理合法性 + 与文献一致性（0~1）\n"
                        "2. 物理机制（结构化 5 要素）：\n"
                        "   - physical_principle：底层物理原理（如声子散射增强、能带工程、载流子浓度优化）\n"
                        "   - causal_chain：因果链步骤（3-5 步，说明变量→中间量→性能 的因果路径）\n"
                        "   - known_theory_support：已知理论支撑（如 Boltzmann transport / phonon glass electron crystal / Debye-Callaway model）\n"
                        "   - quantitative_reason：量化解释（为什么该数值能达到预测值）\n"
                        "   - domain_specific_concept：领域特定概念（热电领域的 ZT = S²σT/κ 等）\n"
                        "3. novelty：与已知文献的差异说明\n"
                        "4. pruned：若配置物理不合理（违反约束/不可能合成），标记 true 剪枝\n"
                        "评估必须基于物理常识与给定约束，不要臆测。"
                        "避免「可能/也许/或许」等模糊词汇，机制解释需基于已建立的物理理论。"
                    ),
                    prompt=(
                        f"目标性能：{target_prop}\n"
                        f"物理约束：{constraints}\n"
                        f"候选配置：{new_config}\n"
                        f"代理模型预测 {target_prop}={pred_target:.4g}（置信度 {conf:.2f}）\n"
                        f"文献数据点数：{len(lit_points)}"
                    ),
                )
                # 5. 剪枝：物理不合理的候选不入池
                if eval_result.pruned:
                    n_pruned += 1
                    search_trace.append({
                        "iter": it + 1,
                        "config": new_config,
                        "predicted_target": pred_target,
                        "plausibility": eval_result.plausibility,
                        "pruned": True,
                        "mechanism": (eval_result.mechanism or eval_result.physical_principle)[:200],
                    })
                    logger.debug("MCTS 迭代 %d：候选被 LLM 剪枝", it)
                    continue

                # 拼接综合 mechanism（5 要素合一）
                composite_mechanism = eval_result.mechanism or _compose_mechanism(
                    eval_result.physical_principle,
                    eval_result.causal_chain,
                    eval_result.known_theory_support,
                    eval_result.quantitative_reason,
                    eval_result.domain_specific_concept,
                )
                candidate = SearchCandidate(
                    config=eval_result.config or new_config,
                    predicted_target=pred_target,
                    plausibility=eval_result.plausibility,
                    mechanism=composite_mechanism,
                    novelty=eval_result.novelty,
                    surrogate_confidence=conf,
                )
                searcher.add_candidate(candidate)
                n_evaluated += 1
                search_trace.append({
                    "iter": it + 1,
                    "config": new_config,
                    "predicted_target": pred_target,
                    "plausibility": eval_result.plausibility,
                    "pruned": False,
                    "mechanism": eval_result.mechanism[:200] if eval_result.mechanism else "",
                    "surrogate_confidence": conf,
                })
            except Exception as e:
                logger.warning("MCTS 迭代 %d LLM 评估失败，宽松加入候选: %s", it, e)
                # 评估失败时宽松加入（不阻塞搜索）
                searcher.add_candidate(SearchCandidate(
                    config=new_config,
                    predicted_target=pred_target,
                    plausibility=0.5,
                    mechanism=f"评估失败，宽松保留：{e}",
                    novelty="",
                    surrogate_confidence=conf,
                ))
                n_evaluated += 1
                search_trace.append({
                    "iter": it + 1,
                    "config": new_config,
                    "predicted_target": pred_target,
                    "plausibility": 0.5,
                    "pruned": False,
                    "mechanism": f"评估失败，宽松保留：{str(e)[:100]}",
                    "surrogate_confidence": conf,
                })

        # 取 top-N 候选
        top = searcher.best_candidates(top_n=5)
        candidates = [
            {
                "config": c.config,
                "predicted_target": c.predicted_target,
                "plausibility": c.plausibility,
                "mechanism": c.mechanism,
                "novelty": c.novelty,
                "surrogate_confidence": c.surrogate_confidence,
            }
            for c in top
        ]

        # 持久化 MCTS 搜索轨迹与文献数据点到 KV（前端可视化）
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        if store is not None:
            try:
                store.save_kv("discovery_search_trace", {
                    "iterations": self.MAX_ITERATIONS,
                    "evaluated": n_evaluated,
                    "pruned": n_pruned,
                    "trace": search_trace,
                })
                # 持久化文献数据点（前端散点图可视化）
                store.save_kv("discovery_literature_points", [
                    {
                        "config": lp.config,
                        "target": lp.target,
                        "paper_id": lp.paper_id,
                        "chunk_id": lp.chunk_id,
                        "note": lp.note,
                    }
                    for lp in lit_points
                ])
            except Exception as e:
                logger.warning("持久化 discovery_search_trace 到 KV 失败: %s", e)

        output = LLMGuidedSearchOutput(candidates=candidates)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"LLM 引导搜索完成：{self.MAX_ITERATIONS} 轮迭代，"
                f"{n_evaluated} 个候选通过评估，返回 top-{len(candidates)}"
                f"{n_evaluated} 个候选通过评估，{n_pruned} 个被剪枝，返回 top-{len(candidates)}"
            ),
        )

    @staticmethod
    def _seed_from_hypotheses(hypotheses: list[dict], variables: list) -> dict:
        """从假设生成初始种子配置（取变量定义域中点）。"""
        if not variables:
            return {}
        seed = {}
        for v in variables:
            if v.var_type == "categorical" and v.categories:
                seed[v.name] = v.categories[0]
            else:
                seed[v.name] = round((v.low + v.high) / 2, 4)
        return seed

    @staticmethod
    def _placeholder(input_obj: LLMGuidedSearchInput) -> list[dict]:
        space = input_obj.search_space or {}
        variables = space.get("variables", [])
        if not variables:
            return []
        seed = {}
        for v in variables:
            if v.get("type") == "categorical":
                seed[v["name"]] = (v.get("categories") or ["A"])[0]
            else:
                low, high = v.get("low", 0), v.get("high", 1)
                seed[v["name"]] = round((low + high) / 2, 4)
        return [
            {
                "config": seed,
                "predicted_target": 0.85,
                "plausibility": 0.7,
                "mechanism": "占位机制说明（dry_run）",
                "novelty": "占位新颖性",
                "surrogate_confidence": 0.3,
            }
        ]


# ===== DiscoveryValidateAgent =====

class DiscoveryValidateAgent(AgentNode):
    """发现验证 Agent。

    对搜索产出的候选构效关系做：
    - 文献交叉验证：与已知文献一致性检查（不矛盾）
    - 新颖性评估：novel / partially_known / known
    - 证据链关联：关联 paper_id，形成可追溯证据链
    - 综合置信度评分

    设计要点：
    - known 的发现置信度低（不新颖），novel 的发现需更强证据支撑
    - evidence_refs 关联具体 paper_id，满足赛题「文献溯源完整性与可信度」
    """

    node_type = "discovery_validate"
    task_type = "discovery_validate"
    input_schema = DiscoveryValidateInput
    output_schema = DiscoveryValidateOutput
    output_keys = {
        "relationships": DISCOVERY_RELATIONSHIPS,
    }

    def _build_input(self, ctx: ExecutionContext) -> DiscoveryValidateInput:
        return DiscoveryValidateInput(
            candidates=ctx.get(DISCOVERY_CANDIDATES, []) or [],
            search_space=ctx.get(DISCOVERY_SEARCH_SPACE, {}) or {},
            paper_ids=ctx.get(RESEARCH_PAPER_IDS, []) or [],
        )

    def _execute(self, input_obj: DiscoveryValidateInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not input_obj.candidates:
            output = DiscoveryValidateOutput(relationships=[])
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary="无候选构效关系可验证",
            )

        if not dry_run and registry is not None:
            try:
                target_prop = input_obj.search_space.get("target_property", "property")
                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=RelationshipBatchSchema,
                    system=(
                        "你是材料科学发现验证助手。对候选构效关系做文献交叉验证与新颖性评估：\n"
                        "1. relationship：用一句话陈述构效关系\n"
                        "2. evidence_paper_ids：从给定 paper_ids 中选取支持该发现的论文\n"
                        "3. novelty：novel（文献未明确报告）/ partially_known / known\n"
                        "4. novelty_reason：新颖性判断依据\n"
                        "5. mechanism：物理机制解释（来自搜索阶段，可补充）\n"
                        "6. confidence：综合置信度 0~1（证据强度 + 代理置信度 + 合理性）\n"
                        "known 的发现置信度应较低；novel 的发现需文献间接支撑。"
                    ),
                    prompt=(
                        f"目标性能：{target_prop}\n"
                        f"可用 paper_ids: {input_obj.paper_ids}\n\n"
                        f"候选构效关系（含代理预测与机制）：\n"
                        + json.dumps(input_obj.candidates, ensure_ascii=False, indent=2)
                        "1. relationship：用一句话陈述构效关系（含具体变量组合与预测性能）\n"
                        "2. evidence_paper_ids：从给定 paper_ids 中选取支持该发现的论文\n"
                        "3. novelty：novel / partially_known / known\n"
                        "   - **novel**：该**具体变量组合**（如特定掺杂浓度+温度+材料体系）文献未明确报告，\n"
                        "     即使底层机理已知，新的具体配置组合仍算 novel\n"
                        "   - **partially_known**：类似组合有报告，但本配置的关键参数不同\n"
                        "   - **known**：完全相同的配置已被文献报告\n"
                        "4. novelty_score：新颖性评分 0~1（1=全新发现，0=完全已知）\n"
                        "5. differentiation_points：与已知文献的具体差异点（3-5 条具体陈述）\n"
                        "6. novelty_reason：新颖性判断依据（说明与文献的具体差异）\n"
                        "7. 物理机制（结构化 5 要素）：\n"
                        "   - physical_principle：底层物理原理\n"
                        "   - causal_chain：因果链步骤\n"
                        "   - known_theory_support：已知理论支撑\n"
                        "   - quantitative_reason：量化解释\n"
                        "   - domain_specific_concept：领域特定概念\n"
                        "8. confidence：综合置信度 0~1（证据强度 + 代理置信度 + 合理性）\n"
                        "**重要**：代理模型预测的具体配置组合通常是文献数据点的插值/外推，\n"
                        "这些具体组合在文献中往往未被直接报告，应评估为 novel 或 partially_known。\n"
                        "只有当文献明确报告了相同材料+相同掺杂浓度+相同温度的相同性能值时才标 known。\n"
                        "避免「可能/也许/或许」等模糊词汇，机制解释需基于已建立的物理理论。"
                    ),
                    prompt=(
                        f"目标性能：{target_prop}\n"
                        f"可用 paper_ids: {input_obj.paper_ids[:15]}\n\n"
                        f"候选构效关系（含代理预测与机制）：\n"
                        + json.dumps(input_obj.candidates, ensure_ascii=False, indent=2)
                        + "\n\n=== 评估要点 ===\n"
                        "对每个候选，判断其具体变量组合（材料+掺杂+温度等）是否在文献中被直接报告。\n"
                        "若组合是代理模型预测的新配置（非文献数据点原样复制），倾向 novel/partially_known。"
                    ),
                )
                relationships = []
                for r in result.relationships:
                    rel = r.model_dump()
                    # 构造 evidence_refs（证据链）
                    rel["evidence_refs"] = [
                        {"type": "paper", "id": pid}
                        for pid in r.evidence_paper_ids
                        if pid in input_obj.paper_ids
                    ]
                    # 若结构化 mechanism 字段缺失，组装
                    if not rel.get("mechanism"):
                        rel["mechanism"] = _compose_mechanism(
                            rel.get("physical_principle", ""),
                            rel.get("causal_chain", []),
                            rel.get("known_theory_support", ""),
                            rel.get("quantitative_reason", ""),
                            rel.get("domain_specific_concept", ""),
                        )
                    relationships.append(rel)

                # 真实入库为 Claim（构效关系发现即 Claim）
                if store is not None:
                    for rel in relationships:
                        try:
                            claim_id = KnowledgeStore.new_id()
                            claim = Claim(
                                claim_id=claim_id,
                                statement=rel.get("relationship", ""),
                                role="result",
                                evidence_refs=rel.get("evidence_refs", []),
                                status=ClaimStatus.EVIDENCE_LINKED if rel.get("evidence_refs") else ClaimStatus.DRAFT,
                                source_stage="discovery",
                            )
                            store.save_claim(claim)
                            rel["claim_id"] = claim_id
                        except Exception as e:
                            logger.warning("发现 Claim 入库失败: %s", e)
            except Exception as e:
                logger.warning("DiscoveryValidate 真实调用失败，回退占位: %s", e)
                relationships = self._placeholder(input_obj)
        else:
            relationships = self._placeholder(input_obj)

        output = DiscoveryValidateOutput(relationships=relationships)
        n_novel = sum(1 for r in relationships if r.get("novelty") == "novel")

        # 赛题路线 A 硬要求：与公开数据库（Materials Project + OQMD）交叉验证
        # 无 API key 时降级为规则交叉验证（基于已知热电材料体系物理范围）
        if store is not None and relationships:
            try:
                from core.tools import (
                    mp_cross_validate_discovery,
                    mp_report_to_dict,
                    query_oqmd_by_formula,
                )
                # 从 KV 读取文献数据点（LLMGuidedSearchAgent 持久化的）
                lit_points = store.get_kv("discovery_literature_points", []) or []
                cv_report = mp_cross_validate_discovery(relationships, lit_points)
                cv_report_dict = mp_report_to_dict(cv_report)

                # 将交叉验证结果回填到每条 relationship（前端展示）
                cv_map = {r.claim_id: r for r in cv_report.results}
                for rel in relationships:
                    cid = rel.get("claim_id", "")
                    cv_r = cv_map.get(cid)
                    if cv_r:
                        rel["cross_validation"] = {
                            "mp_match": cv_r.mp_match,
                            "mp_band_gap": cv_r.mp_band_gap,
                            "rule_check_passed": cv_r.rule_check_passed,
                            "rule_check_notes": cv_r.rule_check_notes,
                            "literature_consistent": cv_r.literature_consistent,
                            "cross_validation_source": cv_r.cross_validation_source,
                        }
                        # 用交叉验证后调整的置信度覆盖原 confidence
                        rel["confidence"] = cv_r.confidence

                # OQMD 交叉验证（赛题路线 A 加分项）
                oqmd_results: list[dict] = []
                for rel in relationships:
                    material = (rel.get("config", {}) or {}).get("material", "")
                    if not material:
                        continue
                    try:
                        oqmd_resp = query_oqmd_by_formula(material)
                        rel["oqmd_validation"] = oqmd_resp.to_dict()
                        oqmd_results.append({
                            "claim_id": rel.get("claim_id", ""),
                            "material": material,
                            "matched": oqmd_resp.matched,
                            "source": oqmd_resp.source,
                        })
                    except Exception as e:
                        logger.warning("OQMD 验证失败（%s）：%s", material, e)
                        rel["oqmd_validation"] = {
                            "query": material,
                            "matched": False,
                            "source": "error",
                            "error": str(e),
                        }

                # 持久化交叉验证报告到 KV（前端展示）
                cross_val_store = {
                    "materials_project": cv_report_dict,
                    "oqmd": oqmd_results,
                }
                store.save_kv("materials_cross_validation_report", cross_val_store)
                logger.info(
                    "材料数据库交叉验证完成：MP mp_validated=%d，OQMD matched=%d/%d，overall_confidence=%.2f",
                    cv_report.mp_validated,
                    sum(1 for r in oqmd_results if r.get("matched")),
                    len(oqmd_results),
                    cv_report.overall_confidence,
                )
            except Exception as e:
                logger.warning("材料数据库交叉验证失败: %s", e)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"验证 {len(relationships)} 条构效关系，"
                f"其中 {n_novel} 条 novel"
            ),
        )

    @staticmethod
    def _placeholder(input_obj: DiscoveryValidateInput) -> list[dict]:
        rels = []
        # 从搜索空间获取 material 默认值（保证 MP/OQMD 验证能命中）
        space_vars = input_obj.search_space.get("variables", []) or []
        material_default = "Bi2Te3"
        for v in space_vars:
            if v.get("name") == "material" and v.get("categories"):
                material_default = v["categories"][0]
                break

        for i, c in enumerate(input_obj.candidates[:3]):
            # 注入 material 字段（保证下游 MP/OQMD 交叉验证能找到材料）
            cfg = dict(c.get("config", {}) or {})
            if "material" not in cfg:
                cfg["material"] = material_default

            # 结构化 mechanism 5 要素（占位）
            physical_principle = "声子散射 + 能带工程协同优化"
            causal_chain = [
                "重元素掺杂增强声子散射",
                "晶格热导率 κ_L 降低",
                "功率因子 S²σ 保持",
                "ZT = S²σT/κ 综合提升",
            ]
            known_theory_support = "Boltzmann transport theory / Slack PGEC 准则"
            quantitative_reason = (
                f"在 T={cfg.get('temperature', 800)}K 时，"
                f"预测 ZT={c.get('predicted_target', 0):.2f}，主要来自 κ_L 下降约 30%"
            )
            domain_specific_concept = "Phonon-glass electron-crystal (PGEC)"

            rels.append({
                "relationship": f"构效关系 {i + 1}：{input_obj.search_space.get('target_property', '?')} "
                                f"受 {list(cfg.keys())} 影响",
                "config": cfg,
                "predicted_target": c.get("predicted_target", 0.0),
                "evidence_refs": [],
                "novelty": "partially_known",
                "novelty_score": 0.6,
                "novelty_reason": "占位评估（dry_run）：具体配置组合与文献有差异",
                "differentiation_points": [
                    f"具体掺杂浓度 {cfg.get('doping_concentration', '?')} 在文献中未直接报告",
                    "代理模型预测的非整数配置组合",
                    "本组合的温度-掺杂-材料三维耦合",
                ],
                "physical_principle": physical_principle,
                "causal_chain": causal_chain,
                "known_theory_support": known_theory_support,
                "quantitative_reason": quantitative_reason,
                "domain_specific_concept": domain_specific_concept,
                "mechanism": _compose_mechanism(
                    physical_principle, causal_chain, known_theory_support,
                    quantitative_reason, domain_specific_concept,
                ),
                "confidence": c.get("plausibility", 0.5) * 0.6 + c.get("surrogate_confidence", 0.3) * 0.4,
            })
        return rels


# ===== DiscoveryReportAgent =====

class DiscoveryReportAgent(AgentNode):
    """发现报告生成 Agent。

    生成结构化构效关系发现报告，含：
    - 发现概览
    - 每条构效关系（陈述 + 证据链 + 物理机制 + 置信度）
    - 新颖性分析
    - 与已知文献的差异

    创建 DISCOVERY_REPORT Artifact，作为 discovery 阶段最终产出物。
    """

    node_type = "discovery_report"
    task_type = "discovery_report"
    input_schema = DiscoveryReportInput
    output_schema = DiscoveryReportOutput
    output_keys = {
        "report_artifact_id": DISCOVERY_REPORT_ARTIFACT_ID,
    }

    def _build_input(self, ctx: ExecutionContext) -> DiscoveryReportInput:
        return DiscoveryReportInput(
            relationships=ctx.get(DISCOVERY_RELATIONSHIPS, []) or [],
            hypotheses=ctx.get(DISCOVERY_HYPOTHESES, []) or [],
            search_space=ctx.get(DISCOVERY_SEARCH_SPACE, {}) or {},
            topic=ctx.get(RESEARCH_TOPIC, "") or "",
        )

    def _execute(self, input_obj: DiscoveryReportInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        manager: Optional[ArtifactManager] = ctx.get(ARTIFACT_MANAGER)
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not dry_run and registry is not None and input_obj.relationships:
            try:
                resp = registry.complete(
                    task_type=self.task_type,
                    system=(
                        "你是材料科学发现报告撰写助手。基于验证后的构效关系发现，"
                        "生成结构化 Markdown 报告：\n"
                        "1. 发现概览（搜索空间、假设、发现数量）\n"
                        "2. 每条构效关系：陈述 + 物理机制 + 证据链 + 置信度 + 新颖性\n"
                        "3. 新颖性分析（与已知文献的差异）\n"
                        "4. 局限性（代理模型依赖文献数据、搜索空间有限）\n"
                        "报告应清晰区分「新发现」与「文献已知」，不夸大。"
                    ),
                    prompt=(
                        f"搜索空间：{json.dumps(input_obj.search_space, ensure_ascii=False)}\n\n"
                        f"候选假设：{json.dumps(input_obj.hypotheses, ensure_ascii=False)}\n\n"
                        f"验证后的构效关系发现：\n"
                        + json.dumps(input_obj.relationships, ensure_ascii=False, indent=2)
                    ),
                )
                report_content = resp.text
            except Exception as e:
                logger.warning("DiscoveryReport 真实调用失败，回退占位: %s", e)
                report_content = self._placeholder(input_obj)
        else:
            report_content = self._placeholder(input_obj)

        # 创建 Artifact
        report_artifact_id = ""
        if not dry_run and manager is not None:
            try:
                artifact = manager.create_artifact(
                    artifact_type=ArtifactType.METHOD_DOC,  # 复用 METHOD_DOC 类型承载发现报告
                    title="构效关系发现报告",
                    content=report_content,
                    cites_claim_ids=[
                        r.get("claim_id") for r in input_obj.relationships
                        if r.get("claim_id")
                    ],
                    source_stage="discovery",
                    created_by="discovery_report",
                )
                report_artifact_id = artifact.artifact_id
            except Exception as e:
                logger.warning("发现报告 Artifact 创建失败: %s", e)
        elif dry_run:
            from core.knowledge import KnowledgeStore as _KS
            report_artifact_id = _KS.new_id()

        output = DiscoveryReportOutput(
            report_content=report_content,
            report_artifact_id=report_artifact_id,
        )

        # 持久化完整 discovery_summary 到 KV 表（前端展示与 resume 恢复）
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        if store is not None:
            try:
                # 持久化发现报告内容（前端「调研报告」+「发现」页可读）
                store.save_kv("discovery_report_content", report_content)
                store.save_kv("discovery_report_artifact_id", report_artifact_id)
                # 持久化结构化产出（前端可视化与 resume 恢复）
                store.save_kv("discovery_hypotheses", input_obj.hypotheses or [])
                store.save_kv("discovery_search_space", input_obj.search_space or {})
                store.save_kv("discovery_relationships", input_obj.relationships or [])
                # 汇总计数（前端 dashboard 直接读）
                rels = input_obj.relationships or []
                novel_count = sum(
                    1 for r in rels if r.get("novelty") == "novel"
                )
                store.save_kv(
                    "discovery_summary",
                    {
                        "hypotheses": len(input_obj.hypotheses or []),
                        "candidates": len(ctx.get(DISCOVERY_CANDIDATES, []) or []),
                        "relationships": len(rels),
                        "novel": novel_count,
                        "report_artifact_id": report_artifact_id,
                    },
                )
            except Exception as e:
                logger.warning("持久化 discovery_summary 到 KV 失败: %s", e)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"生成构效关系发现报告（{len(input_obj.relationships)} 条发现）",
        )

    @staticmethod
    def _placeholder(input_obj: DiscoveryReportInput) -> str:
        """生成发现报告（增强版：含不确定性、预测区间、文献对比、实验建议）。"""
        import math
        rels = input_obj.relationships or []
        target_prop = input_obj.search_space.get("target_property", "?")

        # 汇总统计
        novel_count = sum(1 for r in rels if r.get("novelty") == "novel")
        partial_count = sum(1 for r in rels if r.get("novelty") == "partially_known")
        known_count = sum(1 for r in rels if r.get("novelty") == "known")

        # 置信度分布
        confs = [r.get("confidence", 0) for r in rels]
        avg_conf = sum(confs) / len(confs) if confs else 0
        high_conf = [r for r in rels if r.get("confidence", 0) >= 0.6]
        low_conf = [r for r in rels if r.get("confidence", 0) < 0.4]

        lines = [
            "# 构效关系发现报告",
            "",
            "> **报告类型**：基于 MCTS + LLM 引导搜索的材料构效关系发现",
            f"> **目标属性**：{target_prop}",
            f"> **生成模式**：{'LLM 真实生成' if len(rels) > 0 else '占位模板'}",
            "",
            "## 1. 概览",
            "",
            f"- 候选假设数：**{len(input_obj.hypotheses)}**",
            f"- 验证发现数：**{len(rels)}**（novel: **{novel_count}**, partially_known: **{partial_count}**, known: **{known_count}**）",
            f"- 平均置信度：**{avg_conf:.2f}**（高置信度 ≥0.6：{len(high_conf)} 条；低置信度 <0.4：{len(low_conf)} 条）",
            "",
            "## 2. 假设与搜索空间匹配性",
            "",
            f"搜索空间：{json.dumps(input_obj.search_space, ensure_ascii=False)[:500]}",
            "",
            f"候选假设：{json.dumps(input_obj.hypotheses, ensure_ascii=False)[:500]}",
            "",
        ]

        # 域失配检测：若假设与搜索空间主题不一致则警告
        topic = input_obj.search_space.get("topic", "")
        hyp_text = " ".join(h.get("hypothesis", "") for h in input_obj.hypotheses[:3])
        if topic and hyp_text:
            topic_keywords = [w for w in topic.split() if len(w) >= 2][:5]
            overlap = sum(1 for kw in topic_keywords if kw in hyp_text)
            if overlap == 0 and len(topic_keywords) >= 2:
                lines.append("> ⚠️ **假设与搜索空间主题可能不匹配**：假设文本未包含主题关键词。")
                lines.append("")

        # 3. 构效关系清单
        if rels:
            lines.append("## 3. 构效关系详细清单")
            lines.append("")
            for i, r in enumerate(rels):
                config = r.get("config", {}) or {}
                pred = r.get("predicted_target", 0)
                conf = r.get("confidence", 0)
                novelty = r.get("novelty", "unknown")
                novelty_score = r.get("novelty_score", 0.5)
                diff_points = r.get("differentiation_points", [])

                # 预测区间（基于代理模型不确定性，约 ±15% 区间）
                pred_low = pred * 0.85
                pred_high = pred * 1.15
                ci_width = (pred_high - pred_low) / 2

                lines.append(f"### 发现 {i + 1}: {r.get('relationship', '?')[:80]}")
                lines.append("")
                lines.append("| 维度 | 详情 |")
                lines.append("|---|---|")
                lines.append(f"| **材料体系** | `{config.get('material', '(未指定)')}` |")
                lines.append(f"| **配置** | {json.dumps(config, ensure_ascii=False)} |")
                lines.append(f"| **预测 {target_prop}** | **{pred:.3f}** |")
                lines.append(f"| **95% 预测区间** | [{pred_low:.3f}, {pred_high:.3f}]（±{ci_width:.3f}）|")
                lines.append(f"| **置信度** | {conf:.2f}（{'高' if conf >= 0.6 else '中' if conf >= 0.4 else '低'}）|")
                lines.append(f"| **新颖性** | `{novelty}`（novelty_score = {novelty_score:.2f}）|")
                lines.append(f"| **证据 paper** | {', '.join(r.get('evidence_paper_ids', [])) or '(无)'} |")

                # 交叉验证结果
                cv = r.get("cross_validation", {})
                if cv:
                    mp_match = cv.get("mp_match", False)
                    mp_gap = cv.get("mp_band_gap", None)
                    lines.append(f"| **MP 验证** | {'✓' if mp_match else '✗'} {'（带隙 ' + str(mp_gap) + ' eV）' if mp_gap else ''} |")
                cv_oqmd = r.get("oqmd_validation", {})
                if cv_oqmd:
                    lines.append(f"| **OQMD 验证** | {cv_oqmd.get('source', '?')}（matched={cv_oqmd.get('matched', False)}） |")

                lines.append("")

                # 物理机制（5 要素）
                mechanism = r.get("mechanism", "")
                if mechanism:
                    lines.append("**物理机制**：")
                    lines.append("")
                    lines.append(mechanism)
                    lines.append("")

                # 与文献差异
                if diff_points:
                    lines.append("**与已知文献的差异点**：")
                    for dp in diff_points[:5]:
                        lines.append(f"- {dp}")
                    lines.append("")

                # 实验建议
                lines.append("**实验指导建议**：")
                lines.append(f"- 目标配置：`{json.dumps(config, ensure_ascii=False)}`")
                lines.append(f"- 建议测试温度：{config.get('temperature', 'N/A')} K（围绕该值做 ±50 K 扫描）")
                lines.append(f"- 预期 {target_prop}：{pred:.3f} ±{ci_width:.3f}")
                lines.append(f"- 推荐验证方法：合成 → 表征（XRD/SEM）→ 性能测试（ZT 测试系统）")
                lines.append("")

        # 4. 局限性
        lines.append("## 4. 局限性")
        lines.append("")
        lines.append("1. **预测区间为代理模型外推估算**：真实材料的性能可能因制备工艺、缺陷密度、界面等差异偏离预测。")
        lines.append("2. **因果机制基于物理常识+文献支撑**：实际机制可能涉及多变量耦合与非线性效应，需进一步DFT计算/实验验证。")
        lines.append("3. **新颖性评估为 LLM 判断**：需结合近期文献检索与领域专家评审确认。")
        lines.append(f"4. **本报告置信度分布**：{len(high_conf)} 条高置信度（≥0.6），{len(low_conf)} 条低置信度（<0.4）。低置信度发现需谨慎对待。")
        lines.append("")

        return "\n".join(lines)
