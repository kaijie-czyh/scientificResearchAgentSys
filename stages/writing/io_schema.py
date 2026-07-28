"""writing 阶段节点 IO schema 定义。

论文写作阶段全流程（借鉴 AI-Researcher 层级式生成）：
    溯源链硬校验
    → 风格学习（从目标会议论文学习写作风格）
    → 大纲确定（AI-Researcher：章节结构 + 每章关联 Claim/Experiment）
    → 检查点
    → 按章逐步撰写（AI-Researcher：MiMo 1M 上下文装载全部素材）
    → 审稿（以审稿人视角给修改意见）
    → 用户确认终稿

层级式生成的优势：避免「一次性生成全文」导致的结构松散与引证缺失。
先在大纲层确定每章要引用的 Claim/Experiment，再按章节填充，
最后以审稿人视角校对，确保论文结构紧凑、引证完整。
"""
from __future__ import annotations

from typing import Any

from core.orchestration.node import NodeInput, NodeOutput


# ===== ProvenanceCheckTool =====

class ProvenanceCheckInput(NodeInput):
    """溯源链硬校验输入。

    进入 writing 阶段前，所有 EXPERIMENT_RESULT Artifact 必须通过溯源校验。
    校验内容：溯源链无断点 + 所有 Claim 已 VERIFIED + 所有 Experiment 已 COMPLETED。
    未验证 Claim / 未完成 Experiment 全部拒绝，整阶段直接 FAILED。
    """

    result_artifact_ids: list[str]


class ProvenanceCheckOutput(NodeOutput):
    """溯源链校验输出。

    provenance_ok=False 时附 failed_artifact_ids 与 failure_reasons，便于人工追溯。
    """

    provenance_ok: bool
    checked_artifact_ids: list[str] = []
    failed_artifact_ids: list[str] = []
    # 校验失败原因（按 artifact_id 顺序对应）
    failure_reasons: list[str] = []


# ===== StyleLearnAgent =====

class StyleLearnInput(NodeInput):
    """风格学习输入：目标会议/期刊的产出物参考。"""

    result_artifact_ids: list[str]


class StyleLearnOutput(NodeOutput):
    """风格学习输出：写作风格特征描述。

    style_profile 是一段自由文本，描述句式、术语密度、章节结构、引用风格等。
    """

    style_profile: str


# ===== OutlineAgent（借鉴 AI-Researcher 层级式生成）=====

class OutlineInput(NodeInput):
    """大纲生成输入。

    借鉴 AI-Researcher：先在大纲层确定论文整体结构与每章关联的 Claim/Experiment。
    输入所有 Claim（DESIGN_CLAIM_IDS）+ 实验结果（EXPERIMENT_RESULT_ARTIFACT_IDS）
    + 风格 profile，由 LLM 规划章节结构。
    """

    claim_ids: list[str]
    result_artifact_ids: list[str]
    style_profile: str = ""


class OutlineOutput(NodeOutput):
    """大纲生成输出。

    outline 结构：
    {
        "sections": [
            {
                "title": "章节标题",
                "claim_ids": ["该章引用的 Claim ID"],
                "key_points": ["该章要覆盖的要点"],
                "target_word_count": 1500,
            },
            ...
        ],
        "abstract": "论文摘要（占位）",
        "total_target_word_count": 8000,
    }

    设计要点：
    - 每章显式关联 claim_ids，避免引证遗漏
    - target_word_count 用于后续 SectionDraftAgent 控制每章篇幅
    - 大纲层决策一次，避免按章撰写时反复调整结构
    """

    outline: dict[str, Any]


# ===== SectionDraftAgent（借鉴 AI-Researcher 按章填充）=====

class SectionDraftInput(NodeInput):
    """按章节撰写输入。

    借鉴 AI-Researcher：按大纲逐章填充内容。MiMo 1M 上下文可装载全部
    Claim/Experiment 素材，避免长文截断导致的事实漂移。
    """

    outline: dict[str, Any]
    claim_ids: list[str]
    result_artifact_ids: list[str]
    style_profile: str = ""


class SectionDraftOutput(NodeOutput):
    """按章撰写输出：各章节内容列表 + 组装后的全文草稿。

    sections 结构：
    [
        {"title": "...", "content": "...", "word_count": 1500},
        ...
    ]
    draft_content 是把各章节按顺序拼装后的全文（含 Markdown 标题层级）。
    """

    sections: list[dict[str, Any]]
    draft_content: str


# ===== ReviewAgent（以审稿人视角）=====

class ReviewInput(NodeInput):
    """审稿输入：草稿全文 + 各章节内容（便于按章节给意见）。"""

    draft_content: str
    sections: list[dict[str, Any]] = []


class ReviewOutput(NodeOutput):
    """审稿输出：三维度修改意见（结构/引证/表达）。

    review_notes 是结构化文本，按三维度组织：
    - 结构（structure）：章节组织是否合理、论证逻辑是否连贯
    - 引证（citation）：Claim 引证是否充分、证据链是否完整
    - 表达（expression）：术语使用、句式风格、清晰度
    每维度附评分（0~5）与具体修改建议。
    """

    review_notes: str


# ===== ReviseHuman =====

class ReviseOutput(NodeOutput):
    """用户确认终稿后的输出。

    confirmed=True 表示用户接受草稿并产出最终 PAPER_DRAFT Artifact。
    revision_direction 仅在 confirmed=False 时填写，给出用户的修改方向。
    """

    confirmed: bool
    revision_direction: str = ""
    paper_draft_artifact_id: str = ""
