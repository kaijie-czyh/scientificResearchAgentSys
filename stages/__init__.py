"""科研论文 Agent 系统 — 5 个生命周期阶段子图。

每个阶段提供一个 build_*_graph() 函数，返回经 validate() 校验的 DAG 子图。

阶段流转：
    research（调研）→ ideation（思路探讨）→ design（方案制定）
    → experiment（实验运行）→ writing（论文写作）

各阶段节点统一从 ExecutionContext 取系统依赖（LLMRegistry / KnowledgeStore /
ArtifactManager / ProvenanceValidator），依赖键定义见 stages.common。
"""
from stages.design.graph import build_design_graph
from stages.experiment.graph import build_experiment_graph
from stages.ideation.graph import build_ideation_graph
from stages.research.graph import build_research_graph
from stages.writing.graph import build_writing_graph

__all__ = [
    "build_research_graph",
    "build_ideation_graph",
    "build_design_graph",
    "build_experiment_graph",
    "build_writing_graph",
]
