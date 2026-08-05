"""topic_discovery 阶段节点 IO schema 定义。

方向推荐流程：
    用户研究兴趣
    → 趋势数据获取（arXiv API + 关键词频率统计）
    → 趋势分析（增长率计算 + 新兴/稳定/饱和分类）
    → 主题推荐（LLM 生成推荐主题 + 解释）
    → 用户选择（写入 RESEARCH_TOPIC，接入原有 research 流程）
"""
from __future__ import annotations

from typing import Any

from core.orchestration.node import NodeInput, NodeOutput


# ===== TrendFetchAgent =====

class TrendFetchInput(NodeInput):
    """趋势数据获取输入。"""

    # 用户研究兴趣/领域关键词（如 "thermoelectric materials"）
    interest: str


class TrendFetchOutput(NodeOutput):
    """趋势数据获取输出。

    trend_data 结构：
    {
        "keyword_frequencies": {keyword: {year: count}},
        "total_papers_by_year": {year: count},
        "sample_papers": [list of paper meta dicts],
        "query": str,
        "total_fetched": int,
    }
    """

    trend_data: dict[str, Any]


# ===== TrendAnalysisAgent =====

class TrendAnalysisInput(NodeInput):
    """趋势分析输入。"""

    trend_data: dict[str, Any]


class TrendAnalysisOutput(NodeOutput):
    """趋势分析输出。

    analysis 结构：
    {
        "emerging": [{"keyword": ..., "growth_rate": ..., "total_count": ..., "trend": [...]}],
        "stable": [...],
        "saturated": [...],
        "all_keywords": [...],
    }
    """

    analysis: dict[str, Any]


# ===== TopicRecommendAgent =====

class TopicRecommendInput(NodeInput):
    """主题推荐输入。"""

    interest: str
    trend_data: dict[str, Any]
    analysis: dict[str, Any]


class TopicRecommendOutput(NodeOutput):
    """主题推荐输出。

    recommendations 结构（list of dict，按 popularity_score 降序）：
    [
        {
            "topic": "基于高熵合金化的Bi₂Te₃热电材料ZT值突破研究",
            "rationale": "该方向增长率 120%，目前仅 3 篇论文...",
            "innovation_point": "使用机器学习辅助筛选高熵合金元素组合",
            "recommended_materials": ["Bi2Te3", "Sb2Te3", "高熵合金化体系"],
            "trend_summary": "halide perovskite 增长 180%，machine learning 增长 150%",
            "difficulty": "medium",
            "novelty": "high",
            "relevance": "high",          # 与用户兴趣关联度 low/medium/high
            "popularity_score": 88,       # 热门度 0-100
            "growth_rate": 1.2,           # 年度增长率（浮点）
        },
        ...
    ]
    """

    recommendations: list[dict[str, Any]]


# ===== TopicSelectHuman =====

class TopicSelectOutput(NodeOutput):
    """用户选择推荐主题后的输出。"""

    # 用户选择的主题（写入 RESEARCH_TOPIC）
    selected_topic: str
    # 是否确认
    confirmed: bool = False
