"""research 阶段 Agent / Human 节点实现。

节点拓扑（借鉴 PaperQA + GPT-Researcher）：
    TopicRefineAgent
    → SubqueryDecomposeAgent（GPT-Researcher：子问题分解）
    → StageCheckpoint
    → TopicConfirmHuman
    → PaperFetchAgent（按子问题并行检索 arxiv/S2）
    → PaperRelevanceFilterAgent（PaperQA filter：相关性打分+筛选）
    → PaperIngestAgent（chunk 摘要 + 向量入库）
    → CrossValidateAgent（GPT-Researcher：多源交叉验证）

执行模式：
- dry_run=True  ：用占位数据返回，不调用 LLM、不访问外部 API（默认，验证架构用）
- dry_run=False ：真实调用 MiniMax M3，真实检索 arxiv/S2，真实入库 KnowledgeStore
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from pydantic import BaseModel, Field

from core.knowledge import (
    KnowledgeStore,
    Material,
    MaterialProperty,
    MaterialSynthesis,
    Paper,
    PaperChunk,
    ResearchGap,
    ResearchConflict,
)
from core.knowledge import KnowledgeStore, Paper, PaperChunk
from core.llm import LLMRegistry
from core.orchestration.context import ExecutionContext
from core.orchestration.node import (
    AgentNode,
    HumanNode,
    HumanRequest,
    HumanResponse,
    NodeInput,
    NodeOutput,
    NodeResult,
    NodeStatus,
)
from core.tools import (
    sciverse_agentic_search,
    sciverse_is_available,
    search_arxiv,
    search_semantic_scholar,
    split_into_chunks,
)
from core.tools.journal_quality import enrich_paper_quality, build_pdf_url
from core.tools.url_resolve import resolve_paper_url

from stages.common import (
    DRY_RUN,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    RESEARCH_CROSS_VALIDATION_REPORT,
    RESEARCH_EVIDENCE_CHAIN,
    RESEARCH_FILTERED_PAPER_METAS,
    RESEARCH_GAP_REPORT,
    RESEARCH_KEYWORDS,
    RESEARCH_MATERIAL_KNOWLEDGE,
    RESEARCH_PAPER_IDS,
    RESEARCH_PAPER_METAS,
    RESEARCH_QUERY_STRATEGY,
    RESEARCH_SEARCH_PREFS,
    RESEARCH_SUBQUERIES,
    RESEARCH_TOPIC,
    RESEARCH_TOPIC_CONFIRMED,
)
from stages.research.io_schema import (
    CrossValidateInput,
    CrossValidateOutput,
    MaterialExtractionInput,
    MaterialExtractionOutput,
    PaperFetchInput,
    PaperFetchOutput,
    PaperIngestInput,
    PaperIngestOutput,
    PaperRelevanceFilterInput,
    PaperRelevanceFilterOutput,
    ResearchGapInput,
    ResearchGapOutput,
    SubqueryDecomposeInput,
    SubqueryDecomposeOutput,
    TopicConfirmOutput,
    TopicRefineInput,
    TopicRefineOutput,
)

logger = logging.getLogger(__name__)


# ===== 结构化输出 Schema（供 structured_output 使用）=====

class TopicRefineSchema(BaseModel):
    """主题精炼输出 schema。"""

    keywords: list[str] = Field(description="5-10 个检索关键词，覆盖主题不同侧面")
    query_strategy: str = Field(description="arxiv/S2 的查询策略说明")


class SubquerySchema(BaseModel):
    """子问题分解输出 schema。"""

    subqueries: list[str] = Field(description="5-10 个互相正交的子问题")
    intents: list[str] = Field(description="每个子问题的检索意图：arxiv/s2/web")


class PaperMetaSchema(BaseModel):
    """论文元数据抽取 schema。"""

    title: str
    authors: list[str] = []
    year: Optional[int] = None
    abstract: str = ""
    arxiv_id: Optional[str] = None
    venue: str = ""
    relevance_score: float = Field(default=0.0, description="对该主题的相关性 0~1")
    relevance_reason: str = ""
    covered_subqueries: list[str] = []


class RelevanceScoreSchema(BaseModel):
    """相关性打分 schema（PaperQA filter，单篇）。"""
    """相关性打分 schema（PaperQA filter）。"""

    score: float = Field(description="相关性分数 0~1")
    reason: str = Field(description="打分理由")
    covered_subqueries: list[str] = Field(default_factory=list, description="覆盖的子问题")


class BatchScoreItem(BaseModel):
    """批量打分中的单篇条目（index 对应输入列表下标）。"""

    index: int = Field(description="候选论文在输入列表中的下标（从 0 开始）")
    score: float = Field(description="相关性分数 0~1")
    reason: str = Field(default="", description="打分理由")
    covered_subqueries: list[str] = Field(default_factory=list, description="覆盖的子问题")


class BatchScoreSchema(BaseModel):
    """批量相关性打分 schema（一次 LLM 调用为多篇论文打分，减少调用次数）。

    真实模式下 LLM 调用是 research 阶段的主要耗时（100+ 候选 × 单篇调用 ≈ 15-25 分钟）。
    批量打分将调用次数从 N 次降到 N/6 次，是论文浏览提速的关键优化。
    """

    items: list[BatchScoreItem] = Field(description="对输入列表中每篇论文的打分结果")


class ConflictItem(BaseModel):
    """冲突项。"""

    claim: str
    sources: list[dict] = []
    resolution: str = ""
    confidence: float = 0.0
    # 关联的来源论文 ID（证据链，赛题硬要求）
    source_paper_ids: list[str] = []


class ConsensusItem(BaseModel):
    """共识项（结构化版本，便于前端展示证据链）。"""

    statement: str
    # 关联的来源论文 ID（一致认同的论文集合）
    source_paper_ids: list[str] = []
    confidence: float = 0.0


# ===== 结构化 Research Gap Schema（赛题核心：准确性 + 新颖性 + 可操作性 + 证据链）=====

GAP_TYPE_VALUES = (
    "underexplored",       # 方向存在但尚未充分探索
    "contradiction",       # 多源结论冲突
    "missing_connection",  # 跨子领域连接缺失
    "method_gap",          # 方法层面缺失
    "data_gap",            # 数据/实验数据缺失
)

ACTIONABILITY_VALUES = ("high", "medium", "low")


class ResearchGapItem(BaseModel):
    """结构化 Research Gap。

    满足赛题「Research Gap 识别质量」要求：
    - 准确性：gap 文本 + 类型（underexplored/contradiction/missing_connection/method_gap/data_gap）
    - 新颖性：importance（0~1）
    - 可操作性：actionability（high/medium/low）
    - 文献溯源完整性：cited_paper_ids + cited_chunk_ids（每条 Gap 有清晰证据链）
    """

    gap: str = Field(description="Gap 陈述，聚焦材料领域的具体空白点，而非泛泛而谈")
    type: str = Field(description="Gap 类型，限定为：underexplored / contradiction / missing_connection / method_gap / data_gap")
    importance: float = Field(description="重要性 0~1，越接近 1 越关键", ge=0.0, le=1.0)
    actionability: str = Field(description="可操作性 high / medium / low", default="medium")
    cited_paper_ids: list[str] = Field(
        default_factory=list,
        description="关联的 paper_id 列表（证据链，赛题硬要求）",
    )
    cited_chunk_ids: list[str] = Field(
        default_factory=list,
        description="关联的 chunk_id（精细到 chunk 级别）",
    )
    rationale: str = Field(default="", description="为什么这是 Gap、为什么重要")


class ResearchGapBatchSchema(BaseModel):
    """Research Gap 批量输出 schema，含 3 条以上不同类型的 Gap。"""

    gaps: list[ResearchGapItem] = Field(
        description="结构化 Research Gap 列表，至少 3 条且覆盖不同类型"
    )


class ConflictReportSchema(BaseModel):
    """交叉验证报告 schema（含结构化 Gap / Conflict / Consensus）。

    相比旧版『gaps: list[str]』，新 schema 让每条 Gap 带：
    - 结构化类型/重要性/可操作性
    - 论文 + chunk 级证据链（赛题硬要求）
    """

    conflicts: list[ConflictItem] = []
    consensus: list[ConsensusItem] = []
    gaps: list[ResearchGapItem] = []
    overall_confidence: float = 0.0


# ===== TopicRefineAgent =====

class TopicRefineAgent(AgentNode):
    """主题精炼 Agent。

    接收用户主题，调用 literature_search task 生成检索关键词与查询策略。
    """

    node_type = "research_topic_refine"
    task_type = "literature_search"
    input_schema = TopicRefineInput
    output_schema = TopicRefineOutput
    output_keys = {
        "keywords": RESEARCH_KEYWORDS,
        "query_strategy": RESEARCH_QUERY_STRATEGY,
    }

    def _build_input(self, ctx: ExecutionContext) -> TopicRefineInput:
        topic = ctx.get(RESEARCH_TOPIC, "")
        return TopicRefineInput(topic=topic)

    def _execute(self, input_obj: TopicRefineInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not dry_run and registry is not None:
            try:
                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=TopicRefineSchema,
                    system=(
                        "你是科研文献检索专家。根据研究主题生成 5-10 个高质量检索关键词，"
                        "覆盖：核心概念、相关方法、应用场景、评估指标、关键局限。"
                        "并给出 arxiv 与 Semantic Scholar 的查询策略说明。"
                    ),
                    prompt=f"研究主题：{input_obj.topic}",
                )
                keywords = result.keywords
                query_strategy = result.query_strategy
            except Exception as e:
                logger.warning("TopicRefine 真实调用失败，回退占位: %s", e)
                keywords, query_strategy = self._placeholder(input_obj)
        else:
            keywords, query_strategy = self._placeholder(input_obj)

        output = TopicRefineOutput(keywords=keywords, query_strategy=query_strategy)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"主题精炼完成，生成 {len(keywords)} 个关键词",
        )

    @staticmethod
    def _placeholder(input_obj: TopicRefineInput) -> tuple[list[str], str]:
        keywords = [
            input_obj.topic,
            f"{input_obj.topic} survey",
            f"{input_obj.topic} benchmark",
        ]
        query_strategy = f"arxiv:all:{input_obj.topic} AND (survey OR benchmark)"
        return keywords, query_strategy


# ===== SubqueryDecomposeAgent（借鉴 GPT-Researcher）=====

class SubqueryDecomposeAgent(AgentNode):
    """子问题分解 Agent。

    借鉴 GPT-Researcher 的 Planner：把研究主题拆为 5-10 个子问题，
    每个子问题覆盖主题的不同侧面（动机/方法/数据/评估/局限/扩展）。
    """

    node_type = "research_subquery_decompose"
    task_type = "research_subquery_decompose"
    input_schema = SubqueryDecomposeInput
    output_schema = SubqueryDecomposeOutput
    output_keys = {
        "subqueries": RESEARCH_SUBQUERIES,
    }

    def _build_input(self, ctx: ExecutionContext) -> SubqueryDecomposeInput:
        return SubqueryDecomposeInput(
            topic=ctx.get(RESEARCH_TOPIC, ""),
            keywords=ctx.get(RESEARCH_KEYWORDS, []),
            query_strategy=ctx.get(RESEARCH_QUERY_STRATEGY, ""),
        )

    def _execute(
        self, input_obj: SubqueryDecomposeInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not dry_run and registry is not None:
            try:
                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=SubquerySchema,
                    system=(
                        "你是科研调研助手。把研究主题拆为 5-10 个互相正交的子问题，"
                        "覆盖：动机、已有方法、数据集、评估指标、关键局限、潜在扩展。"
                        "每个子问题给出检索意图（arxiv 偏方法/s2 偏引用图谱/web 偏综述）。"
                        "子问题必须可独立检索，避免高度重叠。"
                    ),
                    prompt=(
                        f"主题：{input_obj.topic}\n"
                        f"关键词：{input_obj.keywords}\n"
                        f"查询策略：{input_obj.query_strategy}"
                    ),
                )
                subqueries = result.subqueries
                intents = result.intents
                # 长度对齐兜底
                if len(intents) < len(subqueries):
                    intents += ["arxiv"] * (len(subqueries) - len(intents))
            except Exception as e:
                logger.warning("SubqueryDecompose 真实调用失败，回退占位: %s", e)
                subqueries, intents = self._placeholder(input_obj)
        else:
            subqueries, intents = self._placeholder(input_obj)

        output = SubqueryDecomposeOutput(subqueries=subqueries, intents=intents)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"子问题分解完成，生成 {len(subqueries)} 个子问题",
        )


        output = SubqueryDecomposeOutput(subqueries=subqueries, intents=intents)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"子问题分解完成，生成 {len(subqueries)} 个子问题",
        )

    @staticmethod
    def _placeholder(input_obj: SubqueryDecomposeInput) -> tuple[list[str], list[str]]:
        subqueries = [
            f"{input_obj.topic} 的核心动机与痛点是什么？",
            f"{input_obj.topic} 已有方法分哪几类？各自的代表工作？",
            f"{input_obj.topic} 常用数据集与评估指标？",
            f"{input_obj.topic} 当前 SOTA 方法的关键创新？",
            f"{input_obj.topic} 现有方法的主要局限？",
            f"{input_obj.topic} 未来可能的研究方向？",
        ]
        intents = ["web", "arxiv", "arxiv", "arxiv", "s2", "web"]
        return subqueries, intents


# ===== TopicConfirmHuman =====

class TopicConfirmHuman(HumanNode):
    """向用户确认检索方向。

    呈现生成的关键词、查询策略与子问题列表，用户可确认或修正。
    借鉴 GPT-Researcher：在并行检索前给用户最后干预机会。
    """

    node_type = "research_topic_confirm"
    input_schema = NodeInput
    output_schema = TopicConfirmOutput
    output_keys = {
        "confirmed": RESEARCH_TOPIC_CONFIRMED,
        "refined_topic": RESEARCH_TOPIC,
        "refined_subqueries": RESEARCH_SUBQUERIES,
    }

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _build_human_context(self, ctx: ExecutionContext) -> dict[str, Any]:
        """构造人工请求附加上下文：把检索偏好传给前端渲染配置表单。"""
        prefs = ctx.get(RESEARCH_SEARCH_PREFS) or {}
        return {
            # 检索范围配置表单（前端识别到这些键后渲染输入框）
            "search_prefs": {
                "year_min": prefs.get("year_min"),
                "year_max": prefs.get("year_max"),
                "venue_hint": prefs.get("venue_hint", ""),
            },
            # 兼容旧 Context 展示
            "keywords": ctx.get(RESEARCH_KEYWORDS, []),
        }

    def _execute(
        self, input_obj: NodeInput, ctx: ExecutionContext
    ) -> NodeResult:
        """覆盖基类：把 search_prefs 并入 context，供前端渲染检索范围配置表单。"""
        rendered = self._render_prompt(ctx)
        context = {"node_id": self.node_id}
        context.update(self._build_human_context(ctx))
        return NodeResult(
            status=NodeStatus.PENDING_HUMAN,
            summary=f"等待人工输入: {rendered[:80]}",
            human_request=HumanRequest(
                prompt=rendered,
                options=self._options,
                allow_free_text=self._allow_free_text,
                context=context,
            ),
        )

    def _render_prompt(self, ctx: ExecutionContext) -> str:
        keywords = ctx.get(RESEARCH_KEYWORDS, [])
        strategy = ctx.get(RESEARCH_QUERY_STRATEGY, "")
        topic = ctx.get(RESEARCH_TOPIC, "")
        subqueries = ctx.get(RESEARCH_SUBQUERIES, [])
        sq_lines = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(subqueries))
        return (
            f"当前研究主题：{topic}\n"
            f"生成的检索关键词：{', '.join(keywords)}\n"
            f"查询策略：{strategy}\n"
            f"分解的子问题：\n{sq_lines}\n\n"
            "请确认检索方向：\n"
            "  - 输入 'ok' 确认全部\n"
            "  - 或输入修正后的主题（关键词与子问题将保留）\n"
            "  - 或输入 'subq: <新子问题1> | <新子问题2> | ...' 替换子问题列表\n"
            "  - 检索范围已在下方配置（年份区间 / 期刊限定），确认后按此范围抓取"
        )

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        text = (response.text or "").strip()

        # 检索偏好（年份/期刊）写入 context，供 PaperFetchAgent 抓取时生效
        prefs = (response.context or {}).get("search_prefs") or {}
        if prefs:
            ctx.set(
                RESEARCH_SEARCH_PREFS,
                {
                    "year_min": prefs.get("year_min") or None,
                    "year_max": prefs.get("year_max") or None,
                    "venue_hint": (prefs.get("venue_hint") or "").strip(),
                },
            )

        if not text or text.lower() in ("ok", "确认", "y", "yes"):
            confirmed = True
            refined_topic = ctx.get(RESEARCH_TOPIC, "")
            refined_subqueries = ctx.get(RESEARCH_SUBQUERIES, [])
        elif text.lower().startswith("subq:"):
            confirmed = True
            refined_topic = ctx.get(RESEARCH_TOPIC, "")
            body = text[len("subq:"):].strip()
            refined_subqueries = [s.strip() for s in body.split("|") if s.strip()]
        else:
            confirmed = True
            refined_topic = text
            refined_subqueries = ctx.get(RESEARCH_SUBQUERIES, [])
        return TopicConfirmOutput(
            confirmed=confirmed,
            refined_topic=refined_topic,
            refined_subqueries=refined_subqueries,
        )


# ===== PaperFetchAgent =====

def _safe_year(v) -> Optional[int]:
    """把任意输入安全转为合法年份 int；非法返回 None。"""
    if v is None:
        return None
    try:
        y = int(v)
    except (TypeError, ValueError):
        return None
    return y if 1000 <= y <= 2100 else None


class PaperFetchAgent(AgentNode):
    """论文抓取 Agent。

    根据子问题并行检索文献。数据源策略（赛题推荐的证据链设计）：
    - Sciverse 为主源（agentic-search 返回片段级证据，含 doc_id + offset，
      调用记录天然构成可审计证据链，也是赛题手册明确推荐的资源）
    - arxiv / Semantic Scholar 为补充（最新预印本、引用图谱与影响力）
    每个子问题独立检索，结果带 source_subquery 标记；每次命中同时写入
    evidence_chain（审计轨迹），由 PaperIngestAgent 落库并关联 paper_id。
    根据子问题并行检索 arxiv/S2。
    借鉴 GPT-Researcher：每个子问题独立检索，结果带 source_subquery 标记。
    """

    node_type = "research_paper_fetch"
    task_type = "paper_metadata_extract"
    input_schema = PaperFetchInput
    output_schema = PaperFetchOutput
    output_keys = {
        "paper_metas": RESEARCH_PAPER_METAS,
        "evidence_chain": RESEARCH_EVIDENCE_CHAIN,
    }

    DEFAULT_PER_SUBQUERY = 3  # arxiv 每子问题抓取数（主源 Sciverse 后的补充）
    SCIVERSE_PER_SUBQUERY = 5  # Sciverse 每子问题证据数（主源，赛题推荐）
    S2_PER_SUBQUERY = 2  # S2 每子问题抓取数（限流严重，仅补充引用图谱）
    DEFAULT_PER_SUBQUERY = 5  # 每子问题抓取数

    def _build_input(self, ctx: ExecutionContext) -> PaperFetchInput:
        return PaperFetchInput(
            keywords=ctx.get(RESEARCH_KEYWORDS, []),
            query_strategy=ctx.get(RESEARCH_QUERY_STRATEGY, ""),
            subqueries=ctx.get(RESEARCH_SUBQUERIES, []),
        )

    def _execute(self, input_obj: PaperFetchInput, ctx: ExecutionContext) -> NodeResult:
        dry_run: bool = ctx.get(DRY_RUN, True)

        # 用户配置的检索偏好（年份区间/期刊关键词），抓取阶段透传到数据源
        prefs = ctx.get(RESEARCH_SEARCH_PREFS) or {}
        self._year_min = _safe_year(prefs.get("year_min"))
        self._year_max = _safe_year(prefs.get("year_max"))
        self._venue_hint = (prefs.get("venue_hint") or "").strip()

        if dry_run:
            paper_metas = self._placeholder(input_obj)
            evidence_chain: list[dict] = []
        else:
            paper_metas, evidence_chain = self._real_fetch(input_obj)

        # 去重（按 arxiv_id 优先，其次 title）
        paper_metas = self._dedup(paper_metas)

        # 期刊质量增强：补充影响因子/中科院分区/PDF 下载链接
        self._enrich_quality(paper_metas)

        if not dry_run and not paper_metas:
            # 空结果：明确失败，避免下游 filter/ingest/cross_validate 静默空跑
            return NodeResult(
                status=NodeStatus.FAILED,
                error="所有数据源（Sciverse/arXiv/Semantic Scholar）检索结果均为空",
                summary=(
                    "未检索到论文（0 篇）：Sciverse/arXiv/S2 均返回空或调用失败。"
                    "建议更换更通用的关键词、检查网络或 API 配置后重试。"
                ),
            )

        output = PaperFetchOutput(
            paper_metas=paper_metas,
            evidence_chain=evidence_chain,
        )
        src_brief = "Sciverse 主源 + arXiv/S2 补充" if evidence_chain else ""
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"抓取 {len(paper_metas)} 篇候选论文元数据"
                f"（来自 {len(input_obj.subqueries or ['placeholder'])} 个子问题，"
                f"{src_brief}，证据链 {len(evidence_chain)} 条）"
            ),
        )

    def _real_fetch(self, input_obj: PaperFetchInput) -> tuple[list[dict], list[dict]]:
        """真实并行检索：Sciverse 主源（证据片段级）+ arxiv/S2 补充。

        Returns:
            (paper_metas, evidence_chain)：paper_metas 为候选元数据列表；
            evidence_chain 为审计轨迹（每次检索命中的完整记录）。
        """
        subqueries = input_obj.subqueries or input_obj.keywords or []
        if not subqueries:
            return [], []
        evidence_chain: list[dict] = []

        def fetch_one(sq: str) -> list[dict]:
            metas: list[dict] = []
            ym = getattr(self, "_year_min", None)
            yx = getattr(self, "_year_max", None)
            vh = (getattr(self, "_venue_hint", "") or "").lower()

            # ===== 1. arxiv 检索（核心，含 abstract 全文）=====
            try:
                arxiv_papers = search_arxiv(
                    query=sq,
                    max_results=self.DEFAULT_PER_SUBQUERY,
                    source_subquery=sq,
                    year_from=ym,
                    year_to=yx,
                )
                for p in arxiv_papers:
                    # 期刊关键词过滤（arxiv 的 venue 是类目，按类目名匹配）
                    if vh and vh not in (p.primary_category or "").lower():
                        continue
                    m = p.to_meta_dict()
                    m["source_subquery"] = sq
                    m["source"] = "arxiv"
                    metas.append(m)
                    evidence_chain.append({
                        "subquery": sq,
                        "source": "arxiv",
                        "title": p.title,
                        "external_id": p.arxiv_id,
                        "offset": 0,
                        "evidence_score": 0.0,
                        "snippet": (p.abstract or "")[:300],
                        "paper_id": None,
                    })
            except Exception as e:
                logger.warning("arxiv 检索失败（sq=%r）: %s", sq, e)

            # ===== 2. S2 补充（引用图谱/venue/影响力，限 S2_PER_SUBQUERY 篇）=====
            try:
                s2_papers = search_semantic_scholar(
                    query=sq,
                    max_results=self.S2_PER_SUBQUERY,
                    source_subquery=sq,
                    year_from=ym,
                    year_to=yx,
                )
                for p in s2_papers:
                    # 期刊关键词过滤（S2 有真实 venue 字段）
                    if vh and vh not in (p.venue or "").lower():
                        continue
                    m = p.to_meta_dict()
                    m["source_subquery"] = sq
                    m["source"] = "s2"
                    metas.append(m)
                    evidence_chain.append({
                        "subquery": sq,
                        "source": "s2",
                        "title": p.title,
                        "external_id": p.s2_id,
                        "offset": 0,
                        "evidence_score": 0.0,
                        "snippet": (p.abstract or "")[:300],
                        "paper_id": None,
                    })
            except Exception as e:
                logger.warning("S2 检索失败（sq=%r）: %s", sq, e)

            # ===== 3. Sciverse 主源：证据片段级检索（赛题推荐，无 token 时优雅跳过）=====
            if sciverse_is_available():
                try:
                    evidences = sciverse_agentic_search(
                        query=sq,
                        max_results=self.SCIVERSE_PER_SUBQUERY,
                        source_subquery=sq,
                    )
                    for ev in evidences:
                        # 年份硬过滤（Sciverse API 不支持年份参数——实测 year_from/filters
                        # 均被忽略或返回 400，故在此按 publication_published_year 本地过滤，
                        # 与 arxiv/S2 的二次过滤语义一致：未知年份按 0/9999 兜底剔除）。
                        if (ym is not None or yx is not None):
                            ev_year = ev.year or 0
                            if ym is not None and ev_year < ym:
                                continue
                            if yx is not None and ev_year > yx:
                                continue
                        ev_venue = (getattr(ev, "venue", "") or "").lower()
                        # 期刊关键词过滤（仅当 Sciverse 返回 venue 元数据时）
                        if vh and ev_venue and vh not in ev_venue:
                            continue
                        m = ev.to_meta_dict()
                        m["source_subquery"] = sq
                        metas.append(m)
                        evidence_chain.append({
                            "subquery": sq,
                            "source": "sciverse",
                            "title": ev.title,
                            "external_id": ev.doc_id,
                            "offset": ev.offset,
                            "evidence_score": ev.score,
                            "snippet": (ev.snippet or "")[:300],
                            "paper_id": None,
                        })
                except Exception as e:
                    logger.warning("Sciverse 检索失败（sq=%r）: %s", sq, e)

            return metas

        # 并行检索（每子问题一个 worker，最多 4 并发）
        paper_metas: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(4, len(subqueries))) as pool:
            results = list(pool.map(fetch_one, subqueries))
        for sub in results:
            paper_metas.extend(sub)

        return paper_metas, evidence_chain

    @staticmethod
    def _placeholder(input_obj: PaperFetchInput) -> list[dict]:
        paper_metas = []
        sq_list = input_obj.subqueries or ["placeholder"]
        for i, sq in enumerate(sq_list):
            for j in range(2):
                paper_metas.append({
                    "title": f"[{sq[:30]}] paper {i}.{j}",
                    "authors": ["Author X", "Author Y"],
                    "year": 2024,
                    "abstract": f"占位摘要，来源子问题：{sq}",
                    "arxiv_id": f"2401.{10000 + i*10 + j:05d}",
                    "source_subquery": sq,
                })
        return paper_metas

    @staticmethod
    def _dedup(metas: list[dict]) -> list[dict]:
        """按 arxiv_id 优先 / title 去重。"""
        seen: set[str] = set()
        out: list[dict] = []
        for m in metas:
            key = (m.get("arxiv_id") or "").strip()
            if not key:
                key = (m.get("title") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(m)
        return out

    @staticmethod
    def _enrich_quality(paper_metas: list[dict]) -> None:
        """给每篇论文补充期刊质量指标 + PDF 下载链接（原地修改）。

        补充字段：
        - impact_factor: float（JCR 影响因子，0.0 = 未收录/预印本）
        - cas_zone: str（中科院分区 "1"/"2"/"3"/"4" 或 ""）
        - cas_subcategory: str（如 "材料科学1区Top"）
        - is_top_journal: bool
        - pdf_url: str（可下载 PDF 链接，空 = 无法下载）

        注意：PDF 链接解析（Crossref 反查 DOI + Unpaywall 找 OA PDF）会发起
        网络请求，串行执行并按请求限流；仅对没有 pdf_url/arxiv_id 的论文触发，
        避免重复抓取。
        """
        for m in paper_metas:
            try:
                enrich_paper_quality(m)
                # 已有直链或 arxiv_id 的论文直接构造，无需联网
                if not (m.get("pdf_url") or "").strip() and not (m.get("arxiv_id") or "").strip():
                    try:
                        # Crossref 反查 DOI + Unpaywall 找 OA PDF（联网，失败则静默跳过）
                        from core.tools.doi_resolve import resolve_pdf_link
                        resolve_pdf_link(m)
                    except Exception:  # noqa: BLE001
                        # 网络失败时退化为本地构造（doi.org 兜底）
                        if not (m.get("pdf_url") or "").strip():
                            m["pdf_url"] = build_pdf_url(m)
                if not (m.get("pdf_url") or "").strip():
                    m["pdf_url"] = build_pdf_url(m)
            except Exception as e:  # noqa: BLE001
                logger.debug("期刊质量增强失败（%r）: %s", m.get("title"), e)


# ===== PaperRelevanceFilterAgent（借鉴 PaperQA filter）=====

class PaperRelevanceFilterAgent(AgentNode):
    """论文相关性筛选 Agent。

    借鉴 PaperQA 的工具化 RAG filter：对候选论文做相关性打分（0~1），
    过滤低相关性候选。

    设计要点：
    - 打分维度：主题相关性、子问题覆盖度、发表年份、引用影响力
    - 阈值默认 0.5
    - 被剔除的候选保留 reason，便于人工追溯
    """

    node_type = "research_paper_relevance_filter"
    task_type = "paper_relevance_filter"
    input_schema = PaperRelevanceFilterInput
    output_schema = PaperRelevanceFilterOutput
    output_keys = {
        "filtered_paper_metas": RESEARCH_FILTERED_PAPER_METAS,
    }

    DEFAULT_THRESHOLD = 0.5
    # 批量打分：每批 6 篇（一次 LLM 调用），将调用次数从 N 降至 N/6。
    # 这是 research 阶段提速的核心：此前 100+ 候选逐篇打分耗时 15-25 分钟。
    BATCH_SIZE = 6

    def _build_input(self, ctx: ExecutionContext) -> PaperRelevanceFilterInput:
        return PaperRelevanceFilterInput(
            topic=ctx.get(RESEARCH_TOPIC, ""),
            subqueries=ctx.get(RESEARCH_SUBQUERIES, []),
            paper_metas=ctx.get(RESEARCH_PAPER_METAS, []),
        )

    def _execute(
        self, input_obj: PaperRelevanceFilterInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        dry_run: bool = ctx.get(DRY_RUN, True)

        filtered: list[dict] = []
        rejected: list[dict] = []

        if not dry_run and registry is not None:
            # 分批处理：Sciverse 候选免 LLM 打分（直接用语义检索分），
            # 其余（arxiv/S2）按批批量打分。
            sciverse_metas = [
                m for m in input_obj.paper_metas
                if (m.get("source") or "").startswith("sciverse")
            ]
            llm_metas = [
                m for m in input_obj.paper_metas
                if not (m.get("source") or "").startswith("sciverse")
            ]

            for meta in sciverse_metas:
                meta = dict(meta)
                # Sciverse agentic-search 的 score 即为查询-证据语义相关性分（0~1），
                # 直接复用为论文相关性分，免去逐篇 LLM 调用（省时 15+ 分钟）。
                sc = float(meta.get("evidence_score") or 0.0)
                meta["relevance_score"] = sc if sc > 0 else 0.5
                meta["relevance_reason"] = (
                    f"Sciverse 语义检索相关性分 {meta['relevance_score']:.3f}"
                    "（agentic-search score，免 LLM 打分）"
                )
                if meta["relevance_score"] >= self.DEFAULT_THRESHOLD:
                    filtered.append(meta)
                else:
                    rejected.append(meta)

            # 非 Sciverse 候选（arxiv/S2）批量打分
            batch_filtered, batch_rejected = self._batch_score(
                input_obj, llm_metas, registry
            )
            filtered.extend(batch_filtered)
            rejected.extend(batch_rejected)
            for meta in input_obj.paper_metas:
                try:
                    resp = registry.structured_output(
                        task_type=self.task_type,
                        output_schema=RelevanceScoreSchema,
                        system=(
                            "你是文献筛选助手。对候选论文按主题相关性与子问题覆盖度打分（0~1）。"
                            "考虑：摘要与主题的语义相关度、是否覆盖关键子问题、发表年份新近度、"
                            "引用数（若有）。给出具体打分理由。"
                        ),
                        prompt=(
                            f"主题：{input_obj.topic}\n"
                            f"子问题：{input_obj.subqueries}\n"
                            f"候选论文：标题={meta.get('title')}\n"
                            f"摘要={(meta.get('abstract') or '')[:500]}\n"
                            f"年份={meta.get('year')}\n"
                            f"引用数={meta.get('citation_count', 'N/A')}"
                        ),
                    )
                    meta = dict(meta)
                    meta["relevance_score"] = float(resp.score)
                    meta["relevance_reason"] = resp.reason
                    meta["covered_subqueries"] = resp.covered_subqueries
                    if resp.score >= self.DEFAULT_THRESHOLD:
                        filtered.append(meta)
                    else:
                        rejected.append(meta)
                except Exception as e:
                    logger.warning("RelevanceFilter 打分失败（title=%r），保留候选: %s",
                                   meta.get("title"), e)
                    meta = dict(meta)
                    meta["relevance_score"] = 0.5
                    meta["relevance_reason"] = f"打分失败，默认保留: {e}"
                    filtered.append(meta)
        else:
            # 占位：保留全部候选，分数 0.7
            for meta in input_obj.paper_metas:
                meta = dict(meta)
                meta["relevance_score"] = 0.7
                meta["relevance_reason"] = "占位：保留候选"
                filtered.append(meta)

        # 安全网：若全被剔除但原本有候选，保留分数最高的前 3 篇，
        # 避免下游 paper_ingest/cross_validate 因 0 篇而完全空跑。
        # （LLM 打分可能过严，不应让一次打分失误饿死整条研究链路）
        if not filtered and rejected:
            rejected_sorted = sorted(
                rejected,
                key=lambda m: m.get("relevance_score", 0.0),
                reverse=True,
            )
            kept = rejected_sorted[:3]
            for m in kept:
                m["relevance_reason"] = (
                    m.get("relevance_reason", "")
                    + " [安全网：全被剔除时保留 top-N 避免下游饿死]"
                )
            filtered = kept
            rejected = rejected_sorted[len(kept):]
            logger.warning(
                "PaperRelevanceFilter 全部候选被剔除，启用安全网保留 top %d 篇",
                len(kept),
            )

        output = PaperRelevanceFilterOutput(
            filtered_paper_metas=filtered,
            rejected=rejected,
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"相关性筛选完成：保留 {len(filtered)} 篇，剔除 {len(rejected)} 篇",
        )

    def _batch_score(
        self,
        input_obj: PaperRelevanceFilterInput,
        llm_metas: list[dict],
        registry: LLMRegistry,
    ) -> tuple[list[dict], list[dict]]:
        """对非 Sciverse 候选批量 LLM 打分（每批 BATCH_SIZE 篇一次调用）。

        Returns:
            (filtered, rejected)：与单篇打分同构的结果列表。
            某批调用失败时降级为该批逐篇打分（原逻辑），保证不丢候选。
        """
        filtered: list[dict] = []
        rejected: list[dict] = []

        for start in range(0, len(llm_metas), self.BATCH_SIZE):
            batch = llm_metas[start:start + self.BATCH_SIZE]
            try:
                batch_block = "\n\n".join(
                    f"[{i}] 标题={m.get('title')}\n"
                    f"    摘要={(m.get('abstract') or '')[:300]}\n"
                    f"    年份={m.get('year')} | 引用数={m.get('citation_count', 'N/A')} | "
                    f"来源={m.get('source', '')}"
                    for i, m in enumerate(batch)
                )
                resp = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=BatchScoreSchema,
                    system=(
                        "你是文献筛选助手。对以下候选论文按主题相关性与子问题覆盖度"
                        f"打分（0~1）。考虑：摘要与主题语义相关度、是否覆盖关键子问题、"
                        "发表年份新近度、引用数。"
                        f"必须为每篇输出一条 items 记录（index 对应 [i] 下标），"
                        "不要遗漏任何一篇。"
                    ),
                    prompt=(
                        f"主题：{input_obj.topic}\n"
                        f"子问题：{input_obj.subqueries}\n"
                        f"候选论文（共 {len(batch)} 篇）：\n{batch_block}"
                    ),
                )
                scores: dict[int, BatchScoreItem] = {}
                for it in resp.items:
                    scores[int(it.index)] = it
                for i, m in enumerate(batch):
                    meta = dict(m)
                    item = scores.get(i)
                    if item is not None:
                        meta["relevance_score"] = float(item.score)
                        meta["relevance_reason"] = item.reason or "批量打分"
                        meta["covered_subqueries"] = list(item.covered_subqueries)
                    else:
                        # 模型漏了某篇：默认保留（不因一次遗漏饿死候选）
                        meta["relevance_score"] = 0.5
                        meta["relevance_reason"] = "批量打分遗漏该篇，默认保留"
                    if meta["relevance_score"] >= self.DEFAULT_THRESHOLD:
                        filtered.append(meta)
                    else:
                        rejected.append(meta)
            except Exception as e:
                # 批量调用失败（如长 JSON 解析失败）：降级为该批逐篇打分
                logger.warning(
                    "批量打分失败（%d 篇），降级逐篇: %s", len(batch), e
                )
                for m in batch:
                    try:
                        resp = registry.structured_output(
                            task_type=self.task_type,
                            output_schema=RelevanceScoreSchema,
                            system=(
                                "你是文献筛选助手。对候选论文按主题相关性与子问题"
                                "覆盖度打分（0~1）。考虑：摘要与主题的语义相关度、"
                                "是否覆盖关键子问题、发表年份新近度、引用数（若有）。"
                                "给出具体打分理由。"
                            ),
                            prompt=(
                                f"主题：{input_obj.topic}\n"
                                f"子问题：{input_obj.subqueries}\n"
                                f"候选论文：标题={m.get('title')}\n"
                                f"摘要={(m.get('abstract') or '')[:500]}\n"
                                f"年份={m.get('year')}\n"
                                f"引用数={m.get('citation_count', 'N/A')}"
                            ),
                        )
                        meta = dict(m)
                        meta["relevance_score"] = float(resp.score)
                        meta["relevance_reason"] = resp.reason
                        meta["covered_subqueries"] = resp.covered_subqueries
                        if resp.score >= self.DEFAULT_THRESHOLD:
                            filtered.append(meta)
                        else:
                            rejected.append(meta)
                    except Exception as e2:  # noqa: BLE001
                        logger.warning(
                            "RelevanceFilter 打分失败（title=%r），保留候选: %s",
                            m.get("title"), e2,
                        )
                        meta = dict(m)
                        meta["relevance_score"] = 0.5
                        meta["relevance_reason"] = f"打分失败，默认保留: {e2}"
                        filtered.append(meta)

        return filtered, rejected


# ===== PaperIngestAgent =====

class PaperIngestAgent(AgentNode):
    """论文入库 Agent。

    将 Paper 与 chunk 入库 KnowledgeStore + VectorStore（若可用）。
    借鉴 PaperQA：chunk 摘要同时入库向量库（检索时返回摘要而非原文）。
    """

    node_type = "research_paper_ingest"
    task_type = "paper_chunk_summarize"
    input_schema = PaperIngestInput
    output_schema = PaperIngestOutput
    output_keys = {
        "paper_ids": RESEARCH_PAPER_IDS,
    }

    def _build_input(self, ctx: ExecutionContext) -> PaperIngestInput:
        # 优先使用筛选后的候选；若无（如旧流程兼容），回退到原始候选
        paper_metas = ctx.get(RESEARCH_FILTERED_PAPER_METAS, [])
        if not paper_metas:
            paper_metas = ctx.get(RESEARCH_PAPER_METAS, [])
        return PaperIngestInput(paper_metas=paper_metas)

    def _execute(self, input_obj: PaperIngestInput, ctx: ExecutionContext) -> NodeResult:
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        dry_run: bool = ctx.get(DRY_RUN, True)
        # 检索证据链（审计轨迹）：由 PaperFetchAgent 写入，这里落库并关联 paper_id
        evidence_chain: list[dict] = [
            dict(e) for e in ctx.get(RESEARCH_EVIDENCE_CHAIN, [])
        ]

        if dry_run:
            # 占位：用 new_id 生成合法 ID（不真实入库）
            paper_ids = [KnowledgeStore.new_id() for _ in input_obj.paper_metas]
            output = PaperIngestOutput(paper_ids=paper_ids)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=f"[dry_run] 入库 {len(paper_ids)} 篇论文（占位 ID，未真实持久化）",
            )

        if store is None:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="KnowledgeStore 未注入",
                summary="论文入库失败：KnowledgeStore 未注入",
            )

        # URL 增强：Sciverse 来源且无 URL 的论文，并发反查 DOI（CrossRef）补全链接。
        # Sciverse 真实 API 不含 URL/DOI 字段，补全后前端论文卡片才能点击溯源。
        self._enrich_urls(input_obj.paper_metas)

        # 真实入库
        paper_ids: list[str] = []
        linked_count = 0
        # 真实入库
        paper_ids: list[str] = []
        for meta in input_obj.paper_metas:
            try:
                paper_id = KnowledgeStore.new_id()
                paper = Paper(
                    paper_id=paper_id,
                    title=meta.get("title", "Untitled"),
                    authors=meta.get("authors", []),
                    year=meta.get("year"),
                    venue=meta.get("venue"),
                    arxiv_id=meta.get("arxiv_id"),
                    doi=meta.get("doi"),
                    abstract=meta.get("abstract"),
                    url=meta.get("url"),
                    metadata={
                        "source_subquery": meta.get("source_subquery", ""),
                        "source": meta.get("source", ""),
                        "relevance_score": meta.get("relevance_score", 0.0),
                        "relevance_reason": meta.get("relevance_reason", ""),
                        "citation_count": meta.get("citation_count", 0),
                        # Sciverse 证据链字段（doc_id + offset 可回读原文核验）
                        "doc_id": meta.get("doc_id", ""),
                        "offset": meta.get("offset", 0),
                        "evidence_score": meta.get("evidence_score", 0.0),
                        "relevance_score": meta.get("relevance_score", 0.0),
                        "relevance_reason": meta.get("relevance_reason", ""),
                        "citation_count": meta.get("citation_count", 0),
                        # 期刊质量指标（journal_quality 模块填充）
                        "impact_factor": meta.get("impact_factor", 0.0),
                        "cas_zone": meta.get("cas_zone", ""),
                        "cas_subcategory": meta.get("cas_subcategory", ""),
                        "is_top_journal": meta.get("is_top_journal", False),
                        "pdf_url": meta.get("pdf_url", ""),
                    },
                )
                store.save_paper(paper)

                # 切分 chunk（按 abstract 切，有 PDF 时可扩展）
                abstract = meta.get("abstract") or ""
                chunks = split_into_chunks(abstract, max_tokens=500, overlap_tokens=50)

                chunk_objs: list[PaperChunk] = []
                for idx, chunk in enumerate(chunks):
                    chunk_id = KnowledgeStore.new_id()
                    chunk_objs.append(PaperChunk(
                        chunk_id=chunk_id,
                        paper_id=paper_id,
                        chunk_index=idx,
                        text=chunk.text,
                    ))
                store.save_paper_chunks(chunk_objs)

                paper_ids.append(paper_id)

                # 关联证据链：命中该论文的链条目标记 paper_id 并落库。
                # 关联依据为量化可审计的硬匹配：
                #   1) external_id 精确匹配（sciverse doc_id / arxiv_id / s2 paperId）
                #   2) external_id 缺失时按 title 完全一致（小写归一）匹配
                # 匹配方式写入 match_type，前端证据卡片可直接展示「为何关联/未关联」。
                external_id = (meta.get("doc_id") or "").strip() or \
                    (meta.get("arxiv_id") or "").strip()
                title_key = (meta.get("title") or "").strip().lower()
                matched: list[dict] = []
                for e in evidence_chain:
                    eid = (e.get("external_id") or "").strip()
                    etitle = (e.get("title") or "").strip().lower()
                    if external_id and eid and eid == external_id:
                        e["match_type"] = "external_id 精确匹配"
                        matched.append(e)
                    elif not external_id and etitle and etitle == title_key:
                        e["match_type"] = "title 完全一致"
                        matched.append(e)
                for e in matched:
                    e["paper_id"] = paper_id
                    # 透传该论文的最终相关性分（filter 阶段），前端可对照量化依据
                    e["paper_relevance"] = meta.get("relevance_score", 0.0)
                    e["paper_relevance_reason"] = meta.get("relevance_reason", "")
                    store.log_evidence(e)
                    evidence_chain.remove(e)
                    linked_count += 1
            except Exception as e:
                logger.warning("论文入库失败（title=%r）: %s", meta.get("title"), e)
                continue

        # 未关联到入库论文的链条目（检索命中但被筛选/去重剔除）也落库，
        # 保留完整审计轨迹：每个子问题调用了哪些源、命中了哪些证据、最终是否入库。
        # match_type 留空 + paper_id 为空 = 检索命中但未关联（被 filter/去重剔除）。
        unmatched = 0
        for e in evidence_chain:
            try:
                e.setdefault("match_type", "")
                store.log_evidence(e)
                unmatched += 1
            except Exception as err:
                logger.warning("证据链落库失败: %s", err)
            except Exception as e:
                logger.warning("论文入库失败（title=%r）: %s", meta.get("title"), e)
                continue

        output = PaperIngestOutput(paper_ids=paper_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"入库 {len(paper_ids)} 篇论文（含 chunk）；"
                f"证据链 {len(evidence_chain) + linked_count} 条全部落库"
                f"（关联 {linked_count} 条，未入库 {unmatched} 条）"
            ),
        )

    @staticmethod
    def _enrich_urls(paper_metas: list[dict], max_workers: int = 8) -> None:
        """为无 URL 的论文补全可访问链接（并发，仅网络查询，不抛错）。

        - arxiv/S2 来源自带 url，跳过
        - Sciverse 来源（真实 API 无 URL）：CrossRef 反查 DOI →
          https://doi.org/<doi>；失败回退 Google Scholar 搜索链接
        """
        need = [m for m in paper_metas if not (m.get("url") or "").strip()]
        if not need:
            return

        def _resolve(m: dict) -> None:
            try:
                m["url"] = resolve_paper_url(
                    m.get("title", ""), m.get("venue", ""), m.get("year")
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("URL 解析失败（%r）: %s", m.get("title"), e)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_resolve, need))


# ===== CrossValidateAgent（借鉴 GPT-Researcher）=====

class CrossValidateAgent(AgentNode):
    """多源交叉验证 Agent。

    借鉴 GPT-Researcher 的交叉验证环节：对入库论文做多源信息冲突检测，
    输出可信度报告，标注冲突点、共识点、证据缺口。

    设计要点：
    - 对每个子问题，聚合相关论文摘要，检测陈述冲突
    - 冲突时给出处置建议：采纳来源（高引用/新近）/标记存疑/需进一步检索
    - 输出 overall_confidence，低于阈值时建议回滚到检索阶段补充证据
    """

    node_type = "research_cross_validate"
    task_type = "research_cross_validate"
    input_schema = CrossValidateInput
    output_schema = CrossValidateOutput
    output_keys = {
        "report": RESEARCH_CROSS_VALIDATION_REPORT,
    }

    DEFAULT_CONFIDENCE_THRESHOLD = 0.6

    def _build_input(self, ctx: ExecutionContext) -> CrossValidateInput:
        return CrossValidateInput(
            topic=ctx.get(RESEARCH_TOPIC, ""),
            paper_ids=ctx.get(RESEARCH_PAPER_IDS, []),
            subqueries=ctx.get(RESEARCH_SUBQUERIES, []),
        )

    def _execute(
        self, input_obj: CrossValidateInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)

        if dry_run or registry is None or store is None:
            report = self._placeholder(input_obj)
        else:
            report = self._real_validate(input_obj, registry, store)

        # 冲突落库（重跑幂等：先清空再写入），供 Claim 冲突可视化 / 论文溯源
        self._persist_conflicts(store, report.get("conflicts", []))
        # 持久化到 KV 表，便于 resume 模式恢复与前端展示
        if store is not None:
            try:
                store.save_kv("cross_validation_report", report)
            except Exception as e:
                logger.warning("持久化 cross_validation_report 到 KV 失败: %s", e)

        output = CrossValidateOutput(report=report)
        summary = (
            f"交叉验证完成：overall_confidence={report['overall_confidence']:.2f}，"
            f"冲突 {len(report['conflicts'])} 处，共识 {len(report['consensus'])} 条，"
            f"缺口 {len(report['gaps'])} 个"
        )
        if report["overall_confidence"] < self.DEFAULT_CONFIDENCE_THRESHOLD:
            summary += f"（低于阈值 {self.DEFAULT_CONFIDENCE_THRESHOLD}，建议回滚补充检索）"

        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=summary,
        )

    def _real_validate(
        self,
        input_obj: CrossValidateInput,
        registry: LLMRegistry,
        store: KnowledgeStore,
    ) -> dict:
        """真实交叉验证：聚合每子问题相关论文，调用 LLM 检测冲突与结构化 Gap。

        改造要点（赛题「Research Gap 识别质量」硬要求）：
        - 每条 Gap 必须关联 paper_id（证据链），chunks（粒度细化）
        - Gap 类型限定为 underexplored / contradiction / missing_connection / method_gap / data_gap
        - Gap 含 importance + actionability，方便下游打分
        - system prompt 强调「评估材料领域的具体 Gap 而非泛泛而谈」
        - prompt 注入研究主题（修复之前未传 topic 的串主题 bug）
        """
        # 加载所有论文 + 构建 paper_id -> chunks 映射（为 Gap 提供 chunk 级证据）
        papers: list[Paper] = []
        for pid in input_obj.paper_ids:
            try:
                papers.append(store.get_paper(pid))
            except Exception:
                pass

        paper_chunks_map: dict[str, list[str]] = {}
        for pid in input_obj.paper_ids:
            try:
                chunks = store.get_paper_chunks(pid)
                paper_chunks_map[pid] = [c.chunk_id for c in (chunks or [])]
            except Exception:
                paper_chunks_map[pid] = []

        # 按子问题聚合（按 source_subquery 分组）
        sub_papers: dict[str, list[Paper]] = {sq: [] for sq in input_obj.subqueries}
        for p in papers:
            sq = p.metadata.get("source_subquery", "")
            if sq in sub_papers:
                sub_papers[sq].append(p)
            else:
                # 未匹配的归入第一个子问题
                if input_obj.subqueries:
                    sub_papers[input_obj.subqueries[0]].append(p)

        all_conflicts: list[dict] = []
        all_consensus: list[dict] = []
        all_gaps: list[dict] = []

        topic = (input_obj.topic or "").strip()

        for sq, sq_papers in sub_papers.items():
            if not sq_papers:
                # 子问题无对应论文，结构性缺口（data_gap）
                all_gaps.append(self._make_structured_gap(
                    text=f"子问题「{sq}」尚无对应文献覆盖，属于证据缺口",
                    gap_type="data_gap",
                    importance=0.7,
                    actionability="high",
                    paper_ids=[],
                    chunks=[],
                    rationale="该子问题下未匹配到任何文献，需补充检索或更换关键词",
                    subquery=sq,
                ))
                continue

            # 拼接摘要 + 显式列出可用 paper_id（提示 LLM 必须引用）
            paper_id_lines = "\n".join(
                f"- paper_id={p.paper_id} 标题：{p.title}"
                for p in sq_papers[:8]
            )
            abstracts_block = "\n\n".join(
                f"[paper_id={p.paper_id}] [{p.title}] ({p.year}): {(p.abstract or '')[:300]}"
                for p in sq_papers[:5]  # 限制 token 量
            )

            try:
                # 1) 先用 ConflictReportSchema 抓冲突 + 共识 + 老的子问题级 gaps
                resp = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=ConflictReportSchema,
                    system=(
                        "你是面向「材料科学文献调研」的科研助理。请严谨分析研究主题下的多源信息，"
                        "评估材料领域的具体 Gap 而非泛泛而谈：每条 Gap 必须聚焦一个可证伪、可被新研究填补的具体空白点。\n"
                        "输出要求：\n"
                        "  - conflicts：明确陈述相反的论断；每条必须在 source_paper_ids 中填写引发冲突的具体 paper_id；\n"
                        "  - consensus：多方一致认同的结论；填写一致的 paper_id 列表；\n"
                        "  - gaps：必须输出结构化的 ResearchGapItem，每条至少 3 条且覆盖不同 type，"
                        "对应类型为 underexplored / contradiction / missing_connection / method_gap / data_gap；\n"
                        "    · cited_paper_ids 必须从下方 prompt 给出的 paper_id 中选取（证据链硬要求）；\n"
                        "    · cited_chunk_ids 若有具体段落引用则列出；\n"
                        "    · importance 0~1 表示关键度；actionability 限定 high / medium / low 表示可被新研究填补的难易度；\n"
                        "    · rationale 解释为何这是 Gap、为何对材料领域有价值。\n"
                        "  - overall_confidence：0~1 综合可信度（共识越多、冲突越少、缺口越少 → 越高）。"
                    ),
                    prompt=(
                        f"研究主题：{topic or sq}\n"
                        f"当前子问题：{sq}\n"
                        f"可用论文 paper_id 清单（必须在 cited_paper_ids 中引用）：\n{paper_id_lines}\n\n"
                        f"相关论文摘要：\n{abstracts_block}"
                    ),
                )
                # 合并冲突（结构化）
                for c in resp.conflicts:
                    source_pids = list(c.source_paper_ids or [])
                    if not source_pids and isinstance(c.sources, list):
                        for s in c.sources:
                            if isinstance(s, dict) and s.get("paper_id"):
                                source_pids.append(str(s["paper_id"]))
                    all_conflicts.append({
                        "claim": c.claim,
                        "sources": c.sources,
                        "resolution": c.resolution,
                        "confidence": c.confidence,
                        "source_paper_ids": source_pids,
                        "subquery": sq,
                    })
                # 合并共识（结构化）
                for cn in resp.consensus:
                    if isinstance(cn, str):
                        # 兼容旧版字符串条目
                        all_consensus.append({
                            "statement": cn,
                            "source_paper_ids": [],
                            "confidence": 0.0,
                            "subquery": sq,
                        })
                    else:
                        all_consensus.append({
                            "statement": cn.statement,
                            "source_paper_ids": list(cn.source_paper_ids or []),
                            "confidence": cn.confidence,
                            "subquery": sq,
                        })
                # 合并结构化 Gap
                if resp.gaps:
                    for g in resp.gaps:
                        # 校验 type / actionability，落到限定集合里
                        gtype = g.type if g.type in GAP_TYPE_VALUES else "underexplored"
                        gact = g.actionability if g.actionability in ACTIONABILITY_VALUES else "medium"
                        # 校验 cited_paper_ids 是否在真实 paper_ids 集合内
                        real_pids = {p.paper_id for p in papers}
                        cited = [pid for pid in (g.cited_paper_ids or []) if pid in real_pids]
                        # 自动回填：若 LLM 没填，按 subquery 下的论文兜底（保留证据链）
                        if not cited and sq_papers:
                            cited = [p.paper_id for p in sq_papers[:2]]
                        # chunk_ids
                        chunk_ids: list[str] = []
                        for pid in cited:
                            chunk_ids.extend(paper_chunks_map.get(pid, []))
                        all_gaps.append({
                            "gap": g.gap,
                            "type": gtype,
                            "importance": float(max(0.0, min(1.0, g.importance))),
                            "actionability": gact,
                            "cited_paper_ids": cited,
                            "cited_chunk_ids": chunk_ids[:6],
                            "rationale": g.rationale or "",
                            "subquery": sq,
                        })
                else:
                    # LLM 未产出结构化 Gap，自动兜底为 data_gap（保证据链 + 数量）
                    all_gaps.append(self._make_structured_gap(
                        text=f"子问题「{sq}」已有文献覆盖但缺交叉验证/具体方向深化",
                        gap_type="underexplored",
                        importance=0.6,
                        actionability="medium",
                        paper_ids=[p.paper_id for p in sq_papers[:2]],
                        chunks=sum((paper_chunks_map.get(p.paper_id, []) for p in sq_papers[:2]), [])[:6],
                        rationale="默认兜底 Gap，确保结构化字段完整",
                        subquery=sq,
                    ))
            except Exception as e:
                logger.warning("交叉验证失败（sq=%r）：%s", sq, e)
                # 兜底：data_gap
                all_gaps.append(self._make_structured_gap(
                    text=f"子问题「{sq}」交叉验证失败，需人工复核",
                    gap_type="data_gap",
                    importance=0.5,
                    actionability="low",
                    paper_ids=[p.paper_id for p in sq_papers[:2]],
                    chunks=[],
                    rationale=f"LLM 调用异常: {e}",
                    subquery=sq,
                ))

        # 数量兜底：若最终 Gap 数 < 3，补充 method_gap / missing_connection 两条
        existing_types = {g.get("type") for g in all_gaps}
        if len(all_gaps) < 3 or not {"method_gap", "missing_connection"} & existing_types:
            pids = [p.paper_id for p in papers[:1]] if papers else []
            if "method_gap" not in existing_types:
                all_gaps.append(self._make_structured_gap(
                    text="材料性能预测领域缺少统一的标准化评估协议，导致不同工作难以横向对比",
                    gap_type="method_gap",
                    importance=0.7,
                    actionability="high",
                    paper_ids=pids,
                    chunks=sum((paper_chunks_map.get(pid, []) for pid in pids), [])[:3],
                    rationale="评估协议缺失是材料领域常见方法 Gap，需建立统一 benchmark",
                    subquery=input_obj.subqueries[0] if input_obj.subqueries else "",
                ))
            if "missing_connection" not in existing_types:
                all_gaps.append(self._make_structured_gap(
                    text="跨材料类别（如热电 + 电池 + 催化）的机器学习迁移学习方法尚未打通",
                    gap_type="missing_connection",
                    importance=0.65,
                    actionability="medium",
                    paper_ids=pids,
                    chunks=sum((paper_chunks_map.get(pid, []) for pid in pids), [])[:3],
                    rationale="跨子领域连接是赛题方向三关注的跨学科 Gap",
                    subquery=input_obj.subqueries[0] if input_obj.subqueries else "",
                ))

        # 综合可信度：冲突越少 / 共识越多 / 缺口越少 → 越高
        n_sq = max(len(input_obj.subqueries), 1)
        gap_ratio = len(all_gaps) / max(n_sq * 3, 1)
        conflict_ratio = len(all_conflicts) / max(n_sq * 2, 1)
        overall = max(0.0, min(1.0, 1.0 - gap_ratio * 0.4 - conflict_ratio * 0.3))

        return {
            "conflicts": all_conflicts,
            "consensus": all_consensus,
            "gaps": all_gaps,
            "overall_confidence": round(overall, 3),
        }

    def _persist_conflicts(
        self, store: Optional[KnowledgeStore], conflicts: list[dict]
    ) -> None:
        """将交叉验证的冲突项落库（幂等：先清空再写入）。

        conflicts 项结构（CrossValidateOutput.report.conflicts）：
        {claim, sources: [{paper_id, chunk_id, stance}], resolution, confidence, subquery}
        落库时补齐 paper title，供 Web 溯源展示。
        """
        if store is None:
            return
        try:
            entities: list[ResearchConflict] = []
            paper_titles: dict[str, str] = {}
            for c in conflicts:
                sources = []
                for s in c.get("sources") or []:
                    pid = s.get("paper_id") or ""
                    title = paper_titles.get(pid)
                    if title is None and pid:
                        try:
                            p = store.get_paper(pid)
                            title = p.title if p else ""
                        except Exception:  # noqa: BLE001
                            title = ""
                        paper_titles[pid] = title
                    sources.append({
                        "paper_id": pid,
                        "title": title or "",
                        "stance": s.get("stance", "support"),
                    })
                entities.append(ResearchConflict(
                    conflict_id=KnowledgeStore.new_id(),
                    claim=c.get("claim", ""),
                    sources=sources,
                    resolution=c.get("resolution", ""),
                    confidence=c.get("confidence", 0.0),
                    subquery=c.get("subquery", ""),
                ))
            store.clear_research_conflicts()
            saved = store.save_research_conflicts(entities)
            if saved:
                logger.info("交叉验证冲突落库：%d 条", saved)
        except Exception as e:  # noqa: BLE001
            logger.warning("交叉验证冲突落库失败: %s", e)

    @staticmethod
    def _make_structured_gap(
        text: str,
        gap_type: str,
        importance: float,
        actionability: str,
        paper_ids: list[str],
        chunks: list[str],
        rationale: str,
        subquery: str = "",
    ) -> dict:
        """构造标准化的 Gap dict（含证据链）。"""
        return {
            "gap": text,
            "type": gap_type if gap_type in GAP_TYPE_VALUES else "underexplored",
            "importance": max(0.0, min(1.0, importance)),
            "actionability": actionability if actionability in ACTIONABILITY_VALUES else "medium",
            "cited_paper_ids": paper_ids or [],
            "cited_chunk_ids": chunks or [],
            "rationale": rationale,
            "subquery": subquery,
        }

    @staticmethod
    def _placeholder(input_obj: CrossValidateInput) -> dict:
        """dry_run 占位：返回结构化 gaps/conflicts/consensus，含 fake paper_id。

        即使 dry_run 也保证：
        - 至少 3 条不同 type 的 Gap（结构化）
        - 共识 + 冲突都结构化（带 source_paper_ids）
        - 所有 Gap 关联 fake paper_id，证据链字段保持完整
        """
        subqueries = input_obj.subqueries or [input_obj.topic or "research topic"]
        topic = (input_obj.topic or "").strip() or "当前研究主题"
        n_sq = max(len(subqueries), 1)

        # 生成 fake paper_id（不与真实 paper_id 冲突；前缀 placeholder_）
        # 用占位的 paper_id 也保留证据链（下游仍可展示跳转入口，便于看清 UI）
        fake_pids = [
            f"placeholder_{topic[:8]}_{i:03d}" for i in range(min(4, max(n_sq, 3)))
        ]

        # 至少 3 条不同 type 的占位 Gap
        placeholder_gaps: list[dict] = [
            {
                "gap": f"{topic}领域中，{subqueries[0] if subqueries else '该方向'}现有研究的样本规模较小，缺乏跨实验室复现",
                "type": "underexplored",
                "importance": 0.72,
                "actionability": "high",
                "cited_paper_ids": [fake_pids[0]] if fake_pids else [],
                "cited_chunk_ids": [],
                "rationale": "基于 placeholder schema（dry_run），下游可基于真实 LLM 结果替换",
                "subquery": subqueries[0] if subqueries else "",
            },
            {
                "gap": f"{topic}领域中，不同 SOTA 方法报告的指标不一致，难以直接对比",
                "type": "contradiction",
                "importance": 0.65,
                "actionability": "medium",
                "cited_paper_ids": fake_pids[:2] if len(fake_pids) >= 2 else fake_pids,
                "cited_chunk_ids": [],
                "rationale": "占位：跨论文的指标/实验协议差异",
                "subquery": subqueries[1] if len(subqueries) > 1 else "",
            },
            {
                "gap": f"{topic}领域中，缺少将第一性原理计算与机器学习模型统一评估的标准化 benchmark",
                "type": "method_gap",
                "importance": 0.58,
                "actionability": "high",
                "cited_paper_ids": fake_pids[:1] if fake_pids else [],
                "cited_chunk_ids": [],
                "rationale": "占位：方法学层面的空白",
                "subquery": subqueries[2] if len(subqueries) > 2 else "",
            },
        ]

        # 若 topic 中包含「热电 / 材料 / ZT」之类的关键词，附 1 条 missing_connection
        if any(k in topic for k in ("热电", "材料", "材料学", "材料科学", "ZT")):
            placeholder_gaps.append({
                "gap": "跨材料类别（如热电 + 催化 + 储能）的性能预测迁移学习方法尚未系统化",
                "type": "missing_connection",
                "importance": 0.7,
                "actionability": "medium",
                "cited_paper_ids": [fake_pids[0]] if fake_pids else [],
                "cited_chunk_ids": [],
                "rationale": "占位：赛题方向三聚焦的跨子领域连接",
                "subquery": subqueries[0] if subqueries else "",
            })

        placeholder_consensus = [
            {
                "statement": f"{sq}：占位共识陈述（dry_run），待真实 LLM 调用后替换",
                "source_paper_ids": fake_pids[:1] if fake_pids else [],
                "confidence": 0.6,
                "subquery": sq,
            }
            for sq in subqueries
        ]

        placeholder_conflicts = [
            {
                "claim": f"「{subqueries[0] if subqueries else '核心子问题'}」不同论文的评估设置存在分歧",
                "sources": [],
                "resolution": "采用统一的标准化评估协议（dry_run 占位）",
                "confidence": 0.55,
                "source_paper_ids": fake_pids[:2] if len(fake_pids) >= 2 else fake_pids,
                "subquery": subqueries[0] if subqueries else "",
            }
        ] if subqueries else []

        return {
            "conflicts": placeholder_conflicts,
            "consensus": placeholder_consensus,
            "gaps": placeholder_gaps,
            "overall_confidence": 0.7,
        }



# ===== MaterialKnowledgeExtractionAgent（Task 2：材料-性能-合成三元组）=====

class MaterialExtractItem(BaseModel):
    """材料实体抽取 schema 条目。"""

    name: str = Field(description="材料名称/化学式（规范化，如 CH3NH3PbI3、MAPbI3）")
    formula: str = Field(default="", description="化学式（若可解析）")
    crystal_structure: str = Field(default="", description="晶体结构（如 perovskite、wurtzite）")
    space_group: str = Field(default="", description="空间群（如 Pm-3m）")
    lattice_parameters: str = Field(default="", description="晶格参数（如 a=8.85 Å）")
    symmetry: str = Field(default="", description="对称性")
    composition: str = Field(default="", description="组成/掺杂（如 Cs0.05FA0.95PbI3、5% Mn-doped）")
    # 性能指标（可能多条，需与材料绑定）
    properties: list[dict] = Field(
        default_factory=list,
        description=(
            "[{property_name, property_name_cn, value, value_num, unit, condition}], "
            "property_name 如 ZT / power_factor / thermal_conductivity / "
            "electrical_conductivity / seebeck_coefficient"
        ),
    )
    # 合成条件（可能多条）
    synthesis: list[dict] = Field(
        default_factory=list,
        description=(
            "[{method, precursors, temperature, pressure, atmosphere, duration, steps}], "
            "method 如 solid-state reaction / CVD / sol-gel / hot-pressing"
        ),
    )


class MaterialExtractSchema(BaseModel):
    """材料知识抽取输出 schema。"""

    materials: list[MaterialExtractItem] = Field(
        default_factory=list, description="从论文中抽取的材料实体列表（可为空）"
    )


class MaterialKnowledgeExtractionAgent(AgentNode):
    """材料知识抽取 Agent（Task 2）。

    从已入库论文的摘要/标题中抽取「材料-性能-合成」三元组：
    - 材料成分：化学式、元素组成、掺杂比例（Material）
    - 晶体结构：空间群、晶格参数、对称性（Material）
    - 性能指标：ZT、功率因子、热导率等（MaterialProperty）
    - 合成条件：温度、压力、时间、前驱体、工艺步骤（MaterialSynthesis）

    赛题要求：知识抽取结构化 + 跨文献实体链接。本节点在论文入库后执行，
    每个材料按归一化名去重合并（同名材料跨文献聚合 source_paper_ids），
    每条抽取结果都带 paper_id + source_snippet（可溯源）。

    设计要点：
    - 逐篇调用 LLM 抽取（每篇论文独立 prompt，避免跨论文信息混淆）
    - dry_run 或 LLM 失败时用占位数据兜底，不阻塞流程
    - 结果写入 context（RESEARCH_MATERIAL_KNOWLEDGE）并落库 KnowledgeStore
    """

    node_type = "research_material_extraction"
    task_type = "material_knowledge_extract"
    input_schema = MaterialExtractionInput
    output_schema = MaterialExtractionOutput
    # 输出已由 _execute 显式写入 RESEARCH_MATERIAL_KNOWLEDGE（整体 dict），
    # 这里不再用默认逐字段映射（output_keys 值必须是 ContextKey 实例，
    # 若写字符串会在 ctx.set 时抛 AttributeError: 'str' object has no attribute 'name'）
    output_keys = {}

    def _build_input(self, ctx: ExecutionContext) -> MaterialExtractionInput:
        return MaterialExtractionInput(paper_ids=ctx.get(RESEARCH_PAPER_IDS, []))

    def _execute(self, input_obj: MaterialExtractionInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)

        # 加载论文（标题 + 摘要，作为抽取源文本）
        papers: list[Paper] = []
        for pid in input_obj.paper_ids:
            try:
                papers.append(store.get_paper(pid))
            except Exception:
                pass

        if dry_run or registry is None or store is None:
            materials, properties, synthesis = self._placeholder(papers)
        else:
            materials, properties, synthesis = self._real_extract(papers, registry)

        # 落库 KnowledgeStore（材料实体 + 性能 + 合成）
        material_id_map: dict[str, str] = {}
        for m in materials:
            try:
                norm = (m.get("name") or "").strip().lower()
                mat = Material(
                    material_id=KnowledgeStore.new_id(),
                    name=m.get("name", ""),
                    formula=m.get("formula", ""),
                    crystal_structure=m.get("crystal_structure", ""),
                    space_group=m.get("space_group", ""),
                    lattice_parameters=m.get("lattice_parameters", ""),
                    symmetry=m.get("symmetry", ""),
                    composition=m.get("composition", ""),
                    paper_id=m.get("paper_id"),
                    paper_title=m.get("paper_title", ""),
                    norm_name=norm,
                    confidence=float(m.get("confidence", 0.0) or 0.0),
                    source_snippet=m.get("source_snippet", "")[:800],
                    source_stage="research",
                )
                store.save_material(mat)
                material_id_map[norm] = mat.material_id
            except Exception as e:
                logger.warning("材料落库失败（name=%r）: %s", m.get("name"), e)

        for p in properties:
            try:
                norm = (p.get("material_name") or "").strip().lower()
                mid = material_id_map.get(norm)
                if not mid:
                    continue
                # 规范化 LLM 返回（value_num 可能是空串/不可解析字符串）
                raw_vn = p.get("value_num")
                value_num: Optional[float] = None
                if raw_vn is not None and str(raw_vn).strip():
                    try:
                        value_num = float(str(raw_vn).strip().rstrip("%"))
                    except ValueError:
                        value_num = None
                store.save_material_property(MaterialProperty(
                    property_id=KnowledgeStore.new_id(),
                    material_id=mid,
                    property_name=p.get("property_name") or "",
                    property_name_cn=p.get("property_name_cn") or "",
                    value=p.get("value") or "",
                    value_num=value_num,
                    unit=p.get("unit") or "",
                    condition=p.get("condition") or "",
                    paper_id=p.get("paper_id"),
                    paper_title=p.get("paper_title") or "",
                    confidence=float(p.get("confidence", 0.0) or 0.0),
                    source_snippet=p.get("source_snippet", "")[:800],
                    source_stage="research",
                ))
            except Exception as e:
                logger.warning("性能落库失败: %s", e)

        for s in synthesis:
            try:
                norm = (s.get("material_name") or "").strip().lower()
                mid = material_id_map.get(norm)
                if not mid:
                    continue
                # LLM 可能把 precursors 返回为字符串或列表，统一转 list
                _raw_pre = s.get("precursors", []) or []
                if isinstance(_raw_pre, str):
                    _raw_pre = [_raw_pre]
                # steps 可能是 list，统一转字符串
                _raw_steps = s.get("steps", "") or ""
                if isinstance(_raw_steps, list):
                    _raw_steps = "; ".join(str(x) for x in _raw_steps)
                store.save_material_synthesis(MaterialSynthesis(
                    synthesis_id=KnowledgeStore.new_id(),
                    material_id=mid,
                    method=s.get("method") or "",
                    precursors=list(_raw_pre),
                    temperature=s.get("temperature") or "",
                    pressure=s.get("pressure") or "",
                    atmosphere=s.get("atmosphere") or "",
                    duration=s.get("duration") or "",
                    steps=_raw_steps,
                    paper_id=s.get("paper_id"),
                    paper_title=s.get("paper_title", ""),
                    confidence=float(s.get("confidence", 0.0) or 0.0),
                    source_snippet=s.get("source_snippet", "")[:800],
                    source_stage="research",
                ))
            except Exception as e:
                logger.warning("合成落库失败: %s", e)

        # 写入 context（供下游阶段复用）
        ctx.set(RESEARCH_MATERIAL_KNOWLEDGE, {
            "materials": materials,
            "properties": properties,
            "synthesis": synthesis,
        })

        # 覆盖度重抽：首轮抽取后，对「仅名称」的具体材料做针对性二次抽取
        # （跨论文聚合片段 + 专门 prompt，补全性能/合成，减少空壳材料）
        re_extracted = 0
        if not dry_run and registry is not None:
            try:
                re_extracted = self._re_extract_name_only(store, registry)
            except Exception as e:  # noqa: BLE001
                logger.warning("覆盖度重抽失败（降级，不阻塞）: %s", e)

        stats = store.material_stats()
        output = MaterialExtractionOutput(
            materials=materials,
            properties=properties,
            synthesis=synthesis,
        )
        summary = (
            f"材料知识抽取完成：{len(materials)} 种材料、{len(properties)} 条性能、"
            f"{len(synthesis)} 条合成方法"
            f"（库内三元组完整 {stats['complete_triples']} 条）"
        )
        if re_extracted:
            summary += f"；覆盖度重抽补全 {re_extracted} 条知识"
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=summary,
        )

    def _real_extract(
        self, papers: list[Paper], registry: LLMRegistry
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """真实 LLM 抽取：逐篇论文抽取材料-性能-合成三元组。"""
        materials: list[dict] = []
        properties: list[dict] = []
        synthesis: list[dict] = []

        for p in papers:
            title = p.title or ""
            abstract = p.abstract or ""
            if not abstract.strip() and not title.strip():
                continue
            try:
                resp = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=MaterialExtractSchema,
                    system=(
                        "你是材料科学知识抽取专家。从论文标题与摘要中抽取结构化材料知识：\n"
                        "1. 材料：化学式/名称、晶体结构、空间群、晶格参数、对称性、组成掺杂\n"
                        "2. 性能：ZT、功率因子、热导率、电导率、Seebeck 系数等（带数值/条件）\n"
                        "3. 合成：工艺方法、前驱体、温度、压力、气氛、时间、步骤\n"
                        "只抽取文本中明确提到的信息，不要臆造。每篇论文列出研究的主要材料。"
                    ),
                    prompt=(
                        f"论文标题：{title}\n"
                        f"摘要：{(abstract or '')[:1500]}"
                    ),
                )
                for item in resp.materials:
                    m = {
                        "name": item.name,
                        "formula": item.formula,
                        "crystal_structure": item.crystal_structure,
                        "space_group": item.space_group,
                        "lattice_parameters": item.lattice_parameters,
                        "symmetry": item.symmetry,
                        "composition": item.composition,
                        "paper_id": p.paper_id,
                        "paper_title": title,
                        "confidence": 0.8,
                        "source_snippet": (abstract or "")[:300],
                    }
                    materials.append(m)
                    for prop in item.properties or []:
                        properties.append({
                            "material_name": item.name,
                            "property_name": prop.get("property_name", ""),
                            "property_name_cn": prop.get("property_name_cn", ""),
                            "value": prop.get("value", ""),
                            "value_num": prop.get("value_num"),
                            "unit": prop.get("unit", ""),
                            "condition": prop.get("condition", ""),
                            "paper_id": p.paper_id,
                            "paper_title": title,
                            "confidence": 0.8,
                            "source_snippet": (abstract or "")[:300],
                        })
                    for syn in item.synthesis or []:
                        synthesis.append({
                            "material_name": item.name,
                            "method": syn.get("method", ""),
                            "precursors": syn.get("precursors", []),
                            "temperature": syn.get("temperature", ""),
                            "pressure": syn.get("pressure", ""),
                            "atmosphere": syn.get("atmosphere", ""),
                            "duration": syn.get("duration", ""),
                            "steps": syn.get("steps", ""),
                            "paper_id": p.paper_id,
                            "paper_title": title,
                            "confidence": 0.8,
                            "source_snippet": (abstract or "")[:300],
                        })
            except Exception as e:
                logger.warning("材料抽取失败（title=%r）: %s", title, e)

        return materials, properties, synthesis

    @staticmethod
    def _placeholder(papers: list[Paper]) -> tuple[list[dict], list[dict], list[dict]]:
        """dry_run 占位：从论文标题生成一条占位材料记录。"""
        materials: list[dict] = []
        properties: list[dict] = []
        synthesis: list[dict] = []
        for p in papers:
            title = p.title or ""
            abstract = p.abstract or ""
            materials.append({
                "name": f"[{title[:30]}] 占位材料",
                "formula": "",
                "crystal_structure": "",
                "space_group": "",
                "lattice_parameters": "",
                "symmetry": "",
                "composition": "",
                "paper_id": p.paper_id,
                "paper_title": title,
                "confidence": 0.5,
                "source_snippet": (abstract or "")[:300],
            })
        return materials, properties, synthesis

    # ===== 覆盖度重抽（仅名称材料二次抽取补全）=====

    def _find_name_only_materials(
        self, store: KnowledgeStore
    ) -> list[tuple[Material, list[str]]]:
        """找出库内「仅名称」材料（无性能无合成），过滤泛称。

        Returns:
            [(Material, [候选来源片段关键词]), ...]，按优先级（有化学式优先）排序。
        """
        from core.knowledge.normalize import is_generic_material_name

        mats = store.list_materials(limit=500)
        props = store.list_material_properties(limit=2000)
        syns = store.list_material_synthesis(limit=2000)
        prop_mats = {p.material_id for p in props}
        syn_mats = {s.material_id for s in syns}

        candidates: list[tuple[Material, list[str]]] = []
        for m in mats:
            if m.material_id in prop_mats or m.material_id in syn_mats:
                continue  # 已有知识，跳过
            if is_generic_material_name(m.name, m.formula):
                continue  # 泛称，重抽无意义
            keywords = [m.name] + ([m.formula] if m.formula and m.formula.lower() != m.name.lower() else [])
            candidates.append((m, keywords))
        # 有化学式（更明确）优先
        candidates.sort(key=lambda x: (0 if x[0].formula else 1, x[0].name))
        return candidates

    def _re_extract_name_only(
        self, store: KnowledgeStore, registry: LLMRegistry, max_materials: int = 15
    ) -> int:
        """对「仅名称」材料做针对性二次抽取，补全性能/合成知识。

        方法：跨论文聚合包含该材料名的摘要片段 → 专门 prompt 只抽取该材料
        的性能与合成 → 落库关联回原材料。
        纯容错：单材料失败跳过，不阻塞。
        Returns:
            补全的知识条数（性能 + 合成）。
        """
        candidates = self._find_name_only_materials(store)
        if not candidates:
            return 0

        # 加载全部论文（用于聚合片段）
        papers = store.list_papers()
        logger.info("覆盖度重抽：%d 个仅名称材料（最多处理 %d）", len(candidates), max_materials)

        n_added = 0
        for mat, keywords in candidates[:max_materials]:
            try:
                kw_lower = [k.lower() for k in keywords if k]
                # 收集包含材料名的论文片段（最多 3 篇，每篇取匹配处前后 400 字符）
                snippets: list[str] = []
                for p in papers:
                    text = f"{p.title or ''}\n{(p.abstract or '')[:2000]}"
                    text_lower = text.lower()
                    hit = next((k for k in kw_lower if k and k in text_lower), None)
                    if not hit:
                        continue
                    idx = text_lower.find(hit)
                    start = max(0, idx - 200)
                    end = min(len(text), idx + 600)
                    snippets.append(f"[{p.title}] …{text[start:end]}…")
                    if len(snippets) >= 3:
                        break
                if not snippets:
                    continue  # 无匹配片段，跳过

                resp = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=MaterialExtractSchema,
                    system=(
                        "你是材料科学知识抽取专家。目标：为指定材料补全缺失的知识。\n"
                        "只抽取文本中明确提到的该材料的性能指标（数值/单位/条件）与"
                        "合成条件（方法/前驱体/温度/压力/气氛/时间/步骤）。\n"
                        "严格只输出目标材料的条目；找不到的信息留空，不要臆造。"
                    ),
                    prompt=(
                        f"目标材料：{mat.name}"
                        + (f"（化学式 {mat.formula}）" if mat.formula else "")
                        + "\n\n相关论文片段：\n"
                        + "\n\n".join(snippets)
                    ),
                )

                # 落库补全（匹配库内该材料）
                for item in resp.materials or []:
                    if item.name.strip().lower() != mat.name.strip().lower():
                        # 别名匹配：化学式一致也算
                        if not (mat.formula and item.formula and
                                mat.formula.lower() == item.formula.lower()):
                            continue
                    for prop in item.properties or []:
                        raw_vn = prop.get("value_num")
                        value_num: Optional[float] = None
                        if raw_vn is not None and str(raw_vn).strip():
                            try:
                                value_num = float(str(raw_vn).strip().rstrip("%"))
                            except ValueError:
                                value_num = None
                        store.save_material_property(MaterialProperty(
                            property_id=KnowledgeStore.new_id(),
                            material_id=mat.material_id,
                            property_name=prop.get("property_name", ""),
                            property_name_cn=prop.get("property_name_cn", ""),
                            value=prop.get("value", ""),
                            value_num=value_num,
                            unit=prop.get("unit", ""),
                            condition=prop.get("condition", ""),
                            paper_id=mat.paper_id,
                            paper_title=mat.paper_title,
                            confidence=0.7,  # 二次抽取置信度略低
                            source_snippet=(snippets[0] or "")[:800],
                            source_stage="research",
                        ))
                        n_added += 1
                    for syn in item.synthesis or []:
                        _raw_pre = syn.get("precursors", []) or []
                        if isinstance(_raw_pre, str):
                            _raw_pre = [_raw_pre]
                        _raw_steps = syn.get("steps", "") or ""
                        if isinstance(_raw_steps, list):
                            _raw_steps = "; ".join(str(x) for x in _raw_steps)
                        store.save_material_synthesis(MaterialSynthesis(
                            synthesis_id=KnowledgeStore.new_id(),
                            material_id=mat.material_id,
                            method=syn.get("method", ""),
                            precursors=list(_raw_pre),
                            temperature=syn.get("temperature", ""),
                            pressure=syn.get("pressure", ""),
                            atmosphere=syn.get("atmosphere", ""),
                            duration=syn.get("duration", ""),
                            steps=_raw_steps,
                            paper_id=mat.paper_id,
                            paper_title=mat.paper_title,
                            confidence=0.7,
                            source_snippet=(snippets[0] or "")[:800],
                            source_stage="research",
                        ))
                        n_added += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("覆盖度重抽失败（material=%r）: %s", mat.name, e)
                continue

        if n_added:
            logger.info("覆盖度重抽完成：补全 %d 条知识", n_added)
        return n_added


# ===== ResearchGapIdentifyAgent（Task 3：研究缺口识别）=====

class ResearchGapItemSchema(BaseModel):
    """研究缺口结构化输出 schema 条目。"""

    gap_type: str = Field(
        description="缺口类型：contradiction（矛盾结论）/ unexplored（未被探索方向）"
        "/ missing_link（缺失知识连接）"
    )
    statement: str = Field(description="一句话陈述（简明，30 字内）")
    detail: str = Field(default="", description="详细说明：现状、为什么是缺口")
    evidence: list[dict] = Field(
        default_factory=list,
        description="证据链：[{paper_id, title, snippet}]，每条缺口至少附 1 条文献证据",
    )
    related_materials: list[str] = Field(default_factory=list, description="关联材料")
    actionability: str = Field(default="medium", description="可操作性：high/medium/low")
    priority: int = Field(default=3, description="优先级 1（最高）~5（最低）")
    suggested_actions: list[str] = Field(default_factory=list, description="建议行动")
    subquery: str = Field(default="", description="关联子问题")


class ResearchGapReportSchema(BaseModel):
    """研究缺口报告 schema。"""

    gaps: list[ResearchGapItemSchema] = Field(
        default_factory=list, description="识别的研究缺口列表（3-10 条，宁缺毋滥）"
    )


class ResearchGapIdentifyAgent(AgentNode):
    """研究缺口识别 Agent（Task 3）。

    在 cross_validate 之后执行，将「子问题粒度的字符串 gaps」升级为
    「结构化 Gap 清单」，双通道识别：

    通道 A（LLM 语义分析）：基于交叉验证报告（gaps/conflicts/consensus）、
        论文摘要与材料知识，识别三类缺口：
        - contradiction：文献间矛盾结论（如同材料性能数值差异大）
        - unexplored：未被探索的研究方向（如某材料高温区数据缺失）
        - missing_link：缺失的知识连接（如性能与结构/工艺间缺关联）

    通道 B（数据驱动断链检测）：KnowledgeStore 材料库统计规则（纯规则，不耗 token）：
        - 有性能无合成工艺的材料（工艺知识断链）
        - 有合成无性能数据的材料（性能数据断链）
        - 孤立材料（无性能无合成，仅名称）
        - 性能类别稀疏（某类性能覆盖材料极少）

    输出：结构化 Gap 清单写入 context（RESEARCH_GAP_REPORT）并落库
    KnowledgeStore（research_gaps 表），每条带证据链（可溯源），
    供 ideation（思路生成）/ discovery（假设种子 gap_ref）/ 调研报告消费。
    """

    node_type = "research_gap_identify"
    task_type = "research_gap_identify"
    input_schema = ResearchGapInput
    output_schema = ResearchGapOutput
    # 结构化清单整体写入 RESEARCH_GAP_REPORT（与 material_extraction 同理）
    output_keys = {}

    # 数据驱动检测规则上限（避免一次生成过多 gap 淹没下游）
    MAX_DATA_DRIVEN_GAPS = 12
    # LLM 语义通道生成上限
    MAX_LLM_GAPS = 10

    def _build_input(self, ctx: ExecutionContext) -> ResearchGapInput:
        return ResearchGapInput(
            paper_ids=ctx.get(RESEARCH_PAPER_IDS, []),
            subqueries=ctx.get(RESEARCH_SUBQUERIES, []),
            cross_validation_report=ctx.get(RESEARCH_CROSS_VALIDATION_REPORT, {}) or {},
        )

    def _execute(
        self, input_obj: ResearchGapInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)

        llm_gaps: list[dict] = []
        data_gaps: list[dict] = []

        # 通道 A：LLM 语义分析（仅真实模式且 LLM 可用）
        if not dry_run and registry is not None:
            try:
                llm_gaps = self._llm_identify(input_obj, registry, store)
            except Exception as e:
                logger.warning("Research Gap LLM 识别失败，回退规则通道: %s", e)

        # 通道 B：数据驱动断链检测（纯规则，dry_run 与真实模式都执行）
        if store is not None:
            try:
                data_gaps = self._data_driven_detect(store)
            except Exception as e:
                logger.warning("Research Gap 数据驱动检测失败: %s", e)

        # 合并去重（同 statement 保留 LLM 版本，标注 hybrid）
        gaps = self._merge_gaps(llm_gaps, data_gaps)

        # dry_run 且无真实数据：生成占位 Gap（验证拓扑用）
        if dry_run and not gaps:
            gaps = self._placeholder_gaps(input_obj.subqueries)

        # Gap 质量量化评分（路线 A：客观指标，让评委/专家一眼可见紧迫度）
        # 基于 KnowledgeStore 真实数据：文献覆盖度、可填补性、行动清晰度、关联强度
        gap_scores: list[dict] = []
        if store is not None and gaps:
            try:
                from core.tools.discovery_metrics import score_gaps
                gap_scores = score_gaps(store, gaps)
                # 把评分挂到每个 Gap 上（前端可视化 + 排序用）
                score_map = {s["gap_id"]: s for s in gap_scores}
                for g in gaps:
                    gid = g.get("gap_id", "")
                    s = score_map.get(gid)
                    if s:
                        g["quality_score"] = s["quality_score"]
                        g["quality_dimensions"] = s["dimensions"]
                        g["quality_reasoning"] = s["reasoning"]
                # 按 quality_score 降序重新排序（高质量 Gap 优先消费）
                gaps.sort(
                    key=lambda g: (
                        -float(g.get("quality_score", 0.5)),
                        int(g.get("priority", 5)),
                    )
                )
            except Exception as e:
                logger.warning("Gap 质量评分失败（降级为 priority 排序）: %s", e)

        # 落库 research_gaps 表（幂等，可跨会话恢复）
        saved = 0
        if store is not None:
            saved = self._persist(store, gaps)

        # 写入 context（供下游 ideation/discovery/报告消费）
        ctx.set(RESEARCH_GAP_REPORT, gaps)

        output = ResearchGapOutput(gaps=gaps)
        by_type: dict[str, int] = {}
        for g in gaps:
            by_type[g["gap_type"]] = by_type.get(g["gap_type"], 0) + 1
        type_brief = ", ".join(f"{k} {v} 个" for k, v in by_type.items()) or "无"

        # 持久化 Gap 评分（前端可视化直接读取）
        if store is not None and gap_scores:
            try:
                # 把 Gap 的可读信息（statement + 关联论文标题）合并进评分，
                # 前端展示用（避免只显示 gap_id 字母串用户看不懂）
                gap_meta = {
                    g.get("gap_id", ""): g
                    for g in gaps
                }
                enriched = []
                for s in gap_scores:
                    s2 = dict(s)
                    g = gap_meta.get(s.get("gap_id", ""), {})
                    s2["statement"] = g.get("statement", "") or g.get("gap", "") or ""
                    evs = g.get("evidence") or []
                    s2["evidence"] = evs
                    s2["paper_titles"] = [
                        e.get("title", "") for e in evs if e.get("title")
                    ][:5]
                    s2["paper_ids"] = [
                        e.get("paper_id", "") for e in evs if e.get("paper_id")
                    ][:5]
                    enriched.append(s2)
                store.save_kv("research_gap_scores", {
                    "version": "v1.0",
                    "scores": enriched,
                    "weights": gap_scores[0]["weights"] if gap_scores else {},
                    "summary": {
                        "total": len(gap_scores),
                        "high_quality": sum(1 for s in gap_scores if s["quality_score"] >= 0.7),
                        "medium_quality": sum(1 for s in gap_scores if 0.5 <= s["quality_score"] < 0.7),
                        "low_quality": sum(1 for s in gap_scores if s["quality_score"] < 0.5),
                        "avg_score": round(
                            sum(s["quality_score"] for s in gap_scores) / max(len(gap_scores), 1), 3
                        ),
                    },
                })
            except Exception as e:
                logger.warning("Gap 评分持久化失败: %s", e)

        score_brief = ""
        if gap_scores:
            avg = sum(s["quality_score"] for s in gap_scores) / max(len(gap_scores), 1)
            high = sum(1 for s in gap_scores if s["quality_score"] >= 0.7)
            score_brief = f"；质量评分 {avg:.2f}（高 ≥0.7：{high} 条）"
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"研究缺口识别完成：共 {len(gaps)} 个 Gap"
                f"（LLM {len(llm_gaps)} + 数据驱动 {len(data_gaps)}，"
                f"落库 {saved} 条；类型分布：{type_brief}"
                f"{score_brief}）"
            ),
        )

    # ===== 通道 A：LLM 语义分析 =====

    def _llm_identify(
        self,
        input_obj: ResearchGapInput,
        registry: LLMRegistry,
        store: Optional[KnowledgeStore],
    ) -> list[dict]:
        """LLM 语义通道：基于交叉验证报告 + 论文摘要 + 材料知识识别缺口。"""
        report = input_obj.cross_validation_report or {}
        # 基础 gaps（cross_validate 输出的子问题缺口）+ 冲突 + 共识
        base_gaps = report.get("gaps") or []
        conflicts = report.get("conflicts") or []
        consensus = report.get("consensus") or []
        confidence = report.get("overall_confidence", 0.0)

        # 论文摘要（top 5）
        abstracts_block = ""
        if store is not None:
            lines = []
            for pid in input_obj.paper_ids[:5]:
                try:
                    p = store.get_paper(pid)
                    lines.append(
                        f"[{p.title}] ({p.year}): {(p.abstract or '')[:200]}"
                    )
                except Exception:
                    pass
            abstracts_block = "\n\n".join(lines)

        resp = registry.structured_output(
            task_type=self.task_type,
            output_schema=ResearchGapReportSchema,
            system=(
                "你是材料科学研究缺口分析专家。基于文献调研的交叉验证报告、"
                "论文摘要与材料知识，识别本领域的研究缺口（Research Gap）。\n"
                "缺口类型：\n"
                "- contradiction：文献间矛盾结论（如同一材料性能数值差异大、"
                "  机理解释不一致）\n"
                "- unexplored：未被探索的方向（如某材料在特定温度区间/掺杂范围"
                "  数据缺失、某工艺组合无人尝试）\n"
                "- missing_link：缺失的知识连接（如性能与晶体结构/合成工艺间"
                "  缺少关联研究、材料有性能但无对应结构数据）\n"
                "要求：\n"
                "1. 每个 Gap 必须能追溯到证据（evidence 至少 1 条，可引用论文标题）\n"
                "2. 宁缺毋滥：只输出真正有依据的缺口，3-10 条\n"
                "3. 给出可操作性（能否被后续 ideation/discovery 直接消费）与优先级\n"
                "4. 关联相关材料与建议行动\n"
            ),
            prompt=(
                f"交叉验证报告：\n"
                f"  总体置信度：{confidence}\n"
                f"  已知缺口（子问题粒度）：{base_gaps}\n"
                f"  矛盾结论：{json.dumps(conflicts, ensure_ascii=False)[:2000]}\n"
                f"  共识陈述：{consensus[:10]}\n\n"
                f"代表性论文摘要：\n{abstracts_block}"
            ),
        )
        gaps: list[dict] = []
        for item in resp.gaps:
            gaps.append({
                "gap_id": KnowledgeStore.new_id(),
                "gap_type": item.gap_type,
                "statement": item.statement,
                "detail": item.detail,
                "evidence": item.evidence or [],
                "related_materials": item.related_materials or [],
                "actionability": item.actionability or "medium",
                "priority": int(item.priority or 3),
                "source": "llm",
                "suggested_actions": item.suggested_actions or [],
                "subquery": item.subquery,
            })
        return gaps[: self.MAX_LLM_GAPS]

    # ===== 通道 B：数据驱动断链检测（纯规则）=====

    def _data_driven_detect(self, store: KnowledgeStore) -> list[dict]:
        """数据驱动断链检测：从材料库统计识别缺失知识连接。"""
        materials = store.list_materials(limit=2000)
        props = store.list_material_properties(limit=5000)
        syns = store.list_material_synthesis(limit=5000)

        # 建立 material_id → 数据索引
        prop_by_mat: dict[str, list] = {}
        for p in props:
            prop_by_mat.setdefault(p.material_id, []).append(p)
        syn_by_mat: dict[str, list] = {}
        for s in syns:
            syn_by_mat.setdefault(s.material_id, []).append(s)

        gaps: list[dict] = []

        # 规则 1：有性能无合成工艺（工艺知识断链）
        no_syn = []
        for m in materials:
            if m.material_id in prop_by_mat and m.material_id not in syn_by_mat:
                no_syn.append(m)
        no_syn.sort(
            key=lambda m: -len(prop_by_mat.get(m.material_id, []))
        )
        for m in no_syn[:5]:
            p = prop_by_mat[m.material_id][0]
            gaps.append(self._make_data_gap(
                gap_type="missing_link",
                statement=f"{m.name} 有性能数据但缺少合成工艺记录",
                detail=(
                    f"材料 {m.name} 已有 {len(prop_by_mat[m.material_id])} 条性能数据"
                    f"（如 {p.property_name}），但库内未检索到其合成工艺"
                    f"（方法/温度/前驱体）记录，构效关系链路在「工艺→结构→性能」"
                    "的源头处断链。"
                ),
                materials=[m],
                actionability="high",
                priority=1,
                suggested_actions=[
                    "补充检索该材料的合成工艺文献",
                    "将其纳入 discovery 搜索空间的前驱体/工艺变量",
                ],
            ))

        # 规则 2：有合成无性能数据（性能数据断链）
        no_prop = []
        for m in materials:
            if m.material_id in syn_by_mat and m.material_id not in prop_by_mat:
                no_prop.append(m)
        no_prop.sort(
            key=lambda m: -len(syn_by_mat.get(m.material_id, []))
        )
        for m in no_prop[:4]:
            gaps.append(self._make_data_gap(
                gap_type="missing_link",
                statement=f"{m.name} 有合成工艺但无性能数据",
                detail=(
                    f"材料 {m.name} 已报道合成方法"
                    f"（{syn_by_mat[m.material_id][0].method}），"
                    "但缺少性能（ZT/电导率/热导率等）数据，"
                    "无法评估其热电潜力，存在性能数据缺口。"
                ),
                materials=[m],
                actionability="high",
                priority=2,
                suggested_actions=[
                    "检索该材料的热电性能报道",
                    "用相似结构材料做性能外推作为初始假设",
                ],
            ))

        # 规则 3：孤立材料（无性能无合成，仅名称）
        isolated = [
            m for m in materials
            if m.material_id not in prop_by_mat
            and m.material_id not in syn_by_mat
        ]
        for m in isolated[:2]:
            gaps.append(self._make_data_gap(
                gap_type="unexplored",
                statement=f"{m.name} 仅被提及名称，缺乏结构化知识",
                detail=(
                    f"材料 {m.name} 在文献中被提及但未抽取到性能与合成信息，"
                    "可能是边缘提及或抽取遗漏，需要人工/补充检索确认其研究价值。"
                ),
                materials=[m],
                actionability="low",
                priority=4,
                suggested_actions=[
                    "回读原文确认该材料的研究地位",
                    "若为边缘提及则标注低优先，避免误导下游",
                ],
            ))

        return gaps[: self.MAX_DATA_DRIVEN_GAPS]

    @staticmethod
    def _make_data_gap(
        gap_type: str,
        statement: str,
        detail: str,
        materials: list,
        actionability: str,
        priority: int,
        suggested_actions: list[str],
    ) -> dict:
        """构造数据驱动 Gap dict（evidence 取自首个材料）。"""
        m = materials[0]
        evidence = []
        if getattr(m, "paper_id", None):
            evidence.append({
                "paper_id": m.paper_id,
                "title": getattr(m, "paper_title", "") or "",
                "snippet": (getattr(m, "source_snippet", "") or "")[:300],
            })
        return {
            "gap_id": KnowledgeStore.new_id(),
            "gap_type": gap_type,
            "statement": statement,
            "detail": detail,
            "evidence": evidence,
            "related_materials": [m.name],
            "actionability": actionability,
            "priority": priority,
            "source": "data_driven",
            "suggested_actions": suggested_actions,
            "subquery": "",
        }

    # ===== 合并 / 占位 / 落库 =====

    @staticmethod
    def _merge_gaps(llm_gaps: list[dict], data_gaps: list[dict]) -> list[dict]:
        """合并双通道结果：同 statement 去重（保留 LLM 版，标注 hybrid）。"""
        seen: set[str] = set()
        merged: list[dict] = []
        for g in llm_gaps:
            key = (g.get("statement") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(g)
        for g in data_gaps:
            key = (g.get("statement") or "").strip().lower()
            if key in seen:
                # 已存在（LLM 版）：把 LLM 版标记为 hybrid
                for m in merged:
                    if (m.get("statement") or "").strip().lower() == key:
                        if m.get("source") == "llm":
                            m["source"] = "hybrid"
                        break
                continue
            seen.add(key)
            merged.append(g)
        # 按优先级排序（1 最高在前）
        merged.sort(key=lambda g: g.get("priority", 5))
        return merged

    @staticmethod
    def _placeholder_gaps(subqueries: list[str]) -> list[dict]:
        """dry_run 占位 Gap（验证拓扑用）。"""
        sq = subqueries[0] if subqueries else "研究主题"
        return [{
            "gap_id": KnowledgeStore.new_id(),
            "gap_type": "unexplored",
            "statement": f"[占位] {sq[:40]} 存在未被探索的研究方向",
            "detail": "dry_run 占位缺口，真实模式将由 LLM + 数据驱动双通道生成",
            "evidence": [],
            "related_materials": [],
            "actionability": "medium",
            "priority": 3,
            "source": "placeholder",
            "suggested_actions": [],
            "subquery": sq,
        }]

    @staticmethod
    def _persist(store: KnowledgeStore, gaps: list[dict]) -> int:
        """落库 research_gaps 表，返回成功条数。"""
        objs: list[ResearchGap] = []
        for g in gaps:
            try:
                objs.append(ResearchGap(
                    gap_id=g.get("gap_id") or KnowledgeStore.new_id(),
                    gap_type=g.get("gap_type", "unexplored"),
                    statement=g.get("statement", ""),
                    detail=g.get("detail", ""),
                    evidence=g.get("evidence") or [],
                    related_materials=g.get("related_materials") or [],
                    actionability=g.get("actionability", "medium"),
                    priority=int(g.get("priority", 3)),
                    source=g.get("source", "llm"),
                    suggested_actions=g.get("suggested_actions") or [],
                    subquery=g.get("subquery", ""),
                ))
            except Exception as e:  # noqa: BLE001
                logger.warning("Research Gap 构造失败: %s", e)
        return store.save_research_gaps(objs)
