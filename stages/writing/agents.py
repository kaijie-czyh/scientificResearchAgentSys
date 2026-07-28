"""writing 阶段 Tool / Agent / Human 节点实现。

节点拓扑（借鉴 AI-Researcher 层级式论文生成）：
    ProvenanceCheckTool（溯源链硬校验，ToolNode，未验证 Claim/未完成 Experiment 全部拒绝）
    → StyleLearnAgent（从目标会议论文学习写作风格）
    → OutlineAgent（AI-Researcher：确定大纲，每章关联 Claim/Experiment）
    → StageCheckpoint
    → SectionDraftAgent（AI-Researcher：按章节逐步撰写，用 MiMo 1M 上下文装载全部素材）
    → ReviewAgent（以审稿人视角给修改意见）
    → ReviseHuman（用户确认终稿）

层级式生成（借鉴 AI-Researcher）的核心思想：
避免「一次性生成全文」导致的结构松散与引证缺失。先在大纲层确定每章要引用的
Claim/Experiment，再按章节填充内容，最后以审稿人视角校对。

说明：_execute 内 LLM 调用以完整注释范式给出，实际执行用占位数据返回，
既能验证 IO 闭环，又不会产生 API 费用。
"""
from __future__ import annotations

from typing import Optional

from core.artifacts import (
    ArtifactManager,
    ProvenanceError,
    ProvenanceValidator,
)
from core.knowledge import Artifact, ArtifactType, Claim, KnowledgeStore
from core.llm import LLMRegistry
from core.orchestration.context import ExecutionContext
from core.orchestration.node import (
    AgentNode,
    HumanNode,
    HumanResponse,
    NodeInput,
    NodeOutput,
    NodeResult,
    NodeStatus,
    ToolNode,
)

from stages.common import (
    ARTIFACT_MANAGER,
    DESIGN_CLAIM_IDS,
    EXPERIMENT_RESULT_ARTIFACT_IDS,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    PROVENANCE_VALIDATOR,
    WRITING_DRAFT_CONTENT,
    WRITING_OUTLINE,
    WRITING_PAPER_DRAFT_ARTIFACT_ID,
    WRITING_REVIEW_NOTES,
    WRITING_SECTIONS,
    WRITING_STYLE_PROFILE,
)
from stages.writing.io_schema import (
    OutlineInput,
    OutlineOutput,
    ProvenanceCheckInput,
    ProvenanceCheckOutput,
    ReviewInput,
    ReviewOutput,
    ReviseOutput,
    SectionDraftInput,
    SectionDraftOutput,
    StyleLearnInput,
    StyleLearnOutput,
)


# ===== ProvenanceCheckTool =====

class ProvenanceCheckTool(ToolNode):
    """溯源链硬校验工具节点。

    调用 ProvenanceValidator.validate_for_writing 校验溯源链完整性。
    校验内容：
    - 溯源链无断点（所有 cites_claim / cites_experiment 指向的实体存在）
    - 所有 Claim 已 VERIFIED（未验证 Claim 一律拒绝）
    - 所有 Experiment 已 COMPLETED（未完成 Experiment 一律拒绝）

    任何一项失败，整节点返回 NodeStatus.FAILED，阻断 writing 阶段后续流程。
    """

    node_type = "writing_provenance_check"
    input_schema = ProvenanceCheckInput
    output_schema = ProvenanceCheckOutput
    output_keys: dict = {}

    def _build_input(self, ctx: ExecutionContext) -> ProvenanceCheckInput:
        result_artifact_ids = ctx.get(EXPERIMENT_RESULT_ARTIFACT_IDS, [])
        return ProvenanceCheckInput(result_artifact_ids=result_artifact_ids)

    def _execute(
        self, input_obj: ProvenanceCheckInput, ctx: ExecutionContext
    ) -> NodeResult:
        validator: Optional[ProvenanceValidator] = ctx.get(PROVENANCE_VALIDATOR)

        # 校验器未注入：直接 FAILED（框架层配置错误，不可跳过）
        if validator is None:
            output = ProvenanceCheckOutput(
                provenance_ok=False,
                checked_artifact_ids=list(input_obj.result_artifact_ids),
                failed_artifact_ids=list(input_obj.result_artifact_ids),
                failure_reasons=["ProvenanceValidator 未注入到 context"] * len(
                    input_obj.result_artifact_ids
                ),
            )
            return NodeResult(
                status=NodeStatus.FAILED,
                output=output,
                error="ProvenanceValidator 未注入到 context",
                summary="溯源校验失败：ProvenanceValidator 未注入",
            )

        # 逐个 artifact 调用 validate_for_writing，捕获 ProvenanceError
        failed_ids: list[str] = []
        failure_reasons: list[str] = []
        for artifact_id in input_obj.result_artifact_ids:
            try:
                validator.validate_for_writing(artifact_id)
            except ProvenanceError as e:
                failed_ids.append(artifact_id)
                failure_reasons.append(str(e))

        if failed_ids:
            output = ProvenanceCheckOutput(
                provenance_ok=False,
                checked_artifact_ids=list(input_obj.result_artifact_ids),
                failed_artifact_ids=failed_ids,
                failure_reasons=failure_reasons,
            )
            return NodeResult(
                status=NodeStatus.FAILED,
                output=output,
                error=f"{len(failed_ids)} 个 artifact 溯源链校验失败",
                summary=(
                    f"溯源链校验失败：{len(failed_ids)}/{len(input_obj.result_artifact_ids)} "
                    f"artifact 未通过（未验证 Claim / 未完成 Experiment / 断链）"
                ),
            )

        output = ProvenanceCheckOutput(
            provenance_ok=True,
            checked_artifact_ids=list(input_obj.result_artifact_ids),
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"溯源链校验通过：{len(input_obj.result_artifact_ids)} 个 artifact "
                f"全部满足 writing 阶段要求（Claim 已 VERIFIED + Experiment 已 COMPLETED）"
            ),
        )


# ===== StyleLearnAgent =====

class StyleLearnAgent(AgentNode):
    """风格学习 Agent。

    调用 writing_style_learn task 从目标会议论文学习写作风格特征
    （句式、术语密度、章节结构、引用风格），输出 style_profile 文本。
    """

    node_type = "writing_style_learn"
    task_type = "writing_style_learn"
    input_schema = StyleLearnInput
    output_schema = StyleLearnOutput
    output_keys = {
        "style_profile": WRITING_STYLE_PROFILE,
    }

    def _build_input(self, ctx: ExecutionContext) -> StyleLearnInput:
        result_artifact_ids = ctx.get(EXPERIMENT_RESULT_ARTIFACT_IDS, [])
        return StyleLearnInput(result_artifact_ids=result_artifact_ids)

    def _execute(
        self, input_obj: StyleLearnInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 AI-Researcher：从目标会议/期刊的样本论文中提取风格特征
        # resp = registry.complete(
        #     task_type=self.task_type,
        #     system=(
        #         "你是学术写作助手。从目标会议论文中学习写作风格特征，"
        #         "输出结构化的风格描述：句式偏好（被动/主动）、术语密度、"
        #         "章节结构（IMRaD 或变体）、引用风格、段落长度。"
        #     ),
        #     prompt=(
        #         "请从以下参考产出物学习写作风格：\n"
        #         f"{input_obj.result_artifact_ids}"
        #     ),
        # )
        # style_profile = resp.text

        # 占位数据
        style_profile = (
            "学术正式风格：被动语态为主、术语密度适中、IMRaD 结构、"
            "段落 4-6 句、引用风格 APA、章节标题层级不超过 3 级"
        )

        output = StyleLearnOutput(style_profile=style_profile)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary="写作风格学习完成，生成 style_profile",
        )


# ===== OutlineAgent（借鉴 AI-Researcher 层级式生成）=====

class OutlineAgent(AgentNode):
    """大纲生成 Agent。

    借鉴 AI-Researcher 的「层级式论文生成」核心思想：
    先在大纲层确定论文整体结构与每章关联的 Claim/Experiment，
    避免后续按章撰写时反复调整结构、遗漏引证。

    设计要点：
    - 输入所有 Claim（DESIGN_CLAIM_IDS）+ 实验结果（EXPERIMENT_RESULT_ARTIFACT_IDS）
      + 风格 profile，由 LLM 规划章节结构
    - 每章显式关联 claim_ids（写进 outline），保证引证完整
    - 输出 target_word_count 用于后续 SectionDraftAgent 控制篇幅
    - task_type="writing_outline"，路由到 minimax MiniMax-M3（temp=0.3，平衡创意与稳定）
    """

    node_type = "writing_outline"
    task_type = "writing_outline"
    input_schema = OutlineInput
    output_schema = OutlineOutput
    output_keys = {
        "outline": WRITING_OUTLINE,
    }

    def _build_input(self, ctx: ExecutionContext) -> OutlineInput:
        return OutlineInput(
            claim_ids=ctx.get(DESIGN_CLAIM_IDS, []),
            result_artifact_ids=ctx.get(EXPERIMENT_RESULT_ARTIFACT_IDS, []),
            style_profile=ctx.get(WRITING_STYLE_PROFILE, ""),
        )

    def _execute(
        self, input_obj: OutlineInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 AI-Researcher：让 LLM 在大纲层做章节规划，每章关联 Claim/Experiment
        # from core.llm.base import StructuredOutputRequest
        # from pydantic import BaseModel
        # class SectionPlan(BaseModel):
        #     title: str
        #     claim_ids: list[str]
        #     key_points: list[str]
        #     target_word_count: int
        # class OutlineSchema(BaseModel):
        #     sections: list[SectionPlan]
        #     abstract: str
        #     total_target_word_count: int
        # # 从 KnowledgeStore 装载 Claim 与 Experiment 详情，作为 LLM 上下文
        # claims: list[Claim] = [
        #     store.get_claim(cid) for cid in input_obj.claim_ids
        # ]
        # claim_briefs = "\n".join(
        #     f"- {c.claim_id} [{c.status.value}] [{c.role}]: {c.statement}"
        #     for c in claims
        # )
        # result = registry.structured_output(
        #     task_type=self.task_type,
        #     output_schema=OutlineSchema,
        #     system=(
        #         "你是论文结构规划师。借鉴 AI-Researcher 的层级式生成思想，"
        #         "在写作前先确定整体大纲。要求：\n"
        #         "1. 每章关联明确的 claim_ids，覆盖所有提供的 Claim（不可遗漏）\n"
        #         "2. 章节组织符合目标会议风格 profile\n"
        #         "3. target_word_count 总和符合会议篇幅要求"
        #     ),
        #     prompt=(
        #         f"可用 Claim：\n{claim_briefs}\n\n"
        #         f"实验结果 artifact_ids：{input_obj.result_artifact_ids}\n\n"
        #         f"风格 profile：{input_obj.style_profile}"
        #     ),
        # )
        # outline = result.model_dump()

        # 占位数据：5 章结构，覆盖全部输入 Claim
        claim_ids = list(input_obj.claim_ids)
        sections = [
            {
                "title": "1. Introduction",
                "claim_ids": claim_ids[:1] if claim_ids else [],
                "key_points": ["研究动机", "核心贡献", "论文组织"],
                "target_word_count": 1200,
            },
            {
                "title": "2. Related Work",
                "claim_ids": [],
                "key_points": ["相关工作分类", "与本文方法对比"],
                "target_word_count": 1000,
            },
            {
                "title": "3. Method",
                "claim_ids": claim_ids[1:3] if len(claim_ids) >= 3 else claim_ids,
                "key_points": ["方法概述", "形式化定义", "算法描述"],
                "target_word_count": 2000,
            },
            {
                "title": "4. Experiments",
                "claim_ids": claim_ids[3:] if len(claim_ids) > 3 else [],
                "key_points": ["实验设置", "主结果", "消融实验", "案例分析"],
                "target_word_count": 2500,
            },
            {
                "title": "5. Conclusion",
                "claim_ids": [],
                "key_points": ["工作总结", "局限与未来工作"],
                "target_word_count": 800,
            },
        ]
        outline = {
            "sections": sections,
            "abstract": "论文摘要（占位，待 SectionDraftAgent 填充）",
            "total_target_word_count": sum(s["target_word_count"] for s in sections),
        }

        output = OutlineOutput(outline=outline)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"大纲生成完成：{len(sections)} 章节，"
                f"覆盖 {sum(len(s['claim_ids']) for s in sections)} 个 Claim 引用"
            ),
        )


# ===== SectionDraftAgent（借鉴 AI-Researcher 按章填充）=====

class SectionDraftAgent(AgentNode):
    """按章节逐步撰写 Agent。

    借鉴 AI-Researcher 的「按章填充」思想：
    - 基于大纲逐章生成内容，每章装载相关 Claim/Experiment 素材
    - MiMo 1M 上下文可一次性装载全部 Claim/Experiment 素材，
      避免长文截断导致的事实漂移（相比传统分块生成，能保持全文事实一致性）
    - 输出各章节内容列表 + 拼装后的全文草稿

    task_type="writing_section_draft"，路由到 mimo MiMo-V2-Pro（1M 上下文）。
    """

    node_type = "writing_section_draft"
    task_type = "writing_section_draft"
    input_schema = SectionDraftInput
    output_schema = SectionDraftOutput
    output_keys = {
        "sections": WRITING_SECTIONS,
        "draft_content": WRITING_DRAFT_CONTENT,
    }

    def _build_input(self, ctx: ExecutionContext) -> SectionDraftInput:
        return SectionDraftInput(
            outline=ctx.get(WRITING_OUTLINE, {}),
            claim_ids=ctx.get(DESIGN_CLAIM_IDS, []),
            result_artifact_ids=ctx.get(EXPERIMENT_RESULT_ARTIFACT_IDS, []),
            style_profile=ctx.get(WRITING_STYLE_PROFILE, ""),
        )

    def _execute(
        self, input_obj: SectionDraftInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 AI-Researcher：按大纲逐章生成，MiMo 1M 上下文装载全部素材
        # from core.llm.base import StructuredOutputRequest
        # from pydantic import BaseModel
        # class SectionDraft(BaseModel):
        #     title: str
        #     content: str  # Markdown 格式
        #     word_count: int
        # class SectionDraftList(BaseModel):
        #     sections: list[SectionDraft]
        # # 从 KnowledgeStore 装载全部 Claim 与 Experiment 素材
        # # MiMo 1M 上下文足够装下所有 Claim.statement + Experiment.result_summary，
        # # 不需要分块检索，避免长文事实漂移
        # claims: list[Claim] = [
        #     store.get_claim(cid) for cid in input_obj.claim_ids
        # ]
        # claim_context = "\n\n".join(
        #     f"[Claim {c.claim_id}] ({c.role}, {c.status.value})\n{c.statement}\n"
        #     f"证据：{c.evidence_refs}"
        #     for c in claims
        # )
        # # Experiment 素材通过 artifact_id 装载
        # experiment_context = "\n".join(
        #     f"- EXPERIMENT_RESULT artifact: {aid}" for aid in input_obj.result_artifact_ids
        # )
        # result = registry.structured_output(
        #     task_type=self.task_type,
        #     output_schema=SectionDraftList,
        #     system=(
        #         "你是科研论文撰写助手。借鉴 AI-Researcher 按章填充思想，"
        #         "基于大纲逐章生成内容。\n"
        #         "要求：\n"
        #         "1. 严格按大纲 sections 顺序撰写，每章引用其 claim_ids 标注的 Claim\n"
        #         "2. 利用 MiMo 1M 上下文一次性装载全部素材，保持全文事实一致\n"
        #         "3. 风格遵循 style_profile\n"
        #         "4. 每章字数接近 target_word_count"
        #     ),
        #     prompt=(
        #         f"大纲：\n{input_obj.outline}\n\n"
        #         f"全部 Claim 素材：\n{claim_context}\n\n"
        #         f"实验结果素材：\n{experiment_context}\n\n"
        #         f"风格 profile：{input_obj.style_profile}"
        #     ),
        # )
        # sections = [s.model_dump() for s in result.sections]
        # draft_content = "\n\n".join(
        #     f"## {s['title']}\n\n{s['content']}" for s in sections
        # )

        # 占位数据：按大纲逐章填充
        outline_sections = input_obj.outline.get("sections", []) if input_obj.outline else []
        if not outline_sections:
            outline_sections = [
                {"title": "1. Introduction", "target_word_count": 1200},
                {"title": "2. Related Work", "target_word_count": 1000},
                {"title": "3. Method", "target_word_count": 2000},
                {"title": "4. Experiments", "target_word_count": 2500},
                {"title": "5. Conclusion", "target_word_count": 800},
            ]

        sections = []
        draft_parts = []
        for sec in outline_sections:
            title = sec.get("title", "未命名章节")
            target_wc = sec.get("target_word_count", 1000)
            content = f"（占位内容：{title}，目标字数 {target_wc}）"
            word_count = target_wc
            sections.append({
                "title": title,
                "content": content,
                "word_count": word_count,
            })
            draft_parts.append(f"## {title}\n\n{content}")

        draft_content = "# 论文草稿\n\n" + "\n\n".join(draft_parts)

        output = SectionDraftOutput(
            sections=sections,
            draft_content=draft_content,
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"按章撰写完成：{len(sections)} 章，"
                f"全文 {sum(s['word_count'] for s in sections)} 字（MiMo 1M 上下文装载全部素材）"
            ),
        )


# ===== ReviewAgent（以审稿人视角）=====

class ReviewAgent(AgentNode):
    """审稿 Agent。

    以审稿人视角审查论文草稿，从三维度给出修改意见：
    - 结构（structure）：章节组织是否合理、论证逻辑是否连贯
    - 引证（citation）：Claim 引证是否充分、证据链是否完整
    - 表达（expression）：术语使用、句式风格、清晰度

    task_type="writing_review"，路由到 minimax MiniMax-M3（temp=0.2，稳定审稿）。
    """

    node_type = "writing_review"
    task_type = "writing_review"
    input_schema = ReviewInput
    output_schema = ReviewOutput
    output_keys = {
        "review_notes": WRITING_REVIEW_NOTES,
    }

    def _build_input(self, ctx: ExecutionContext) -> ReviewInput:
        return ReviewInput(
            draft_content=ctx.get(WRITING_DRAFT_CONTENT, ""),
            sections=ctx.get(WRITING_SECTIONS, []),
        )

    def _execute(
        self, input_obj: ReviewInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)

        # === LLM 调用范式（占位，实际未执行）===
        # 以审稿人视角从三维度（结构/引证/表达）给修改意见
        # resp = registry.complete(
        #     task_type=self.task_type,
        #     system=(
        #         "你是资深审稿人。从三维度审阅论文草稿并给出修改意见：\n"
        #         "1. 结构（structure, 0~5 分）：章节组织、论证逻辑连贯性\n"
        #         "2. 引证（citation, 0~5 分）：Claim 引证充分性、证据链完整性\n"
        #         "3. 表达（expression, 0~5 分）：术语使用、句式风格、清晰度\n"
        #         "每维度给出分数、问题清单与具体修改建议。"
        #     ),
        #     prompt=(
        #         f"论文草稿：\n{input_obj.draft_content}\n\n"
        #         f"各章节内容：\n{input_obj.sections}"
        #     ),
        # )
        # review_notes = resp.text

        # 占位数据：三维度结构化审稿意见
        review_notes = (
            "## 审稿意见\n\n"
            "### 1. 结构（structure）- 评分：4/5\n"
            "- 章节组织基本合理，符合 IMRaD 结构\n"
            "- 引言到方法的过渡略显突兀，建议增加动机铺垫\n"
            "- 实验章节缺乏与 baseline 的对比讨论\n\n"
            "### 2. 引证（citation）- 评分：3/5\n"
            "- 部分章节 Claim 引证不足，相关工作缺少对比引证\n"
            "- 方法章节有未引证的论断，需补充证据链\n"
            "- 建议核查所有 claim_ids 是否在正文中显式引用\n\n"
            "### 3. 表达（expression）- 评分：4/5\n"
            "- 术语使用规范，句式符合学术风格\n"
            "- 部分段落过长，建议拆分\n"
            "- 被动语态使用过多，可适当改为主动"
        )

        output = ReviewOutput(review_notes=review_notes)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary="审稿意见生成完成（结构 4/5 · 引证 3/5 · 表达 4/5）",
        )


# ===== ReviseHuman =====

class ReviseHuman(HumanNode):
    """用户确认终稿。

    呈现审稿意见，用户确认修改方向后产出最终 PAPER_DRAFT Artifact。
    借鉴 AI-Researcher：在产出最终稿前给用户最后干预机会，
    用户可选择接受当前草稿或给出具体修改方向（触发回滚到 SectionDraftAgent）。
    """

    node_type = "writing_revise"
    input_schema = NodeInput
    output_schema = ReviseOutput
    output_keys = {
        "paper_draft_artifact_id": WRITING_PAPER_DRAFT_ARTIFACT_ID,
    }

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _render_prompt(self, ctx: ExecutionContext) -> str:
        review_notes = ctx.get(WRITING_REVIEW_NOTES, "")
        return (
            f"审稿意见如下：\n{review_notes}\n\n"
            "请确认修改方向：\n"
            "  - 输入 'confirm' 接受当前草稿并产出最终论文稿 PAPER_DRAFT Artifact\n"
            "  - 或输入具体修改方向（将触发回滚到按章撰写阶段）"
        )

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        manager: Optional[ArtifactManager] = ctx.get(ARTIFACT_MANAGER)
        text = (response.text or "").strip()
        confirmed = text.lower() in ("confirm", "确认", "ok", "y", "yes")
        direction = "" if confirmed else text

        # === 产出最终 PAPER_DRAFT Artifact 范式（占位，实际未执行）===
        # draft_content = ctx.get(WRITING_DRAFT_CONTENT, "")
        # claim_ids = ctx.get(DESIGN_CLAIM_IDS, [])
        # result_artifact_ids = ctx.get(EXPERIMENT_RESULT_ARTIFACT_IDS, [])
        # # 通过 ArtifactManager 创建带版本的 PAPER_DRAFT Artifact
        # artifact: Artifact = manager.create_artifact(
        #     artifact_type=ArtifactType.PAPER_DRAFT,
        #     title="论文终稿",
        #     content=draft_content,
        #     cites_claim_ids=claim_ids,
        #     cites_experiment_ids=[],  # experiment_ids 通过 result_artifact_ids 间接引用
        #     source_stage="writing",
        #     created_by="writing_revise",
        # )
        # paper_draft_id = artifact.artifact_id

        # 占位数据：生成最终论文稿 Artifact ID（用静态方法，无需 DB 实例）
        paper_draft_id = KnowledgeStore.new_id()

        return ReviseOutput(
            confirmed=confirmed,
            revision_direction=direction,
            paper_draft_artifact_id=paper_draft_id,
        )
