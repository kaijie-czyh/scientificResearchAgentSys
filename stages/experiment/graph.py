"""experiment 阶段子图构建。"""
from __future__ import annotations

from core.orchestration.graph import Graph
from core.state.lifecycle import LifecycleStage

from stages.common import StageCheckpoint
from stages.experiment.agents import (
    AnomalyCheckAgent,
    ClaimVerifyAgent,
    CodeGenerateAgent,
    CodeReviewAgent,
    ExperimentConfigAgent,
    ExperimentRunTool,
)


def build_experiment_graph() -> Graph:
    """构建实验运行阶段子图。

    拓扑（借鉴 AI-Researcher 的「导师-学生迭代」核心方法）：
        ExperimentConfigAgent（生成实验配置：数据集/baseline/超参）
        → CodeGenerateAgent（AI-Researcher Code Agent：DeepSeek 生成实验代码）
        → CodeReviewAgent（AI-Researcher Advisor Agent：审查代码，多轮迭代）
        → StageCheckpoint
        → ExperimentRunTool（执行实验，ToolNode）
        → AnomalyCheckAgent（检测异常：loss spike/NaN/不收敛）
        → ClaimVerifyAgent（用实验结果验证 Claim）

    检查点置于实验运行前（代码审查通过后的关键决策点），便于实验失败回滚到
    代码生成阶段。导师-学生迭代语义：CodeReviewAgent 审查不通过时，理论上应回到
    CodeGenerateAgent 重新生成；由于 graph 是 DAG 不支持环，实际多轮迭代由
    GraphRunner 外部循环驱动（重跑 CodeGenerate→CodeReview 子链），或在
    CodeReviewAgent 内部循环 MAX_REVIEW_ROUNDS 次。
    """
    graph = Graph(name="experiment", stage=LifecycleStage.EXPERIMENT.value)

    # 节点（按拓扑序添加：首个为入口，末个为出口）
    graph.add_node(ExperimentConfigAgent("experiment_config"))
    graph.add_node(CodeGenerateAgent("code_generate"))
    graph.add_node(CodeReviewAgent("code_review"))
    graph.add_node(StageCheckpoint("cp_before_run"))
    graph.add_node(ExperimentRunTool("experiment_run"))
    graph.add_node(AnomalyCheckAgent("anomaly_check"))
    graph.add_node(ClaimVerifyAgent("claim_verify"))

    # 边
    graph.add_edge("experiment_config", "code_generate")
    graph.add_edge("code_generate", "code_review")
    graph.add_edge("code_review", "cp_before_run")
    graph.add_edge("cp_before_run", "experiment_run")
    graph.add_edge("experiment_run", "anomaly_check")
    graph.add_edge("anomaly_check", "claim_verify")

    graph.validate()
    return graph
