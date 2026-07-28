"""design 阶段子图构建。"""
from __future__ import annotations

from core.orchestration.graph import Graph
from core.state.lifecycle import LifecycleStage

from stages.common import StageCheckpoint
from stages.design.agents import (
    AtomDecomposeAgent,
    ClaimEvidenceLinkAgent,
    MethodArtifactAgent,
    MethodFormalizeAgent,
    MethodReviewHuman,
)


def build_design_graph() -> Graph:
    """构建方案制定阶段子图。

    拓扑（借鉴 AI-Researcher 原子概念分解）：
        AtomDecomposeAgent（AI-Researcher：原子概念分解，建立公式↔代码映射）
        → MethodFormalizeAgent（将方法形式化为公式与伪代码）
        → StageCheckpoint
        → MethodReviewHuman（用户确认方法）
        → ClaimEvidenceLinkAgent（抽取 Claim 并关联证据）
        → MethodArtifactAgent（生成方法文档 Artifact）

    检查点置于用户审核前（关键决策点），便于回滚到方法形式化阶段。
    原子概念分解在最前端，确保后续形式化与实验代码基于一致的公式↔代码映射。
    """
    graph = Graph(name="design", stage=LifecycleStage.DESIGN.value)

    # 节点（按拓扑序添加：首个为入口，末个为出口）
    graph.add_node(AtomDecomposeAgent("atom_decompose"))
    graph.add_node(MethodFormalizeAgent("method_formalize"))
    graph.add_node(StageCheckpoint("cp_before_review"))
    graph.add_node(MethodReviewHuman("method_review"))
    graph.add_node(ClaimEvidenceLinkAgent("claim_evidence_link"))
    graph.add_node(MethodArtifactAgent("method_artifact"))

    # 边
    graph.add_edge("atom_decompose", "method_formalize")
    graph.add_edge("method_formalize", "cp_before_review")
    graph.add_edge("cp_before_review", "method_review")
    graph.add_edge("method_review", "claim_evidence_link")
    graph.add_edge("claim_evidence_link", "method_artifact")

    graph.validate()
    return graph
