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

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from pydantic import BaseModel, Field

from core.knowledge import KnowledgeStore, Paper, PaperChunk
from core.llm import LLMRegistry
from core.orchestration.context import ExecutionContext
from core.orchestration.node import (
    AgentNode,
    HumanNode,
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
from core.tools.url_resolve import resolve_paper_url

from stages.common import (
    DRY_RUN,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    RESEARCH_CROSS_VALIDATION_REPORT,
    RESEARCH_EVIDENCE_CHAIN,
    RESEARCH_FILTERED_PAPER_METAS,
    RESEARCH_KEYWORDS,
    RESEARCH_PAPER_IDS,
    RESEARCH_PAPER_METAS,
    RESEARCH_QUERY_STRATEGY,
    RESEARCH_SUBQUERIES,
    RESEARCH_TOPIC,
    RESEARCH_TOPIC_CONFIRMED,
)
from stages.research.io_schema import (
    CrossValidateInput,
    CrossValidateOutput,
    PaperFetchInput,
    PaperFetchOutput,
    PaperIngestInput,
    PaperIngestOutput,
    PaperRelevanceFilterInput,
    PaperRelevanceFilterOutput,
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


class ConflictReportSchema(BaseModel):
    """交叉验证报告 schema。"""

    conflicts: list[ConflictItem] = []
    consensus: list[str] = []
    gaps: list[str] = []
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
            "  - 或输入 'subq: <新子问题1> | <新子问题2> | ...' 替换子问题列表"
        )

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        text = (response.text or "").strip()
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

class PaperFetchAgent(AgentNode):
    """论文抓取 Agent。

    根据子问题并行检索文献。数据源策略（赛题推荐的证据链设计）：
    - Sciverse 为主源（agentic-search 返回片段级证据，含 doc_id + offset，
      调用记录天然构成可审计证据链，也是赛题手册明确推荐的资源）
    - arxiv / Semantic Scholar 为补充（最新预印本、引用图谱与影响力）
    每个子问题独立检索，结果带 source_subquery 标记；每次命中同时写入
    evidence_chain（审计轨迹），由 PaperIngestAgent 落库并关联 paper_id。
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

    def _build_input(self, ctx: ExecutionContext) -> PaperFetchInput:
        return PaperFetchInput(
            keywords=ctx.get(RESEARCH_KEYWORDS, []),
            query_strategy=ctx.get(RESEARCH_QUERY_STRATEGY, ""),
            subqueries=ctx.get(RESEARCH_SUBQUERIES, []),
        )

    def _execute(self, input_obj: PaperFetchInput, ctx: ExecutionContext) -> NodeResult:
        dry_run: bool = ctx.get(DRY_RUN, True)

        if dry_run:
            paper_metas = self._placeholder(input_obj)
            evidence_chain: list[dict] = []
        else:
            paper_metas, evidence_chain = self._real_fetch(input_obj)

        # 去重（按 arxiv_id 优先，其次 title）
        paper_metas = self._dedup(paper_metas)

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

            # ===== 1. Sciverse 主源：证据片段级检索（赛题推荐，可审计证据链）=====
            if sciverse_is_available():
                try:
                    evidences = sciverse_agentic_search(
                        query=sq,
                        max_results=self.SCIVERSE_PER_SUBQUERY,
                        source_subquery=sq,
                    )
                    for ev in evidences:
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

            # ===== 2. arxiv 补充（最新预印本，含 abstract 全文）=====
            try:
                arxiv_papers = search_arxiv(
                    query=sq,
                    max_results=self.DEFAULT_PER_SUBQUERY,
                    source_subquery=sq,
                )
                for p in arxiv_papers:
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

            # ===== 3. S2 补充（引用图谱/venue/影响力，限 2 篇避免重复）=====
            try:
                s2_papers = search_semantic_scholar(
                    query=sq,
                    max_results=self.S2_PER_SUBQUERY,
                    source_subquery=sq,
                )
                for p in s2_papers:
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

                # 关联证据链：命中该论文的链条目标记 paper_id 并落库
                external_id = (meta.get("doc_id") or "").strip() or \
                    (meta.get("arxiv_id") or "").strip()
                title_key = (meta.get("title") or "").strip().lower()
                matched: list[dict] = []
                for e in evidence_chain:
                    eid = (e.get("external_id") or "").strip()
                    etitle = (e.get("title") or "").strip().lower()
                    if external_id and eid and eid == external_id:
                        matched.append(e)
                    elif not external_id and etitle and etitle == title_key:
                        matched.append(e)
                for e in matched:
                    e["paper_id"] = paper_id
                    store.log_evidence(e)
                    evidence_chain.remove(e)
                    linked_count += 1
            except Exception as e:
                logger.warning("论文入库失败（title=%r）: %s", meta.get("title"), e)
                continue

        # 未关联到入库论文的链条目（检索命中但被筛选/去重剔除）也落库，
        # 保留完整审计轨迹：每个子问题调用了哪些源、命中了哪些证据、最终是否入库
        unmatched = 0
        for e in evidence_chain:
            try:
                store.log_evidence(e)
                unmatched += 1
            except Exception as err:
                logger.warning("证据链落库失败: %s", err)

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
        """真实交叉验证：聚合每子问题相关论文，调用 LLM 检测冲突。"""
        # 加载所有论文
        papers: list[Paper] = []
        for pid in input_obj.paper_ids:
            try:
                papers.append(store.get_paper(pid))
            except Exception:
                pass

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
        all_consensus: list[str] = []
        gaps: list[str] = []

        for sq, sq_papers in sub_papers.items():
            if not sq_papers:
                gaps.append(sq)
                continue

            # 拼接摘要
            abstracts_block = "\n\n".join(
                f"[{p.title}] ({p.year}): {(p.abstract or '')[:300]}"
                for p in sq_papers[:5]  # 限制 token 量
            )

            try:
                resp = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=ConflictReportSchema,
                    system=(
                        "你是科研调研助手。对以下多源信息检测冲突与共识，"
                        "给出处置建议与可信度评分。"
                        "冲突：明确陈述相反的论断；共识：多方一致认同的陈述；"
                        "gaps：证据不足的子问题。overall_confidence: 0~1。"
                    ),
                    prompt=(
                        f"子问题：{sq}\n"
                        f"相关论文摘要：\n{abstracts_block}"
                    ),
                )
                # 合并报告
                for c in resp.conflicts:
                    all_conflicts.append({
                        "claim": c.claim,
                        "sources": c.sources,
                        "resolution": c.resolution,
                        "confidence": c.confidence,
                        "subquery": sq,
                    })
                all_consensus.extend([f"[{sq}] {s}" for s in resp.consensus])
                if not resp.consensus and not resp.conflicts:
                    gaps.append(sq)
            except Exception as e:
                logger.warning("交叉验证失败（sq=%r）: %s", sq, e)
                gaps.append(sq)

        # 综合可信度：冲突越少 / 共识越多 / 缺口越少 → 越高
        n_sq = max(len(input_obj.subqueries), 1)
        gap_ratio = len(gaps) / n_sq
        conflict_ratio = len(all_conflicts) / max(n_sq * 2, 1)
        overall = max(0.0, 1.0 - gap_ratio * 0.5 - conflict_ratio * 0.3)

        return {
            "conflicts": all_conflicts,
            "consensus": all_consensus,
            "gaps": gaps,
            "overall_confidence": round(overall, 3),
        }

    @staticmethod
    def _placeholder(input_obj: CrossValidateInput) -> dict:
        return {
            "conflicts": [],
            "consensus": [f"{sq}: 占位共识陈述" for sq in input_obj.subqueries],
            "gaps": [],
            "overall_confidence": 0.8,
        }
