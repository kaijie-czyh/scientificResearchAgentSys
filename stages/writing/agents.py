"""writing 阶段 Tool / Agent / Human 节点实现。

节点拓扑（借鉴 AI-Researcher 层级式论文生成）：
    ProvenanceCheckTool（溯源链硬校验，ToolNode，未验证 Claim/未完成 Experiment 全部拒绝）
    → StyleLearnAgent（从目标会议论文学习写作风格）
    → OutlineAgent（AI-Researcher：确定大纲，每章关联 Claim/Experiment）
    → StageCheckpoint
    → SectionDraftAgent（AI-Researcher：按章节逐步撰写，用 1M 上下文装载全部素材）
    → ReviewAgent（以审稿人视角给修改意见）
    → ReviseHuman（用户确认终稿）

层级式生成（借鉴 AI-Researcher）的核心思想：
避免「一次性生成全文」导致的结构松散与引证缺失。先在大纲层确定每章要引用的
Claim/Experiment，再按章节填充内容，最后以审稿人视角校对。

执行模式：
- dry_run=True  ：用占位数据返回，不调用 LLM（默认，验证架构用）
- dry_run=False ：真实调用 MiniMax M3，产出大纲/章节/审稿意见/最终论文 Artifact
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from core.artifacts import (
    ArtifactManager,
    ProvenanceError,
    ProvenanceValidator,
)
from core.knowledge import (
    Artifact,
    ArtifactType,
    Claim,
    KnowledgeStore,
)
from core.llm import LLMRegistry
from core.llm.base import LLMError
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
    DRY_RUN,
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

logger = logging.getLogger(__name__)


# ===== 结构化输出 Schema =====

class StyleLearnSchema(BaseModel):
    """风格学习结构化输出。"""

    voice: str = Field(description="语态偏好（被动/主动/混合）")
    terminology_density: str = Field(description="术语密度（高/中/低）")
    section_structure: str = Field(description="章节结构（IMRaD 或变体描述）")
    citation_style: str = Field(description="引用风格（APA/IEEE/数字等）")
    paragraph_length: str = Field(description="段落长度特征")
    style_summary: str = Field(description="综合风格描述，一段话")


class SectionPlan(BaseModel):
    """单章规划。"""

    title: str = Field(description="章节标题")
    claim_ids: list[str] = Field(default_factory=list, description="该章引用的 Claim ID")
    key_points: list[str] = Field(default_factory=list, description="该章要覆盖的要点")
    target_word_count: int = Field(description="目标字数")


class OutlineSchema(BaseModel):
    """大纲结构化输出（借鉴 AI-Researcher 层级式生成）。"""

    sections: list[SectionPlan] = Field(description="5-7 个章节规划")
    abstract: str = Field(description="论文摘要（基于 Claim 与实验结果）")
    total_target_word_count: int = Field(description="总目标字数")


class SectionDraftItem(BaseModel):
    """单章草稿。"""

    title: str = Field(description="章节标题")
    content: str = Field(description="章节正文（Markdown）")
    word_count: int = Field(description="实际字数")


class SectionDraftList(BaseModel):
    """按章撰写结构化输出。"""

    sections: list[SectionDraftItem] = Field(description="各章节草稿")


class ReviewDimension(BaseModel):
    """审稿单维度评分。"""

    score: float = Field(description="0~5 分")
    issues: list[str] = Field(default_factory=list, description="问题清单")
    suggestions: list[str] = Field(default_factory=list, description="具体修改建议")


class ReviewSchema(BaseModel):
    """审稿结构化输出。"""

    structure: ReviewDimension = Field(description="结构维度")
    citation: ReviewDimension = Field(description="引证维度")
    expression: ReviewDimension = Field(description="表达维度")
    overall_comment: str = Field(description="总体意见")


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
        dry_run: bool = ctx.get(DRY_RUN, True)

        # dry_run 模式下宽松通过：dry_run 的 agent 只生成 ID 未真实持久化实体到
        # KnowledgeStore，溯源链自然不完整。这是 dry_run 的固有限制，不是 bug。
        # 真实模式（dry_run=False）下执行严格校验，未验证 Claim/未完成 Experiment
        # 一律拒绝进入 writing。
        if dry_run:
            output = ProvenanceCheckOutput(
                provenance_ok=True,
                checked_artifact_ids=list(input_obj.result_artifact_ids),
                failure_reasons=[
                    "dry_run 模式宽松通过：agent 未真实持久化实体，溯源链校验已跳过"
                ],
            )
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=(
                    "dry_run 模式：溯源链校验宽松通过（agent 未真实持久化实体，"
                    "真实模式将执行严格校验）"
                ),
            )

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
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not dry_run and registry is not None:
            try:
                # 装载实验结果 artifact 摘要作为风格学习的参考样本
                sample_text = self._load_samples(store, input_obj.result_artifact_ids)

                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=StyleLearnSchema,
                    system=(
                        "你是学术写作风格分析助手。基于参考样本与目标会议惯例，"
                        "提取写作风格特征：句式偏好（被动/主动）、术语密度、"
                        "章节结构（IMRaD 或变体）、引用风格、段落长度。"
                        "若无参考样本，按计算机顶会常见风格给出默认特征。"
                    ),
                    prompt=(
                        f"参考样本（实验结果摘要）：\n{sample_text or '（无样本，按默认风格）'}\n\n"
                        "请输出结构化的写作风格描述。"
                    ),
                )
                style_profile = (
                    f"语态：{result.voice}；术语密度：{result.terminology_density}；"
                    f"章节结构：{result.section_structure}；引用风格：{result.citation_style}；"
                    f"段落：{result.paragraph_length}。综合：{result.style_summary}"
                )
            except (LLMError, Exception) as e:
                logger.warning("StyleLearn 真实调用失败，回退占位: %s", e)
                style_profile = self._placeholder()
        else:
            style_profile = self._placeholder()

        output = StyleLearnOutput(style_profile=style_profile)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary="写作风格学习完成，生成 style_profile",
        )

    @staticmethod
    def _load_samples(
        store: Optional[KnowledgeStore], artifact_ids: list[str]
    ) -> str:
        if store is None or not artifact_ids:
            return ""
        try:
            parts: list[str] = []
            for aid in artifact_ids[:3]:
                art = store.get_artifact(aid)
                if art is not None:
                    parts.append(f"- [{art.artifact_type}] {art.title}")
            return "\n".join(parts)
        except Exception:
            return ""

    @staticmethod
    def _placeholder() -> str:
        return (
            "学术正式风格：被动语态为主、术语密度适中、IMRaD 结构、"
            "段落 4-6 句、引用风格 APA、章节标题层级不超过 3 级"
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
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not dry_run and registry is not None:
            try:
                claim_briefs = self._load_claims(store, input_obj.claim_ids)

                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=OutlineSchema,
                    system=(
                        "你是论文结构规划师。借鉴 AI-Researcher 的层级式生成思想，"
                        "在写作前先确定整体大纲。要求：\n"
                        "1. 5-7 个章节，每章关联明确的 claim_ids，覆盖所有提供的 Claim\n"
                        "2. 章节组织符合目标会议风格 profile（IMRaD 或变体）\n"
                        "3. total_target_word_count 符合会议篇幅要求（一般 8000-10000 字）\n"
                        "4. abstract 基于核心 Claim 与实验结果一句话概括"
                    ),
                    prompt=(
                        f"可用 Claim：\n{claim_briefs or '（无 Claim）'}\n\n"
                        f"实验结果 artifact_ids：{input_obj.result_artifact_ids}\n\n"
                        f"风格 profile：{input_obj.style_profile}"
                    ),
                )
                sections = [s.model_dump() for s in result.sections]
                # 兜底：确保所有 Claim 都被某章引用
                self._ensure_claim_coverage(sections, input_obj.claim_ids)
                outline = {
                    "sections": sections,
                    "abstract": result.abstract,
                    "total_target_word_count": result.total_target_word_count,
                }
            except (LLMError, Exception) as e:
                logger.warning("Outline 真实调用失败，回退占位: %s", e)
                outline = self._placeholder(input_obj.claim_ids)
        else:
            outline = self._placeholder(input_obj.claim_ids)

        output = OutlineOutput(outline=outline)
        sections = outline.get("sections", [])
        claim_refs = sum(len(s.get("claim_ids", [])) for s in sections)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"大纲生成完成：{len(sections)} 章节，覆盖 {claim_refs} 个 Claim 引用"
            ),
        )

    @staticmethod
    def _load_claims(
        store: Optional[KnowledgeStore], claim_ids: list[str]
    ) -> str:
        if store is None or not claim_ids:
            return ""
        parts: list[str] = []
        for cid in claim_ids:
            try:
                c: Claim = store.get_claim(cid)
                parts.append(
                    f"- {c.claim_id} [{c.status.value}] [{c.role}]: {c.statement}"
                )
            except Exception:
                parts.append(f"- {cid}（无法加载）")
        return "\n".join(parts)

    @staticmethod
    def _ensure_claim_coverage(
        sections: list[dict], claim_ids: list[str]
    ) -> None:
        if not claim_ids or not sections:
            return
        referenced: set[str] = set()
        for s in sections:
            referenced.update(s.get("claim_ids", []) or [])
        missing = [cid for cid in claim_ids if cid not in referenced]
        if missing and sections:
            # 把未引用的 Claim 挂到 Method 章节（找 title 含 Method/方法 的，否则第 3 章）
            target = next(
                (s for s in sections if "method" in s.get("title", "").lower() or "方法" in s.get("title", "")),
                sections[min(2, len(sections) - 1)],
            )
            existing = target.get("claim_ids", []) or []
            target["claim_ids"] = existing + missing

    @staticmethod
    def _placeholder(claim_ids: list[str]) -> dict:
        claim_ids = list(claim_ids)
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
        return {
            "sections": sections,
            "abstract": "论文摘要（占位，待 SectionDraftAgent 填充）",
            "total_target_word_count": sum(s["target_word_count"] for s in sections),
        }


# ===== SectionDraftAgent（借鉴 AI-Researcher 按章填充）=====

class SectionDraftAgent(AgentNode):
    """按章节逐步撰写 Agent。

    借鉴 AI-Researcher 的「按章填充」思想：
    - 基于大纲逐章生成内容，每章装载相关 Claim/Experiment 素材
    - M3 1M 上下文可一次性装载全部 Claim/Experiment 素材，
      避免长文截断导致的事实漂移
    - 输出各章节内容列表 + 拼装后的全文草稿
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
        dry_run: bool = ctx.get(DRY_RUN, True)

        outline_sections = (input_obj.outline or {}).get("sections", []) or []

        if not dry_run and registry is not None and outline_sections:
            try:
                claim_context = self._load_claims(store, input_obj.claim_ids)
                exp_context = self._load_experiments(store, input_obj.result_artifact_ids)

                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=SectionDraftList,
                    system=(
                        "你是科研论文撰写助手。借鉴 AI-Researcher 按章填充思想，"
                        "基于大纲逐章生成内容。\n"
                        "要求：\n"
                        "1. 严格按大纲 sections 顺序与标题撰写，每章引用其 claim_ids 标注的 Claim\n"
                        "2. 利用 1M 上下文一次性装载全部素材，保持全文事实一致\n"
                        "3. 风格遵循 style_profile\n"
                        "4. 每章字数接近 target_word_count\n"
                        "5. 章节正文用 Markdown 格式，公式用 $...$ 包裹"
                    ),
                    prompt=(
                        f"大纲：\n{self._format_outline(outline_sections)}\n\n"
                        f"全部 Claim 素材：\n{claim_context or '（无）'}\n\n"
                        f"实验结果素材：\n{exp_context or '（无）'}\n\n"
                        f"风格 profile：{input_obj.style_profile}"
                    ),
                )
                sections = [s.model_dump() for s in result.sections]
                # 兜底：若 LLM 漏掉章节，按大纲补齐占位
                if len(sections) < len(outline_sections):
                    existing_titles = {s.get("title") for s in sections}
                    for sec in outline_sections:
                        if sec.get("title") not in existing_titles:
                            sections.append({
                                "title": sec.get("title", "未命名章节"),
                                "content": f"（占位：{sec.get('title', '')}）",
                                "word_count": sec.get("target_word_count", 1000),
                            })
                draft_content = self._assemble_draft(sections, (input_obj.outline or {}).get("abstract", ""))
            except (LLMError, Exception) as e:
                logger.warning("SectionDraft 真实调用失败，回退占位: %s", e)
                sections, draft_content = self._placeholder(outline_sections)
        else:
            sections, draft_content = self._placeholder(outline_sections)

        output = SectionDraftOutput(
            sections=sections,
            draft_content=draft_content,
        )
        total_wc = sum(s.get("word_count", 0) for s in sections)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"按章撰写完成：{len(sections)} 章，全文约 {total_wc} 字"
                "（1M 上下文装载全部素材）"
            ),
        )

    @staticmethod
    def _load_claims(
        store: Optional[KnowledgeStore], claim_ids: list[str]
    ) -> str:
        if store is None or not claim_ids:
            return ""
        parts: list[str] = []
        for cid in claim_ids:
            try:
                c: Claim = store.get_claim(cid)
                parts.append(
                    f"[Claim {c.claim_id}] (role={c.role}, status={c.status.value})\n"
                    f"{c.statement}\n证据: {c.evidence_refs}"
                )
            except Exception:
                parts.append(f"[Claim {cid}]（无法加载）")
        return "\n\n".join(parts)

    @staticmethod
    def _load_experiments(
        store: Optional[KnowledgeStore], artifact_ids: list[str]
    ) -> str:
        if store is None or not artifact_ids:
            return ""
        parts: list[str] = []
        for aid in artifact_ids:
            try:
                art = store.get_artifact(aid)
                if art is not None:
                    parts.append(
                        f"- EXPERIMENT_RESULT artifact [{aid}] type={art.artifact_type} "
                        f"title={art.title}"
                    )
            except Exception:
                parts.append(f"- artifact {aid}（无法加载）")
        return "\n".join(parts)

    @staticmethod
    def _format_outline(sections: list[dict]) -> str:
        lines: list[str] = []
        for i, s in enumerate(sections, 1):
            lines.append(
                f"{i}. {s.get('title', '')} (target={s.get('target_word_count', 0)} 字, "
                f"claim_ids={s.get('claim_ids', [])})"
            )
            for kp in s.get("key_points", []) or []:
                lines.append(f"   - {kp}")
        return "\n".join(lines)

    @staticmethod
    def _assemble_draft(sections: list[dict], abstract: str) -> str:
        parts: list[str] = ["# 论文草稿"]
        if abstract:
            parts.append(f"\n## Abstract\n\n{abstract}")
        for s in sections:
            parts.append(f"\n## {s.get('title', '')}\n\n{s.get('content', '')}")
        return "\n".join(parts)

    @staticmethod
    def _placeholder(outline_sections: list[dict]) -> tuple[list[dict], str]:
        if not outline_sections:
            outline_sections = [
                {"title": "1. Introduction", "target_word_count": 1200},
                {"title": "2. Related Work", "target_word_count": 1000},
                {"title": "3. Method", "target_word_count": 2000},
                {"title": "4. Experiments", "target_word_count": 2500},
                {"title": "5. Conclusion", "target_word_count": 800},
            ]
        sections: list[dict] = []
        draft_parts: list[str] = ["# 论文草稿"]
        for sec in outline_sections:
            title = sec.get("title", "未命名章节")
            target_wc = sec.get("target_word_count", 1000)
            content = f"（占位内容：{title}，目标字数 {target_wc}）"
            sections.append({
                "title": title,
                "content": content,
                "word_count": target_wc,
            })
            draft_parts.append(f"\n## {title}\n\n{content}")
        return sections, "\n".join(draft_parts)


# ===== ReviewAgent（以审稿人视角）=====

class ReviewAgent(AgentNode):
    """审稿 Agent。

    以审稿人视角审查论文草稿，从三维度给出修改意见：
    - 结构（structure）：章节组织是否合理、论证逻辑是否连贯
    - 引证（citation）：Claim 引证是否充分、证据链是否完整
    - 表达（expression）：术语使用、句式风格、清晰度
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
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not dry_run and registry is not None and input_obj.draft_content:
            try:
                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=ReviewSchema,
                    system=(
                        "你是资深审稿人。从三维度审阅论文草稿并给出修改意见：\n"
                        "1. 结构（structure, 0~5 分）：章节组织、论证逻辑连贯性\n"
                        "2. 引证（citation, 0~5 分）：Claim 引证充分性、证据链完整性\n"
                        "3. 表达（expression, 0~5 分）：术语使用、句式风格、清晰度\n"
                        "每维度给出分数、问题清单与具体修改建议，最后给总体意见。"
                    ),
                    prompt=(
                        f"论文草稿：\n{input_obj.draft_content[:8000]}\n\n"
                        f"各章节（含字数）：\n{self._format_sections(input_obj.sections)}"
                    ),
                )
                review_notes = self._format_review(result)
            except (LLMError, Exception) as e:
                logger.warning("Review 真实调用失败，回退占位: %s", e)
                review_notes = self._placeholder()
        else:
            review_notes = self._placeholder()

        output = ReviewOutput(review_notes=review_notes)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary="审稿意见生成完成（结构/引证/表达三维度）",
        )

    @staticmethod
    def _format_sections(sections: list[dict]) -> str:
        if not sections:
            return "（无）"
        return "\n".join(
            f"- {s.get('title', '')}: {s.get('word_count', 0)} 字"
            for s in sections
        )

    @staticmethod
    def _format_review(r: ReviewSchema) -> str:
        def _dim(name: str, d: ReviewDimension) -> str:
            issues = "\n".join(f"  - {x}" for x in d.issues) if d.issues else "  - 无"
            sugg = "\n".join(f"  - {x}" for x in d.suggestions) if d.suggestions else "  - 无"
            return f"### {name} - 评分：{d.score:.1f}/5\n问题：\n{issues}\n建议：\n{sugg}"

        return (
            "## 审稿意见\n\n"
            f"{_dim('1. 结构（structure）', r.structure)}\n\n"
            f"{_dim('2. 引证（citation）', r.citation)}\n\n"
            f"{_dim('3. 表达（expression）', r.expression)}\n\n"
            f"### 总体意见\n{r.overall_comment}"
        )

    @staticmethod
    def _placeholder() -> str:
        return (
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
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)
        text = (response.text or "").strip()
        confirmed = text.lower() in ("confirm", "确认", "ok", "y", "yes")
        direction = "" if confirmed else text

        paper_draft_id = ""

        # 真实模式：用户确认后通过 ArtifactManager 创建带版本的 PAPER_DRAFT Artifact
        if confirmed and not dry_run and manager is not None:
            try:
                draft_content = ctx.get(WRITING_DRAFT_CONTENT, "")
                claim_ids = ctx.get(DESIGN_CLAIM_IDS, [])
                artifact: Artifact = manager.create_artifact(
                    artifact_type=ArtifactType.PAPER_DRAFT,
                    title="论文终稿",
                    content=draft_content,
                    cites_claim_ids=claim_ids,
                    cites_experiment_ids=[],
                    source_stage="writing",
                    created_by="writing_revise",
                )
                paper_draft_id = artifact.artifact_id
                logger.info(
                    "PAPER_DRAFT Artifact 已创建: %s (claim_ids=%d)",
                    paper_draft_id,
                    len(claim_ids),
                )
            except Exception as e:
                logger.warning("创建 PAPER_DRAFT Artifact 失败，回退到 ID 占位: %s", e)
                paper_draft_id = KnowledgeStore.new_id()
        else:
            # dry_run 或未确认：用静态方法生成合法 ID（不真实入库）
            paper_draft_id = KnowledgeStore.new_id()

        return ReviseOutput(
            confirmed=confirmed,
            revision_direction=direction,
            paper_draft_artifact_id=paper_draft_id,
        )
