"""writing 阶段子图构建。"""
from __future__ import annotations

from core.orchestration.graph import Graph
from core.state.lifecycle import LifecycleStage

from stages.common import StageCheckpoint
from stages.writing.agents import (
    OutlineAgent,
    ProvenanceCheckTool,
    ReviewAgent,
    ReviseHuman,
    SectionDraftAgent,
    StyleLearnAgent,
)


def build_writing_graph() -> Graph:
    """构建论文写作阶段子图。

    拓扑（借鉴 AI-Researcher 层级式论文生成）：
        ProvenanceCheckTool（溯源链硬校验，未验证 Claim/未完成 Experiment 全部拒绝）
        → StyleLearnAgent（从目标会议论文学习写作风格）
        → OutlineAgent（AI-Researcher：确定大纲，每章关联 Claim/Experiment）
        → StageCheckpoint
        → SectionDraftAgent（AI-Researcher：按章节逐步撰写，MiMo 1M 上下文装载全部素材）
        → ReviewAgent（以审稿人视角给修改意见）
        → ReviseHuman（用户确认终稿）

    检查点置于大纲生成后、按章撰写前（关键决策点）：
    - 大纲一旦确定，按章撰写将基于此大纲填充，结构变更代价高
    - 检查点便于在大纲决策后回滚，避免已生成章节被废弃
    """
    graph = Graph(name="writing", stage=LifecycleStage.WRITING.value)

    # 节点（按拓扑序添加：首个为入口，末个为出口）
    graph.add_node(ProvenanceCheckTool("provenance_check"))
    graph.add_node(StyleLearnAgent("style_learn"))
    graph.add_node(OutlineAgent("outline"))
    graph.add_node(StageCheckpoint("cp_before_draft"))
    graph.add_node(SectionDraftAgent("section_draft"))
    graph.add_node(ReviewAgent("review"))
    graph.add_node(ReviseHuman("revise"))

    # 边
    graph.add_edge("provenance_check", "style_learn")
    graph.add_edge("style_learn", "outline")
    graph.add_edge("outline", "cp_before_draft")
    graph.add_edge("cp_before_draft", "section_draft")
    graph.add_edge("section_draft", "review")
    graph.add_edge("review", "revise")

    graph.validate()
    return graph
