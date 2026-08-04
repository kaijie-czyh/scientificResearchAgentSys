"""research 阶段节点 IO schema 定义。

调研阶段全流程（借鉴 PaperQA + GPT-Researcher）：
    用户给定研究主题
    → 主题精炼（生成关键词与查询策略）
    → 子问题分解（GPT-Researcher：拆为 5-10 个子问题用于并行检索）
    → 用户确认
    → 论文抓取（按子问题并行检索 arxiv/S2）
    → 相关性筛选（PaperQA filter：打分+筛选）
    → 论文入库（chunk 摘要 + 向量入库）
    → 交叉验证（GPT-Researcher：多源冲突可信度评分）
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.orchestration.node import NodeInput, NodeOutput


# ===== TopicRefineAgent =====

class TopicRefineInput(NodeInput):
    """主题精炼输入。"""

    topic: str


class TopicRefineOutput(NodeOutput):
    """主题精炼输出：检索关键词与查询策略。"""

    keywords: list[str]
    query_strategy: str


# ===== SubqueryDecomposeAgent（借鉴 GPT-Researcher）=====

class SubqueryDecomposeInput(NodeInput):
    """子问题分解输入。

    借鉴 GPT-Researcher：把研究主题拆为多个子问题，便于并行检索与信息聚合。
    """

    topic: str
    keywords: list[str]
    query_strategy: str = ""


class SubqueryDecomposeOutput(NodeOutput):
    """子问题分解输出：5-10 个子问题。

    每个子问题应当是可独立检索的、覆盖主题不同侧面的。
    """

    subqueries: list[str]
    # 子问题对应的检索意图（便于 fetch 阶段选择数据源）
    intents: list[str] = []


# ===== TopicConfirmHuman =====

class TopicConfirmOutput(NodeOutput):
    """用户确认检索方向后的输出。"""

    confirmed: bool
    refined_topic: str
    # 用户可修订子问题列表
    refined_subqueries: list[str] = []


# ===== PaperFetchAgent =====

class PaperFetchInput(NodeInput):
    """论文抓取输入。"""

    keywords: list[str]
    query_strategy: str = ""
    subqueries: list[str] = []


class PaperFetchOutput(NodeOutput):
    """论文抓取输出：论文元数据列表。

    每条元数据是 dict，含 title/authors/year/abstract/arxiv_id/source_subquery 等字段。
    source_subquery 标记该候选来自哪个子问题的检索（便于后续交叉验证）。
    """

    paper_metas: list[dict[str, Any]]


# ===== PaperRelevanceFilterAgent（借鉴 PaperQA filter）=====

class PaperRelevanceFilterInput(NodeInput):
    """相关性筛选输入。

    借鉴 PaperQA：对候选论文做相关性打分与筛选，过滤低质量/离题候选。
    """

    topic: str
    subqueries: list[str]
    paper_metas: list[dict[str, Any]]


class PaperRelevanceFilterOutput(NodeOutput):
    """相关性筛选输出：保留高相关性候选，附相关性分数与理由。

    每条带 relevance_score（0~1）与 reason。
    """

    filtered_paper_metas: list[dict[str, Any]]
    # 被剔除的候选（含 reason，便于人工追溯）
    rejected: list[dict[str, Any]] = []


# ===== PaperIngestAgent =====

class PaperIngestInput(NodeInput):
    """论文入库输入。"""

    paper_metas: list[dict[str, Any]]


class PaperIngestOutput(NodeOutput):
    """论文入库输出：入库后的 paper_id 列表。"""

    paper_ids: list[str]


class MaterialKnowledgeSchema(BaseModel):
    """结构化材料知识抽取 schema（赛题基本任务要求）。

    赛题明确要求从文献中提取：
    - 材料成分、结构、性能、模拟方法、合成条件等结构化信息
    """

    compositions: list[str] = Field(default_factory=list, description="材料化学成分/化学式（如 Bi2Te3, SnSe）")
    crystal_structures: list[str] = Field(default_factory=list, description="晶体结构/相结构（如 六方晶系, 单斜, 钙钛矿）")
    properties: list[dict[str, Any]] = Field(
        default_factory=list,
        description="性能指标（如 {name: 'ZT', value: 1.4, unit: '', temperature_K: 373, conditions: '...'}）",
    )
    synthesis_methods: list[str] = Field(default_factory=list, description="合成方法（如 固相反应, 水热法, 机械合金化）")
    synthesis_conditions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="合成条件（如 {temperature_K: 723, time_h: 12, pressure: '1 atm', atmosphere: 'Ar'}）",
    )
    measurement_techniques: list[str] = Field(default_factory=list, description="表征方法（如 XRD, SEM, Hall 测量, DFT 计算）")
    key_findings: list[str] = Field(default_factory=list, description="关键发现/结论摘要")


# ===== CrossValidateAgent（借鉴 GPT-Researcher）=====

class CrossValidateInput(NodeInput):
    """交叉验证输入。

    借鉴 GPT-Researcher：对入库 chunk 做多源信息冲突检测与交叉验证，
    输出可信度评分与冲突处置建议。
    """

    paper_ids: list[str]
    subqueries: list[str]


class CrossValidateOutput(NodeOutput):
    """交叉验证输出：可信度报告。

    report 结构：
    {
        "conflicts": [
            {
                "claim": "冲突陈述",
                "sources": [{"paper_id": "...", "chunk_id": "...", "stance": "support/refute"}, ...],
                "resolution": "采纳来源/标记存疑/需进一步检索",
                "confidence": 0.0~1.0,
            },
            ...
        ],
        "consensus": ["多方一致认同的陈述", ...],
        "gaps": ["缺乏证据的子问题", ...],
        "overall_confidence": 0.0~1.0,
    }
    """

    report: dict[str, Any]
