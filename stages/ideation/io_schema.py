"""ideation 阶段节点 IO schema 定义。

思路探讨阶段：基于 Paper 与用户交互式探讨 → 生成候选思路 → 用户讨论 → 验证 → 派生 draft Claim。
"""
from __future__ import annotations

from core.orchestration.node import NodeInput, NodeOutput


# ===== BrainstormAgent =====

class BrainstormInput(NodeInput):
    """思路生成输入。"""

    paper_ids: list[str]


class BrainstormOutput(NodeOutput):
    """思路生成输出：候选 Idea ID 列表。"""

    idea_ids: list[str]


# ===== IdeaDiscussHuman =====

class IdeaDiscussOutput(NodeOutput):
    """用户讨论后的输出：讨论笔记。"""

    discussion_notes: str


# ===== IdeaValidateAgent =====

class IdeaValidateInput(NodeInput):
    """思路验证输入。"""

    idea_ids: list[str]
    discussion_notes: str = ""


class IdeaValidateOutput(NodeOutput):
    """思路验证输出：通过验证的 Idea ID 列表。"""

    validated_idea_ids: list[str]


# ===== ClaimDraftAgent =====

class ClaimDraftInput(NodeInput):
    """Claim 草稿生成输入。"""

    validated_idea_ids: list[str]


class ClaimDraftOutput(NodeOutput):
    """Claim 草稿生成输出：draft Claim ID 列表（status=DRAFT）。"""

    draft_claim_ids: list[str]
