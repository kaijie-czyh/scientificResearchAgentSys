"""experiment 阶段节点 IO schema 定义。

实验运行阶段（借鉴 AI-Researcher 的「导师-学生迭代」核心方法）：
    生成实验配置（数据集/baseline/超参）
    → Code Agent（学生）根据方法 + 公式↔代码映射生成实验代码
    → Advisor Agent（导师）审查代码，多轮迭代直到通过
    → 检查点
    → 运行实验（ToolNode）
    → 异常检测（loss spike/NaN/不收敛）
    → Claim 验证（用实验结果回填 Claim 证据）

说明：Code Agent 调用 experiment_code_generate（DeepSeek，编程最强）；
Advisor Agent 调用 experiment_code_review（MiniMax）。
"""
from __future__ import annotations

from typing import Any

from core.orchestration.node import NodeInput, NodeOutput


# ===== ExperimentConfigAgent =====

class ExperimentConfigInput(NodeInput):
    """实验配置生成输入。"""

    claim_ids: list[str]
    method_artifact_id: str = ""


class ExperimentConfigOutput(NodeOutput):
    """实验配置生成输出：实验配置列表。"""

    configs: list[dict[str, Any]]


# ===== CodeGenerateAgent（借鉴 AI-Researcher Code Agent）=====

class CodeGenerateInput(NodeInput):
    """实验代码生成输入。

    借鉴 AI-Researcher 的 Code Agent：根据实验配置 + design 阶段的
    公式↔代码映射（DESIGN_FORMULA_CODE_MAP），生成忠实实现论文方法的实验代码。
    """

    # 实验配置：[{name, dataset, baseline, hyperparams, verifies_claim_ids}]
    configs: list[dict[str, Any]]
    # 公式↔代码映射表：[{concept, formula_latex, code_stub, status}]
    # Code Agent 据此把每个公式落地为代码片段，确保代码忠实实现方法
    formula_code_map: list[dict[str, Any]] = []


class CodeGenerateOutput(NodeOutput):
    """实验代码生成输出：实验代码。

    code 结构：
    {
        "path": "experiments/run_exp.py",  # 代码文件路径（相对工程根）
        "content": "...",                   # 完整代码内容
        "language": "python",               # 语言（默认 python）
    }
    """

    code: dict[str, Any]


# ===== CodeReviewAgent（借鉴 AI-Researcher Advisor Agent）=====

class CodeReviewInput(NodeInput):
    """实验代码审查输入。

    借鉴 AI-Researcher 的 Advisor Agent：审查 Code Agent 生成的代码，
    校验是否忠实实现公式↔代码映射中的每个公式，并给出修改建议。
    """

    # 待审查的实验代码
    code: dict[str, Any]
    # 公式↔代码映射（用于校验代码是否忠实实现公式）
    formula_code_map: list[dict[str, Any]] = []
    # 上一轮审查记录（多轮迭代时传入，Advisor Agent 据此判断是否已修正）
    prev_review_notes: list[dict[str, Any]] = []


class CodeReviewOutput(NodeOutput):
    """实验代码审查输出：审查记录列表 + 是否通过标志。

    review_notes 结构（每轮审查追加一条）：
    {
        "round": 1,                         # 审查轮次（1-based）
        "passed": False,                    # 本轮是否通过
        "issues": [                          # 发现的问题
            {"severity": "high/medium/low", "category": "公式偏差/bug/规范",
             "location": "L42-58", "comment": "...", "suggestion": "..."},
            ...
        ],
        "summary": "本轮审查总结",
    }
    """

    # 累计审查记录列表（含历史轮次）
    review_notes: list[dict[str, Any]]
    # 最终是否通过（所有 issue 已修正或可接受）
    passed: bool


# ===== ExperimentRunTool =====

class ExperimentRunInput(NodeInput):
    """实验运行输入。"""

    configs: list[dict[str, Any]]
    # 实验代码（运行时执行此代码）
    code: dict[str, Any] = {}


class ExperimentRunOutput(NodeOutput):
    """实验运行输出：实验 ID 列表。"""

    experiment_ids: list[str]


# ===== AnomalyCheckAgent =====

class AnomalyCheckInput(NodeInput):
    """异常检测输入。"""

    experiment_ids: list[str]


class AnomalyCheckOutput(NodeOutput):
    """异常检测输出：异常报告。"""

    anomaly_report: str


# ===== ClaimVerifyAgent =====

class ClaimVerifyInput(NodeInput):
    """Claim 验证输入。"""

    experiment_ids: list[str]
    claim_ids: list[str]


class ClaimVerifyOutput(NodeOutput):
    """Claim 验证输出：结果 Artifact ID 列表。"""

    result_artifact_ids: list[str]


# ===== ExperimentOutcomeAssessAgent =====

class ExperimentOutcomeAssessInput(NodeInput):
    """实验成败评估输入。

    输入：已完成的实验 ID + 待验证的 Claim ID + 异常报告。
    Agent 据此判断实验结果是否验证了核心 Claim，给出"是否进入 writing"的建议。

    说明：实验失败是科研常态——Claim 被实验反驳是正常、有价值的发现。
    系统不应在实验失败时强行进入论文写作阶段。
    """

    # 已完成的实验 ID 列表
    experiment_ids: list[str]
    # 待验证的 Claim ID 列表（来自 DESIGN_CLAIM_IDS）
    claim_ids: list[str]
    # 异常报告（来自 EXPERIMENT_ANOMALY_REPORT），无异常时为空字符串
    anomaly_report: str = ""


class ExperimentOutcomeAssessOutput(NodeOutput):
    """实验成败评估输出：写入 EXPERIMENT_OUTCOME 域键。

    outcome 结构（写入 EXPERIMENT_OUTCOME 的 dict）：
    {
        "success": bool,                   # 实验是否成功验证了核心 Claim
        "verified_claim_ids": [...],       # 被实验验证的 Claim
        "refuted_claim_ids": [...],        # 被实验反驳的 Claim
        "inconclusive_claim_ids": [...],   # 无法定论的 Claim
        "recommendation": "proceed_to_writing" / "rollback_to_ideation"
                         / "retry_experiment" / "abort",
        "summary": "一句话总结"
    }

    recommendation 取值说明：
    - proceed_to_writing：实验验证了核心 Claim，进入论文写作阶段
    - rollback_to_ideation：Claim 被反驳，回滚到 ideation 重新探讨思路
    - retry_experiment：实验无法定论但有重试价值，重跑实验阶段
    - abort：异常严重或多次重试失败，中止当前流程
    """

    outcome: dict[str, Any]
