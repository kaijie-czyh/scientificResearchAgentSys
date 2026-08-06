"""research 阶段子图构建。"""
from __future__ import annotations

from core.orchestration.graph import Graph
from core.state.lifecycle import LifecycleStage

from stages.common import StageCheckpoint
from stages.research.agents import (
    CrossValidateAgent,
    MaterialKnowledgeExtractionAgent,
    PaperFetchAgent,
    PaperIngestAgent,
    PaperRelevanceFilterAgent,
    ResearchGapIdentifyAgent,
    SubqueryDecomposeAgent,
    TopicConfirmHuman,
    TopicRefineAgent,
)


def build_research_graph() -> Graph:
    """构建调研阶段子图。

    拓扑（借鉴 PaperQA + GPT-Researcher）：
        TopicRefineAgent
        → SubqueryDecomposeAgent（GPT-Researcher：子问题分解）
        → StageCheckpoint
        → TopicConfirmHuman（用户确认检索方向，可修订子问题）
        → PaperFetchAgent（按子问题并行检索 arxiv/S2）
        → PaperRelevanceFilterAgent（PaperQA filter：相关性打分+筛选）
        → PaperIngestAgent（chunk 摘要 + 向量入库）
        → MaterialKnowledgeExtractionAgent（Task 2：材料-性能-合成三元组抽取）
        → CrossValidateAgent（GPT-Researcher：多源交叉验证，输出可信度报告）
        → ResearchGapIdentifyAgent（Task 3：研究缺口识别，双通道结构化 Gap 清单）

    检查点置于用户确认前（关键决策点），便于回滚到子问题分解阶段。
    材料知识抽取在入库后执行（依赖入库论文），交叉验证在其后。
    研究缺口识别是 research 阶段出口节点（升级 cross_validate 的字符串 gaps
    为结构化 Gap，供 ideation/discovery/调研报告消费）。
    """
    graph = Graph(name="research", stage=LifecycleStage.RESEARCH.value)

    # 节点（按拓扑序添加：首个为入口，末个为出口）
    graph.add_node(TopicRefineAgent("topic_refine"))
    graph.add_node(SubqueryDecomposeAgent("subquery_decompose"))
    graph.add_node(StageCheckpoint("cp_before_confirm"))
    graph.add_node(TopicConfirmHuman("topic_confirm"))
    graph.add_node(PaperFetchAgent("paper_fetch"))
    graph.add_node(PaperRelevanceFilterAgent("paper_filter"))
    graph.add_node(PaperIngestAgent("paper_ingest"))
    graph.add_node(MaterialKnowledgeExtractionAgent("material_extraction"))
    graph.add_node(CrossValidateAgent("cross_validate"))
    graph.add_node(ResearchGapIdentifyAgent("research_gap"))

    # 边
    graph.add_edge("topic_refine", "subquery_decompose")
    graph.add_edge("subquery_decompose", "cp_before_confirm")
    graph.add_edge("cp_before_confirm", "topic_confirm")
    graph.add_edge("topic_confirm", "paper_fetch")
    graph.add_edge("paper_fetch", "paper_filter")
    graph.add_edge("paper_filter", "paper_ingest")
    graph.add_edge("paper_ingest", "material_extraction")
    graph.add_edge("material_extraction", "cross_validate")
    graph.add_edge("cross_validate", "research_gap")

    graph.validate()
    return graph
