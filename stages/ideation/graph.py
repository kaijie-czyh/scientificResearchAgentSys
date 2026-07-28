"""ideation 阶段子图构建。"""
from __future__ import annotations

from core.orchestration.graph import Graph
from core.state.lifecycle import LifecycleStage

from stages.common import StageCheckpoint
from stages.ideation.agents import (
    BrainstormAgent,
    ClaimDraftAgent,
    IdeaDiscussHuman,
    IdeaValidateAgent,
)


def build_ideation_graph() -> Graph:
    """构建思路探讨阶段子图。

    拓扑：
        BrainstormAgent → IdeaDiscussHuman → StageCheckpoint
        → IdeaValidateAgent → ClaimDraftAgent

    检查点置于思路验证前（关键决策点），便于讨论后回滚。
    """
    graph = Graph(name="ideation", stage=LifecycleStage.IDEATION.value)

    graph.add_node(BrainstormAgent("brainstorm"))
    graph.add_node(IdeaDiscussHuman("idea_discuss"))
    graph.add_node(StageCheckpoint("cp_before_validate"))
    graph.add_node(IdeaValidateAgent("idea_validate"))
    graph.add_node(ClaimDraftAgent("claim_draft"))

    graph.add_edge("brainstorm", "idea_discuss")
    graph.add_edge("idea_discuss", "cp_before_validate")
    graph.add_edge("cp_before_validate", "idea_validate")
    graph.add_edge("idea_validate", "claim_draft")

    graph.validate()
    return graph
