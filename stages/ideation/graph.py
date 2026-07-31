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
    """构建思路探讨阶段子图（借鉴 LangGraph 人在回路）。

    拓扑：
        BrainstormAgent（基于调研产出 + 交叉验证报告，针对 gaps/conflicts 生成 3-5 个候选思路）
        → IdeaDiscussHuman（用户交互式探讨：可否决/修正/补充思路）
        → StageCheckpoint（讨论后快照，便于回滚到思路生成阶段）
        → IdeaValidateAgent（三维度评估：可行性/新颖性/贡献度，均 >= 0.5 通过）
        → ClaimDraftAgent（从验证通过思路派生 draft Claim，status=DRAFT 无证据）

    检查点置于思路验证前（关键决策点）：用户讨论结束后、量化验证前做快照，
    便于在验证不通过或 Claim 派生异常时回滚到讨论阶段重新交互。
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
