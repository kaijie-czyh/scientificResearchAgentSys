"""topic_discovery 阶段子图构建（方向推荐）。

在用户给定主题之前，主动分析领域趋势、发现新兴方向、推荐研究主题。
作为 research 阶段的可选前置入口，不修改原有流程。

拓扑：
    TrendFetchAgent（获取论文数据 + 关键词频率统计）
    → TrendAnalysisAgent（增长率计算 + 新兴/稳定/饱和分类）
    → TopicRecommendAgent（LLM 生成 3-5 个推荐主题）
    → TopicSelectHuman（用户选择主题，写入 RESEARCH_TOPIC）
"""
from __future__ import annotations

from core.orchestration.graph import Graph

from stages.topic_discovery.agents import (
    TopicRecommendAgent,
    TopicSelectHuman,
    TrendAnalysisAgent,
    TrendFetchAgent,
)


def build_topic_discovery_graph() -> Graph:
    """构建方向推荐阶段子图。

    不属于标准 5 阶段生命周期，作为 research 之前的可选前置入口。
    类似 discovery 子图的处理方式：由 Pipeline.run_topic_discovery() 调用。
    """
    graph = Graph(name="topic_discovery", stage="topic_discovery")

    # 节点（按拓扑序）
    graph.add_node(TrendFetchAgent("trend_fetch"))
    graph.add_node(TrendAnalysisAgent("trend_analysis"))
    graph.add_node(TopicRecommendAgent("topic_recommend"))
    graph.add_node(TopicSelectHuman("topic_select"))

    # 边
    graph.add_edge("trend_fetch", "trend_analysis")
    graph.add_edge("trend_analysis", "topic_recommend")
    graph.add_edge("topic_recommend", "topic_select")

    graph.validate()
    return graph
