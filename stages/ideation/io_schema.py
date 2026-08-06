"""ideation 阶段节点 IO schema 定义。

思路探讨阶段（借鉴 LangGraph 人在回路）：
基于调研产出（paper_ids + 交叉验证报告，含 conflicts/consensus/gaps）生成候选思路
→ 用户交互式探讨（可否决/修正/补充思路）→ 检查点
→ 三维度验证（可行性/新颖性/贡献度）
→ 从验证通过思路派生 draft Claim（status=DRAFT，无证据）。
"""
from __future__ import annotations

from typing import Any

from core.orchestration.node import NodeInput, NodeOutput


# ===== BrainstormAgent =====

class BrainstormInput(NodeInput):
    """思路生成输入。

    借鉴 LangGraph 人在回路：思路生成需基于调研的可信证据，因此同时读取
    paper_ids 与交叉验证报告。思路应针对 gaps（证据缺口）与 conflicts
    （未解决冲突）提出 hypothesis。
    """

    paper_ids: list[str]
    # 交叉验证报告，结构：
    # {
    #     "conflicts": [{"claim":..., "sources":..., "resolution":..., "confidence":...}, ...],
    #     "consensus": ["多方一致陈述", ...],
    #     "gaps": ["缺乏证据的子问题", ...],
    #     "overall_confidence": 0.0~1.0,
    # }
    cross_validation_report: dict[str, Any] = {}
    # 研究缺口清单（Task 3 结构化，优先消费）：
    # [{gap_id, gap_type, statement, detail, evidence, related_materials,
    #   actionability, priority, source, suggested_actions}]
    # 存在时按优先级生成思路；缺失时回退 cross_validation_report.gaps 字符串
    gap_report: list[dict[str, Any]] = []


class BrainstormOutput(NodeOutput):
    """思路生成输出：候选 Idea ID 列表 + 思路元数据。

    ideas_meta 不写回 context（仅留在 NodeResult.output 中供历史追溯），
    思路正文通过 KnowledgeStore 持久化后由下游节点按 id 读取。
    """

    idea_ids: list[str]
    # 每条：{idea_id, text, constraints, source_paper_ids}
    ideas_meta: list[dict[str, Any]] = []


# ===== IdeaDiscussHuman =====

class IdeaDiscussOutput(NodeOutput):
    """用户讨论后的输出：讨论笔记 + 更新后的 Idea ID 列表。

    用户可否决（reject: <序号>）或补充（add: <描述>）思路，
    因此 idea_ids 可能与 BrainstormAgent 输出不同。
    """

    discussion_notes: str
    idea_ids: list[str]


# ===== IdeaValidateAgent =====

class IdeaValidateInput(NodeInput):
    """思路验证输入。"""

    idea_ids: list[str]
    discussion_notes: str = ""


class IdeaValidateOutput(NodeOutput):
    """思路验证输出：通过验证的 Idea ID + 三维度评估报告。

    三维度均 >= 阈值（默认 0.5）视为通过：
    - feasibility（可行性）：是否有可落地的方法路径与资源
    - novelty（新颖性）：相对已有工作的差异度
    - contribution（贡献度）：潜在学术/工程价值
    """

    validated_idea_ids: list[str]
    # 每条：{idea_id, feasibility, novelty, contribution, passed, reason}
    validation_reports: list[dict[str, Any]] = []


# ===== ClaimDraftAgent =====

class ClaimDraftInput(NodeInput):
    """Claim 草稿生成输入。"""

    validated_idea_ids: list[str]


class ClaimDraftOutput(NodeOutput):
    """Claim 草稿生成输出：draft Claim ID 列表（status=DRAFT，无证据）。

    每个 Idea 派生 1-2 个可验证 Claim，statement 为一句话陈述。
    草稿阶段 evidence_refs=[]，进入 design 阶段后才关联证据。
    """

    draft_claim_ids: list[str]
    # 每条：{claim_id, statement, source_idea_id}
    claims_meta: list[dict[str, Any]] = []
