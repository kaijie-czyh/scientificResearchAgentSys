"""ideation 阶段 Agent / Human 节点实现。

节点拓扑：
    BrainstormAgent → IdeaDiscussHuman → StageCheckpoint → IdeaValidateAgent → ClaimDraftAgent

敏感思路的内部推理（ideation_private_reasoning）路由到本地模型，避免数据外流。
"""
from __future__ import annotations

from typing import Optional

from core.knowledge import Claim, ClaimStatus, Idea, KnowledgeStore
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
)

from stages.common import (
    IDEATION_DISCUSSION_NOTES,
    IDEATION_DRAFT_CLAIM_IDS,
    IDEATION_IDEA_IDS,
    IDEATION_VALIDATED_IDEA_IDS,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    RESEARCH_PAPER_IDS,
)
from stages.ideation.io_schema import (
    BrainstormInput,
    BrainstormOutput,
    ClaimDraftInput,
    ClaimDraftOutput,
    IdeaDiscussOutput,
    IdeaValidateInput,
    IdeaValidateOutput,
)


# ===== BrainstormAgent =====

class BrainstormAgent(AgentNode):
    """思路生成 Agent。

    调用 ideation_brainstorm，基于 paper_ids 生成候选思路。
    """

    node_type = "ideation_brainstorm"
    task_type = "ideation_brainstorm"
    input_schema = BrainstormInput
    output_schema = BrainstormOutput
    output_keys = {
        "idea_ids": IDEATION_IDEA_IDS,
    }

    def _build_input(self, ctx: ExecutionContext) -> BrainstormInput:
        paper_ids = ctx.get(RESEARCH_PAPER_IDS, [])
        return BrainstormInput(paper_ids=paper_ids)

    def _execute(self, input_obj: BrainstormInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)

        # === LLM 调用范式（占位，实际未执行）===
        # 思路生成涉及未公开想法，敏感推理走本地模型：
        # resp = registry.complete(
        #     task_type="ideation_private_reasoning",  # 路由到 local provider
        #     prompt=f"基于以下论文 ID 生成候选研究思路：{input_obj.paper_ids}",
        # )
        # 再用 ideation_brainstorm 做交互式发散：
        # resp = registry.complete(
        #     task_type=self.task_type,
        #     prompt=f"论文：{input_obj.paper_ids}\n请生成 3-5 个候选思路。",
        # )

        # 占位数据：生成候选 Idea 并入库（范式注释）
        idea_ids = []
        for i in range(3):
            idea_id = KnowledgeStore.new_id()
            # idea = Idea(
            #     idea_id=idea_id,
            #     text=f"占位思路 {i + 1}",
            #     source_paper_ids=input_obj.paper_ids,
            #     status="draft",
            # )
            # store.save_idea(idea)
            idea_ids.append(idea_id)

        output = BrainstormOutput(idea_ids=idea_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"生成 {len(idea_ids)} 个候选思路",
        )


# ===== IdeaDiscussHuman =====

class IdeaDiscussHuman(HumanNode):
    """与用户讨论思路。

    呈现候选思路，收集用户反馈与讨论笔记。
    """

    node_type = "ideation_discuss"
    input_schema = NodeInput
    output_schema = IdeaDiscussOutput
    output_keys = {
        "discussion_notes": IDEATION_DISCUSSION_NOTES,
    }

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _render_prompt(self, ctx: ExecutionContext) -> str:
        idea_ids = ctx.get(IDEATION_IDEA_IDS, [])
        return (
            f"已生成 {len(idea_ids)} 个候选思路（ID: {idea_ids}）。\n"
            "请与系统讨论这些思路的可行性、新颖性，输入你的反馈："
        )

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        notes = response.text or ""
        return IdeaDiscussOutput(discussion_notes=notes)


# ===== IdeaValidateAgent =====

class IdeaValidateAgent(AgentNode):
    """思路验证 Agent。

    调用 ideation_validate 验证思路可行性/新颖性。
    """

    node_type = "ideation_validate"
    task_type = "ideation_validate"
    input_schema = IdeaValidateInput
    output_schema = IdeaValidateOutput
    output_keys = {
        "validated_idea_ids": IDEATION_VALIDATED_IDEA_IDS,
    }

    def _build_input(self, ctx: ExecutionContext) -> IdeaValidateInput:
        idea_ids = ctx.get(IDEATION_IDEA_IDS, [])
        notes = ctx.get(IDEATION_DISCUSSION_NOTES, "")
        return IdeaValidateInput(idea_ids=idea_ids, discussion_notes=notes)

    def _execute(self, input_obj: IdeaValidateInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)

        # === LLM 调用范式（占位，实际未执行）===
        # for idea_id in input_obj.idea_ids:
        #     resp = registry.structured_output(
        #         task_type=self.task_type,
        #         output_schema=IdeaValidationSchema,
        #         prompt=f"思路 ID：{idea_id}\n讨论笔记：{input_obj.discussion_notes}\n"
        #                "请评估可行性、新颖性、贡献度。",
        #     )
        #     # 更新 Idea 状态为 validated
        #     idea = store.get_idea(idea_id)
        #     idea.status = "validated"
        #     idea.validation_notes = resp.model_dump()
        #     store.save_idea(idea)

        # 占位数据：全部通过验证
        validated_idea_ids = list(input_obj.idea_ids)

        output = IdeaValidateOutput(validated_idea_ids=validated_idea_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"验证通过 {len(validated_idea_ids)} 个思路",
        )


# ===== ClaimDraftAgent =====

class ClaimDraftAgent(AgentNode):
    """Claim 草稿生成 Agent。

    从验证通过的 Idea 派生 draft Claim（调用 design_claim_extract，但 status=DRAFT）。
    """

    node_type = "ideation_claim_draft"
    task_type = "design_claim_extract"
    input_schema = ClaimDraftInput
    output_schema = ClaimDraftOutput
    output_keys = {
        "draft_claim_ids": IDEATION_DRAFT_CLAIM_IDS,
    }

    def _build_input(self, ctx: ExecutionContext) -> ClaimDraftInput:
        validated_idea_ids = ctx.get(IDEATION_VALIDATED_IDEA_IDS, [])
        return ClaimDraftInput(validated_idea_ids=validated_idea_ids)

    def _execute(self, input_obj: ClaimDraftInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)

        # === LLM 调用范式（占位，实际未执行）===
        # for idea_id in input_obj.validated_idea_ids:
        #     resp = registry.structured_output(
        #         task_type=self.task_type,
        #         output_schema=ClaimDraftSchema,
        #         prompt=f"从思路 {idea_id} 派生可验证的 Claim 草稿。",
        #     )
        #     claim_id = KnowledgeStore.new_id()
        #     claim = Claim(
        #         claim_id=claim_id,
        #         statement=resp.statement,
        #         source_idea_id=idea_id,
        #         evidence_refs=[],  # 草稿阶段无证据
        #         status=ClaimStatus.DRAFT,
        #     )
        #     store.save_claim(claim)

        # 占位数据
        draft_claim_ids = [KnowledgeStore.new_id() for _ in input_obj.validated_idea_ids]

        output = ClaimDraftOutput(draft_claim_ids=draft_claim_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"派生 {len(draft_claim_ids)} 个 draft Claim",
        )
