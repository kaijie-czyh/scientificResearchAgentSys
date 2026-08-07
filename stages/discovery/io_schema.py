"""discovery 阶段节点 IO schema 定义。

构效关系发现阶段（路线 A）：
    候选假设生成（从 Research Gap 出发，LLM 生成构效关系假设作为搜索种子）
    → 搜索空间定义（材料变量/性能目标/物理约束，从文献抽取数据点）
    → LLM 引导搜索（MCTS + LLM 融合：生成候选 + 评估合理性 + 剪枝）
    → 发现验证（文献交叉验证 + 新颖性评估 + 证据链关联）
    → 发现报告（结构化报告 + 物理机制解释 + Artifact）

核心创新：LLM 深度参与搜索过程，而非仅生成搜索代码。
- LLM 生成候选构效关系假设作为搜索种群种子
- LLM 评估中间结果的科学合理性（物理合法性、与文献一致性）
- LLM 引导搜索空间剪枝（排除物理不合理的区域）
"""
from __future__ import annotations

from typing import Any

from core.orchestration.node import NodeInput, NodeOutput


# ===== HypothesisSeedAgent =====

class HypothesisSeedInput(NodeInput):
    """候选假设生成输入。

    从 research 阶段的 Research Gap + 共识/冲突 + 入库论文出发，
    LLM 生成候选构效关系假设作为搜索种子。
    """

    topic: str
    gaps: list[str] = []
    conflicts: list[dict] = []
    consensus: list[str] = []
    paper_ids: list[str] = []


class HypothesisSeedOutput(NodeOutput):
    """候选假设生成输出：构效关系假设列表（搜索种子）。

    hypotheses 结构（每条假设）：
        {
            "hypothesis": str,          # 假设陈述（如 "Y掺杂浓度x∈[0,0.1]与Bi2Te3的ZT呈正相关"）
            "variables": list[str],     # 涉及的变量名
            "target_property": str,     # 目标性能（如 "ZT"）
            "rationale": str,           # 假设依据（关联哪个 Gap/冲突/共识）
            "gap_ref": str,             # 关联的 Research Gap
        }
    """

    hypotheses: list[dict[str, Any]]


# ===== SearchSpaceAgent =====

class SearchSpaceInput(NodeInput):
    """搜索空间定义输入。"""

    topic: str
    hypotheses: list[dict[str, Any]] = []
    paper_ids: list[str] = []


class SearchSpaceOutput(NodeOutput):
    """搜索空间定义输出。

    search_space 结构：
        {
            "variables": [
                {"name": str, "low": float, "high": float, "unit": str,
                 "type": "continuous|discrete|categorical", "categories": list[str]}
            ],
            "target_property": str,       # 目标性能名（如 "ZT"）
            "target_unit": str,           # 目标性能单位
            "constraints": list[str],     # 物理约束（如 "掺杂浓度不超过0.2"）
            "literature_points": [        # 从文献抽取的 (结构, 性能) 数据点
                {"config": {var: value}, "target": float,
                 "paper_id": str, "chunk_id": str, "note": str}
            ],
        }
    """

    search_space: dict[str, Any]


# ===== LLMGuidedSearchAgent（核心创新节点）=====

class LLMGuidedSearchInput(NodeInput):
    """LLM 引导搜索输入。"""

    hypotheses: list[dict[str, Any]] = []
    search_space: dict[str, Any] = {}


class LLMGuidedSearchOutput(NodeOutput):
    """LLM 引导搜索输出：候选构效关系列表。

    candidates 结构（每条候选）：
        {
            "config": {var: value},        # 材料配置
            "predicted_target": float,     # 代理模型预测的性能
            "plausibility": float,         # LLM 评估的科学合理性 0~1
            "mechanism": str,              # 物理机制解释
            "novelty": str,                # 新颖性说明
            "surrogate_confidence": float, # 代理模型置信度
        }
    """

    candidates: list[dict[str, Any]]


# ===== DiscoveryValidateAgent =====

class DiscoveryValidateInput(NodeInput):
    """发现验证输入。"""

    candidates: list[dict[str, Any]] = []
    search_space: dict[str, Any] = {}
    paper_ids: list[str] = []


class DiscoveryValidateOutput(NodeOutput):
    """发现验证输出：经验证与新颖性评估的构效关系列表。

    relationships 结构（每条发现）：
        {
            "relationship": str,           # 构效关系陈述
            "config": {var: value},        # 最优配置
            "predicted_target": float,     # 预测性能
            "evidence_refs": list[dict],   # 证据链 [{type:"paper", id:...}]
            "novelty": str,                # 新颖性评估（novel/partially_known/known）
            "novelty_reason": str,         # 新颖性理由
            "mechanism": str,              # 物理机制解释
            "confidence": float,           # 综合置信度 0~1
        }
    """

    relationships: list[dict[str, Any]]


# ===== DiscoveryReportAgent =====

class DiscoveryReportInput(NodeInput):
    """发现报告生成输入。"""

    relationships: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    search_space: dict[str, Any] = {}


class DiscoveryReportOutput(NodeOutput):
    """发现报告生成输出：报告内容 + Artifact ID。"""

    report_content: str
    report_artifact_id: str = ""
