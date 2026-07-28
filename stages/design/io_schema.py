"""design 阶段节点 IO schema 定义。

方案制定阶段（借鉴 AI-Researcher 原子概念分解）：
    原子概念分解（建立公式↔代码双向映射）
    → 方法形式化（公式 + 伪代码）
    → 用户审核
    → Claim 抽取与证据关联
    → 方法 Artifact 产出

核心思想（AI-Researcher）：把方法拆为最小可独立验证的原子概念，
每个概念建立「数学公式 ↔ 代码实现」双向映射，确保论文中的公式与
实验代码一一对应，避免「论文写一套、代码做一套」。
"""
from __future__ import annotations

from typing import Any

from core.orchestration.node import NodeInput, NodeOutput


# ===== AtomDecomposeAgent（借鉴 AI-Researcher）=====

class AtomDecomposeInput(NodeInput):
    """原子概念分解输入。

    借鉴 AI-Researcher：从 ideation 阶段传入的 validated idea 出发，
    把方法拆为原子概念，每个概念建立「数学公式 ↔ 代码实现」双向映射。
    """

    idea_ids: list[str]


class AtomDecomposeOutput(NodeOutput):
    """原子概念分解输出：原子概念列表 + 公式↔代码映射表。

    atom_concepts 结构（每个原子概念）：
        {
            "concept_name": str,        # 概念名（如 "attention_weight"）
            "description": str,         # 概念描述
            "formula_latex": str,       # 对应数学公式（LaTeX）
            "code_stub": str,           # 对应代码骨架（Python stub）
            "dependencies": list[str],  # 依赖的其他 concept_name
        }

    formula_code_map 结构（公式↔代码映射表）：
        {
            "concept": str,         # 关联的 concept_name
            "formula_latex": str,   # 公式
            "code_stub": str,       # 代码
            "status": str,          # mapped / pending / mismatched
        }

    设计要点：
    - 原子概念应互相正交，每个可独立验证
    - 公式与代码必须一一对应（status=mapped），冲突时标记 mismatched
    - dependencies 形成 DAG，便于后续实验阶段按依赖顺序实现
    """

    atom_concepts: list[dict[str, Any]]
    formula_code_map: list[dict[str, Any]]


# ===== MethodFormalizeAgent =====

class MethodFormalizeInput(NodeInput):
    """方法形式化输入。

    基于原子概念与公式↔代码映射，整合为完整方法文档（公式 + 伪代码）。
    """

    idea_ids: list[str]
    atom_concepts: list[dict[str, Any]] = []
    formula_code_map: list[dict[str, Any]] = []


class MethodFormalizeOutput(NodeOutput):
    """方法形式化输出：方法内容（公式与伪代码）。"""

    method_content: str


# ===== MethodReviewHuman =====

class MethodReviewOutput(NodeOutput):
    """用户审核方法后的输出。"""

    approved: bool
    review_comments: str = ""


# ===== ClaimEvidenceLinkAgent =====

class ClaimEvidenceLinkInput(NodeInput):
    """Claim 抽取与证据关联输入。

    借鉴 AI-Researcher：从形式化方法中抽取可验证的 Claim，
    并为每个 Claim 关联 Paper/Experiment 证据。
    """

    method_content: str
    atom_concepts: list[dict[str, Any]] = []


class ClaimEvidenceLinkOutput(NodeOutput):
    """Claim 抽取与证据关联输出：含证据的 Claim ID 列表。

    每个 Claim 状态置为 EVIDENCE_LINKED，evidence_refs 指向 Paper/Experiment。
    """

    claim_ids: list[str]


# ===== MethodArtifactAgent =====

class MethodArtifactInput(NodeInput):
    """方法 Artifact 生成输入。"""

    claim_ids: list[str]
    method_content: str = ""


class MethodArtifactOutput(NodeOutput):
    """方法 Artifact 生成输出：METHOD_DOC Artifact ID。"""

    method_artifact_id: str
