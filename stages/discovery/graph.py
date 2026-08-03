"""discovery 阶段子图构建（路线 A：构效关系发现）。"""
from __future__ import annotations

from core.orchestration.graph import Graph

from stages.common import StageCheckpoint
from stages.discovery.agents import (
    DiscoveryReportAgent,
    DiscoveryValidateAgent,
    HypothesisSeedAgent,
    LLMGuidedSearchAgent,
    SearchSpaceAgent,
)


def build_discovery_graph() -> Graph:
    """构建构效关系发现阶段子图。

    拓扑（LLM 深度参与搜索过程）：
        HypothesisSeedAgent（从 Research Gap 生成候选构效关系假设作为搜索种子）
        → SearchSpaceAgent（定义搜索空间 + 从文献抽取数据点）
        → StageCheckpoint（搜索前快照，便于回滚）
        → LLMGuidedSearchAgent（核心创新：MCTS + LLM 融合）
        → DiscoveryValidateAgent（文献交叉验证 + 新颖性评估 + 证据链关联）
        → DiscoveryReportAgent（结构化发现报告 + Artifact）

    检查点置于 LLM 引导搜索前（核心创新节点前），便于失败回滚到搜索空间定义阶段。
    """
    graph = Graph(name="discovery", stage="discovery")

    # 节点（按拓扑序添加）
    graph.add_node(HypothesisSeedAgent("hypothesis_seed"))
    graph.add_node(SearchSpaceAgent("search_space"))
    graph.add_node(StageCheckpoint("cp_before_search"))
    graph.add_node(LLMGuidedSearchAgent("llm_guided_search"))
    graph.add_node(DiscoveryValidateAgent("discovery_validate"))
    graph.add_node(DiscoveryReportAgent("discovery_report"))

    # 边
    graph.add_edge("hypothesis_seed", "search_space")
    graph.add_edge("search_space", "cp_before_search")
    graph.add_edge("cp_before_search", "llm_guided_search")
    graph.add_edge("llm_guided_search", "discovery_validate")
    graph.add_edge("discovery_validate", "discovery_report")

    graph.validate()
    return graph
