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

说明：_execute 内 LLM 调用以完整注释范式给出，实际执行用占位数据返回，
既能验证 IO 闭环，又不会产生 API 费用。
"""
from __future__ import annotations

from typing import Optional

from core.knowledge import KnowledgeStore, Paper
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

from stages.common import (
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    RESEARCH_CROSS_VALIDATION_REPORT,
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

        # === LLM 调用范式（占位，实际未执行）===
        # resp = registry.complete(
        #     task_type=self.task_type,
        #     prompt=(
        #         f"研究主题：{input_obj.topic}\n"
        #         "请生成 5-10 个检索关键词，并给出 arxiv/S2 的查询策略。"
        #     ),
        # )
        # keywords, query_strategy = parse_literature_search(resp.text)

        # 占位数据
        keywords = [input_obj.topic, f"{input_obj.topic} survey", f"{input_obj.topic} benchmark"]
        query_strategy = f"arxiv:all:{input_obj.topic} AND (survey OR benchmark)"

        output = TopicRefineOutput(keywords=keywords, query_strategy=query_strategy)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"主题精炼完成，生成 {len(keywords)} 个关键词",
        )


# ===== SubqueryDecomposeAgent（借鉴 GPT-Researcher）=====

class SubqueryDecomposeAgent(AgentNode):
    """子问题分解 Agent。

    借鉴 GPT-Researcher 的 Planner：把研究主题拆为 5-10 个子问题，
    每个子问题覆盖主题的不同侧面（动机/方法/数据/评估/局限/扩展），
    便于后续并行检索与多源信息聚合。

    设计要点：
    - 子问题应当互相正交，避免检索结果高度重叠
    - 每个子问题对应一个检索意图（intent），便于 fetch 阶段选择数据源
      （arxiv 偏方法、S2 偏引用图谱、web 偏综述）
    """

    node_type = "research_subquery_decompose"
    task_type = "research_subquery_decompose"
    input_schema = SubqueryDecomposeInput
    output_schema = SubqueryDecomposeOutput
    output_keys = {
        "subqueries": RESEARCH_SUBQUERIES,
        # intents 不写回 context（仅供 fetch 阶段在内存中使用，避免污染域键）
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

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 GPT-Researcher：让 LLM 输出结构化的子问题列表
        # from core.llm.base import StructuredOutputRequest
        # class SubquerySchema(BaseModel):
        #     subqueries: list[str]
        #     intents: list[str]
        # result = registry.structured_output(
        #     task_type=self.task_type,
        #     output_schema=SubquerySchema,
        #     system=(
        #         "你是科研调研助手。把研究主题拆为 5-10 个互相正交的子问题，"
        #         "覆盖：动机、已有方法、数据集、评估指标、关键局限、潜在扩展。"
        #         "每个子问题给出检索意图（arxiv/s2/web）。"
        #     ),
        #     prompt=(
        #         f"主题：{input_obj.topic}\n"
        #         f"关键词：{input_obj.keywords}\n"
        #         f"查询策略：{input_obj.query_strategy}"
        #     ),
        # )
        # subqueries = result.subqueries
        # intents = result.intents

        # 占位数据：覆盖主题的 6 个正交侧面
        subqueries = [
            f"{input_obj.topic} 的核心动机与痛点是什么？",
            f"{input_obj.topic} 已有方法分哪几类？各自的代表工作？",
            f"{input_obj.topic} 常用数据集与评估指标？",
            f"{input_obj.topic} 当前 SOTA 方法的关键创新？",
            f"{input_obj.topic} 现有方法的主要局限？",
            f"{input_obj.topic} 未来可能的研究方向？",
        ]
        intents = ["web", "arxiv", "arxiv", "arxiv", "s2", "web"]

        output = SubqueryDecomposeOutput(subqueries=subqueries, intents=intents)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"子问题分解完成，生成 {len(subqueries)} 个子问题",
        )


# ===== TopicConfirmHuman =====

class TopicConfirmHuman(HumanNode):
    """向用户确认检索方向。

    呈现生成的关键词、查询策略与子问题列表，用户可确认或修正。
    借鉴 GPT-Researcher：在并行检索前给用户最后干预机会，避免检索方向跑偏。
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
            # 替换子问题列表
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

    根据子问题并行检索 arxiv/S2（占位），调用 paper_metadata_extract 抽取元数据。
    借鉴 GPT-Researcher：每个子问题独立检索，结果带 source_subquery 标记，
    便于后续交叉验证时溯源到具体子问题。
    """

    node_type = "research_paper_fetch"
    task_type = "paper_metadata_extract"
    input_schema = PaperFetchInput
    output_schema = PaperFetchOutput
    output_keys = {
        "paper_metas": RESEARCH_PAPER_METAS,
    }

    def _build_input(self, ctx: ExecutionContext) -> PaperFetchInput:
        return PaperFetchInput(
            keywords=ctx.get(RESEARCH_KEYWORDS, []),
            query_strategy=ctx.get(RESEARCH_QUERY_STRATEGY, ""),
            subqueries=ctx.get(RESEARCH_SUBQUERIES, []),
        )

    def _execute(self, input_obj: PaperFetchInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 GPT-Researcher：按子问题并行检索，每条候选带 source_subquery
        # from concurrent.futures import ThreadPoolExecutor
        # def fetch_one(subquery: str) -> list[dict]:
        #     # 1. 调用 arxiv/S2 API 获取候选列表
        #     candidates = arxiv_search(subquery) + s2_search(subquery)
        #     # 2. 对每个候选抽取结构化元数据
        #     out = []
        #     for cand in candidates:
        #         resp = registry.structured_output(
        #             task_type=self.task_type,
        #             output_schema=PaperMetaSchema,
        #             prompt=f"从以下内容抽取论文元数据：\n{cand.abstract}",
        #         )
        #         meta = resp.model_dump()
        #         meta["source_subquery"] = subquery
        #         out.append(meta)
        #     return out
        # with ThreadPoolExecutor(max_workers=4) as pool:
        #     results = list(pool.map(fetch_one, input_obj.subqueries))
        # paper_metas = [m for sub in results for m in sub]

        # 占位数据：每个子问题返回 2 篇候选
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

        output = PaperFetchOutput(paper_metas=paper_metas)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"抓取 {len(paper_metas)} 篇候选论文元数据（来自 {len(sq_list)} 个子问题）",
        )


# ===== PaperRelevanceFilterAgent（借鉴 PaperQA filter）=====

class PaperRelevanceFilterAgent(AgentNode):
    """论文相关性筛选 Agent。

    借鉴 PaperQA 的工具化 RAG filter 环节：对候选论文做相关性打分（0~1），
    过滤低相关性候选，保留高相关性候选进入入库环节。

    设计要点：
    - 打分维度：主题相关性、子问题覆盖度、发表年份、引用影响力
    - 阈值默认 0.5，可由用户在 TopicConfirmHuman 阶段调整
    - 被剔除的候选保留 reason，便于人工追溯（避免误删关键工作）
    """

    node_type = "research_paper_relevance_filter"
    task_type = "paper_relevance_filter"
    input_schema = PaperRelevanceFilterInput
    output_schema = PaperRelevanceFilterOutput
    output_keys = {
        "filtered_paper_metas": RESEARCH_FILTERED_PAPER_METAS,
    }

    DEFAULT_THRESHOLD = 0.5

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

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 PaperQA：让 LLM 对每篇候选打相关性分数并给出理由
        # from core.llm.base import StructuredOutputRequest
        # class RelevanceScore(BaseModel):
        #     score: float  # 0~1
        #     reason: str
        #     covered_subqueries: list[str]
        # filtered, rejected = [], []
        # for meta in input_obj.paper_metas:
        #     resp = registry.structured_output(
        #         task_type=self.task_type,
        #         output_schema=RelevanceScore,
        #         system="你是文献筛选助手。对候选论文按主题相关性与子问题覆盖度打分。",
        #         prompt=(
        #             f"主题：{input_obj.topic}\n"
        #             f"子问题：{input_obj.subqueries}\n"
        #             f"候选论文：{meta}"
        #         ),
        #     )
        #     meta["relevance_score"] = resp.score
        #     meta["relevance_reason"] = resp.reason
        #     (filtered if resp.score >= self.DEFAULT_THRESHOLD else rejected).append(meta)

        # 占位数据：保留全部候选，分数 0.7
        filtered = []
        rejected = []
        for meta in input_obj.paper_metas:
            meta = dict(meta)
            meta["relevance_score"] = 0.7
            meta["relevance_reason"] = "占位：保留候选"
            filtered.append(meta)

        output = PaperRelevanceFilterOutput(
            filtered_paper_metas=filtered,
            rejected=rejected,
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"相关性筛选完成：保留 {len(filtered)} 篇，剔除 {len(rejected)} 篇",
        )


# ===== PaperIngestAgent =====

class PaperIngestAgent(AgentNode):
    """论文入库 Agent。

    将 Paper 与 chunk 入库 KnowledgeStore + VectorStore。
    借鉴 PaperQA：chunk 摘要同时入库（便于检索增强时返回摘要而非原文）。
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

        # === 实际入库范式（占位，实际未执行）===
        # from core.knowledge import PaperChunk, KnowledgeStore
        # from core.knowledge.vector_store import VectorRecord
        # from core.llm import LLMRegistry
        # registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        # paper_ids = []
        # for meta in input_obj.paper_metas:
        #     paper_id = KnowledgeStore.new_id()
        #     paper = Paper(
        #         paper_id=paper_id,
        #         title=meta["title"],
        #         authors=meta.get("authors", []),
        #         year=meta.get("year"),
        #         abstract=meta.get("abstract"),
        #         arxiv_id=meta.get("arxiv_id"),
        #     )
        #     store.save_paper(paper)
        #     # 切分 chunk（按段落，每 chunk ~500 token）
        #     raw_chunks = split_into_chunks(paper.abstract or "", max_tokens=500)
        #     # 借鉴 PaperQA：对每个 chunk 生成摘要，摘要也入库向量库
        #     chunk_objs = []
        #     vector_records = []
        #     for idx, text in enumerate(raw_chunks):
        #         chunk_id = KnowledgeStore.new_id()
        #         # 调用 paper_chunk_summarize 生成摘要
        #         resp = registry.complete(
        #             task_type=self.task_type,
        #             prompt=f"用一句话概括以下段落的核心论点：\n{text}",
        #         )
        #         summary = resp.text
        #         chunk_objs.append(PaperChunk(
        #             chunk_id=chunk_id, paper_id=paper_id,
        #             chunk_index=idx, text=text,
        #         ))
        #         # 摘要作为向量入库（检索时返回摘要，更精准）
        #         emb = registry.embed([summary]).embeddings[0]
        #         vector_records.append(VectorRecord(
        #             chunk_id=chunk_id, paper_id=paper_id,
        #             text=summary, embedding=emb,
        #             metadata={"orig_chunk_text": text},
        #         ))
        #     store.save_paper_chunks(chunk_objs)
        #     vector_store.add(vector_records)
        #     paper_ids.append(paper_id)

        # 占位数据：用 new_id 生成合法 ID（静态方法，无需 DB 实例）
        paper_ids = [KnowledgeStore.new_id() for _ in input_obj.paper_metas]

        output = PaperIngestOutput(paper_ids=paper_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"入库 {len(paper_ids)} 篇论文（含 chunk 摘要向量）",
        )


# ===== CrossValidateAgent（借鉴 GPT-Researcher）=====

class CrossValidateAgent(AgentNode):
    """多源交叉验证 Agent。

    借鉴 GPT-Researcher 的交叉验证环节：对入库 chunk 做多源信息冲突检测，
    输出可信度报告，标注冲突点、共识点、证据缺口。

    设计要点：
    - 对每个子问题，聚合相关 chunk，检测陈述冲突（如方法 A 优于 B vs B 优于 A）
    - 冲突时给出处置建议：采纳来源（高引用/新近）/标记存疑/需进一步检索
    - 输出 overall_confidence，低于阈值时建议回滚到检索阶段补充证据
    - 报告写入 context，供 ideation 阶段读取（思路探讨需基于可信证据）
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

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 GPT-Researcher：让 LLM 对每个子问题聚合 chunk，检测冲突
        # from core.knowledge import Retriever
        # retriever: Optional[Retriever] = ctx.get(RESEARCH_RETRIEVER)
        # report = {"conflicts": [], "consensus": [], "gaps": [], "overall_confidence": 0.0}
        # for sq in input_obj.subqueries:
        #     # 1. 检索该子问题相关 chunk
        #     chunks = retriever.search_papers(sq, top_k=10)
        #     # 2. 让 LLM 检测冲突与共识
        #     resp = registry.structured_output(
        #         task_type=self.task_type,
        #         output_schema=ConflictReportSchema,
        #         system=(
        #             "你是科研调研助手。对以下多源信息检测冲突与共识，"
        #             "给出处置建议与可信度评分。"
        #         ),
        #         prompt=f"子问题：{sq}\n相关 chunk：\n" + "\n".join(c.text for c in chunks),
        #     )
        #     report["conflicts"].extend(resp.conflicts)
        #     report["consensus"].extend(resp.consensus)
        #     if resp.has_gap:
        #         report["gaps"].append(sq)
        # report["overall_confidence"] = compute_overall_confidence(report)

        # 占位数据
        report = {
            "conflicts": [],
            "consensus": [f"{sq}: 占位共识陈述" for sq in input_obj.subqueries],
            "gaps": [],
            "overall_confidence": 0.8,
        }

        output = CrossValidateOutput(report=report)
        summary = (
            f"交叉验证完成：overall_confidence={report['overall_confidence']:.2f}，"
            f"冲突 {len(report['conflicts'])} 处，共识 {len(report['consensus'])} 条，"
            f"缺口 {len(report['gaps'])} 个"
        )
        # 若可信度低于阈值，提示回滚
        if report["overall_confidence"] < self.DEFAULT_CONFIDENCE_THRESHOLD:
            summary += f"（低于阈值 {self.DEFAULT_CONFIDENCE_THRESHOLD}，建议回滚补充检索）"

        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=summary,
        )
