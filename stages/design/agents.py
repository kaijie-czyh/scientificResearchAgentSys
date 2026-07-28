"""design 阶段 Agent / Human 节点实现。

节点拓扑（借鉴 AI-Researcher 原子概念分解）：
    AtomDecomposeAgent（AI-Researcher：原子概念分解，建立公式↔代码映射）
    → MethodFormalizeAgent（将方法形式化为公式与伪代码）
    → StageCheckpoint
    → MethodReviewHuman（用户确认方法）
    → ClaimEvidenceLinkAgent（抽取 Claim 并关联证据）
    → MethodArtifactAgent（生成方法文档 Artifact）

核心思想（AI-Researcher）：把方法拆为最小可独立验证的原子概念，
每个概念建立「数学公式 ↔ 代码实现」双向映射，确保论文中的公式与
实验代码一一对应，避免「论文写一套、代码做一套」。

说明：_execute 内 LLM 调用以完整注释范式给出，实际执行用占位数据返回，
既能验证 IO 闭环，又不会产生 API 费用。
"""
from __future__ import annotations

from typing import Optional

from core.artifacts import ArtifactManager
from core.knowledge import Artifact, ArtifactType, Claim, ClaimStatus, KnowledgeStore
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
    ARTIFACT_MANAGER,
    DESIGN_ATOM_CONCEPTS,
    DESIGN_CLAIM_IDS,
    DESIGN_FORMULA_CODE_MAP,
    DESIGN_METHOD_ARTIFACT_ID,
    DESIGN_METHOD_CONTENT,
    IDEATION_VALIDATED_IDEA_IDS,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
)
from stages.design.io_schema import (
    AtomDecomposeInput,
    AtomDecomposeOutput,
    ClaimEvidenceLinkInput,
    ClaimEvidenceLinkOutput,
    MethodArtifactInput,
    MethodArtifactOutput,
    MethodFormalizeInput,
    MethodFormalizeOutput,
    MethodReviewOutput,
)


# ===== AtomDecomposeAgent（借鉴 AI-Researcher）=====

class AtomDecomposeAgent(AgentNode):
    """原子概念分解 Agent。

    借鉴 AI-Researcher 的核心方法「原子概念分解」：把方法拆为最小可独立
    验证的原子概念，每个概念建立「数学公式 ↔ 代码实现」双向映射。

    设计要点：
    - 原子概念应互相正交，每个可独立验证（便于实验阶段逐概念实现与测试）
    - 每个概念同时给出 formula_latex（论文中将出现的公式）与 code_stub
      （实验代码骨架），确保论文公式与代码一一对应
    - dependencies 标注概念间依赖，形成 DAG，便于实验阶段按序实现
    - formula_code_map 显式记录映射状态（mapped/pending/mismatched），
      status=mismatched 时需人工介入对齐公式与代码

    输入：ideation 阶段传入的 validated idea（IDEATION_VALIDATED_IDEA_IDS）
    输出：原子概念列表（DESIGN_ATOM_CONCEPTS）+ 公式↔代码映射表（DESIGN_FORMULA_CODE_MAP）
    """

    node_type = "design_atom_decompose"
    task_type = "design_atom_decompose"
    input_schema = AtomDecomposeInput
    output_schema = AtomDecomposeOutput
    output_keys = {
        "atom_concepts": DESIGN_ATOM_CONCEPTS,
        "formula_code_map": DESIGN_FORMULA_CODE_MAP,
    }

    def _build_input(self, ctx: ExecutionContext) -> AtomDecomposeInput:
        idea_ids = ctx.get(IDEATION_VALIDATED_IDEA_IDS, [])
        return AtomDecomposeInput(idea_ids=idea_ids)

    def _execute(
        self, input_obj: AtomDecomposeInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 AI-Researcher：让 LLM 把方法分解为原子概念并建立公式↔代码双向映射
        # from pydantic import BaseModel, Field
        # class AtomConceptSchema(BaseModel):
        #     concept_name: str
        #     description: str
        #     formula_latex: str
        #     code_stub: str
        #     dependencies: list[str] = Field(default_factory=list)
        # class FormulaCodeMapItem(BaseModel):
        #     concept: str
        #     formula_latex: str
        #     code_stub: str
        #     status: str  # mapped / pending / mismatched
        # class AtomDecomposeSchema(BaseModel):
        #     atom_concepts: list[AtomConceptSchema]
        #     formula_code_map: list[FormulaCodeMapItem]
        # result = registry.structured_output(
        #     task_type=self.task_type,
        #     output_schema=AtomDecomposeSchema,
        #     system=(
        #         "你是科研方法设计助手。借鉴 AI-Researcher 的原子概念分解思想，"
        #         "把方法拆为最小可独立验证的原子概念，每个概念必须同时给出"
        #         "数学公式（LaTeX）与代码实现骨架（Python stub），并标注概念间依赖。"
        #         "确保公式与代码一一对应（status=mapped），避免「论文写一套、代码做一套」。"
        #     ),
        #     prompt=(
        #         f"基于以下 validated idea 分解原子概念：\n"
        #         f"idea_ids: {input_obj.idea_ids}"
        #     ),
        # )
        # atom_concepts = [ac.model_dump() for ac in result.atom_concepts]
        # formula_code_map = [m.model_dump() for m in result.formula_code_map]

        # 占位数据：3 个原子概念，覆盖 embedding → attention → aggregation 链路
        atom_concepts = [
            {
                "concept_name": "input_embedding",
                "description": "将输入 token 映射到高维向量空间",
                "formula_latex": r"\mathbf{x} = W_e \, \text{onehot}(t)",
                "code_stub": "x = embedding_layer(token_ids)",
                "dependencies": [],
            },
            {
                "concept_name": "attention_weight",
                "description": "计算 query 与 key 的相似度并归一化为注意力权重",
                "formula_latex": r"\alpha = \mathrm{softmax}\!\left(\frac{Q K^{\top}}{\sqrt{d_k}}\right)",
                "code_stub": "alpha = softmax(Q @ K.T / sqrt(d_k))",
                "dependencies": ["input_embedding"],
            },
            {
                "concept_name": "context_vector",
                "description": "按注意力权重加权求和 value 得到上下文向量",
                "formula_latex": r"\mathbf{c} = \sum_{i} \alpha_i \, V_i",
                "code_stub": "c = alpha @ V",
                "dependencies": ["attention_weight"],
            },
        ]
        # 公式↔代码映射表：与原子概念一一对应，均标记为 mapped
        formula_code_map = [
            {
                "concept": ac["concept_name"],
                "formula_latex": ac["formula_latex"],
                "code_stub": ac["code_stub"],
                "status": "mapped",
            }
            for ac in atom_concepts
        ]

        output = AtomDecomposeOutput(
            atom_concepts=atom_concepts,
            formula_code_map=formula_code_map,
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"原子概念分解完成：{len(atom_concepts)} 个概念，"
                f"{len(formula_code_map)} 条公式↔代码映射"
            ),
        )


# ===== MethodFormalizeAgent =====

class MethodFormalizeAgent(AgentNode):
    """方法形式化 Agent。

    基于原子概念与公式↔代码映射，调用 design_method_formalize 整合为
    完整方法文档（含数学公式与算法伪代码）。

    设计要点：
    - 整合原子概念为连贯的方法叙述（动机→定义→算法→复杂度）
    - 保留公式↔代码映射，便于写作与实验阶段溯源到具体概念
    - 输出 method_content 供后续用户审核与 Claim 抽取
    """

    node_type = "design_method_formalize"
    task_type = "design_method_formalize"
    input_schema = MethodFormalizeInput
    output_schema = MethodFormalizeOutput
    output_keys = {
        "method_content": DESIGN_METHOD_CONTENT,
    }

    def _build_input(self, ctx: ExecutionContext) -> MethodFormalizeInput:
        return MethodFormalizeInput(
            idea_ids=ctx.get(IDEATION_VALIDATED_IDEA_IDS, []),
            atom_concepts=ctx.get(DESIGN_ATOM_CONCEPTS, []),
            formula_code_map=ctx.get(DESIGN_FORMULA_CODE_MAP, []),
        )

    def _execute(
        self, input_obj: MethodFormalizeInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 AI-Researcher：基于原子概念与公式↔代码映射，整合为完整方法文档
        # resp = registry.complete(
        #     task_type=self.task_type,
        #     system=(
        #         "你是科研方法形式化助手。基于已分解的原子概念与公式↔代码映射，"
        #         "整合为完整的方法文档（含数学公式与算法伪代码），"
        #         "保持公式与代码的对应关系。"
        #     ),
        #     prompt=(
        #         f"Idea IDs: {input_obj.idea_ids}\n"
        #         f"原子概念：\n{input_obj.atom_concepts}\n"
        #         f"公式↔代码映射：\n{input_obj.formula_code_map}"
        #     ),
        # )
        # method_content = resp.text

        # 占位数据：将原子概念整合为方法文档
        concept_lines = "\n".join(
            f"- **{c.get('concept_name', '?')}**: {c.get('description', '')} "
            f"$${c.get('formula_latex', '')}$$"
            for c in input_obj.atom_concepts
        ) or "(无原子概念)"
        method_content = (
            "## 方法\n\n"
            "### 动机\n基于 validated idea 形式化方法。\n\n"
            "### 核心概念\n"
            f"{concept_lines}\n\n"
            "### 算法伪代码\n```\n"
            "for batch in dataloader:\n"
            "    x = embedding_layer(batch.tokens)\n"
            "    alpha = softmax(Q @ K.T / sqrt(d_k))\n"
            "    c = alpha @ V\n"
            "    output = output_layer(c)\n"
            "```\n"
        )

        output = MethodFormalizeOutput(method_content=method_content)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary="方法形式化完成（基于原子概念整合）",
        )


# ===== MethodReviewHuman =====

class MethodReviewHuman(HumanNode):
    """用户审核方法。

    呈现方法内容（含原子概念与公式↔代码映射），用户确认或提出修改意见。
    借鉴 AI-Researcher：在进入实验阶段前给用户最后干预机会，
    确保方法形式化与用户预期一致。
    """

    node_type = "design_method_review"
    input_schema = NodeInput
    output_schema = MethodReviewOutput
    output_keys: dict = {}

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _render_prompt(self, ctx: ExecutionContext) -> str:
        method = ctx.get(DESIGN_METHOD_CONTENT, "")
        preview = method[:500] if method else "(空)"
        return (
            "方法形式化结果如下：\n"
            f"{preview}\n\n"
            "请审核：\n"
            "  - 输入 'approve' 确认通过\n"
            "  - 或输入修改意见"
        )

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        text = (response.text or "").strip()
        approved = text.lower() in ("approve", "通过", "ok", "y", "yes")
        comments = "" if approved else text
        return MethodReviewOutput(approved=approved, review_comments=comments)


# ===== ClaimEvidenceLinkAgent =====

class ClaimEvidenceLinkAgent(AgentNode):
    """Claim 抽取与证据关联 Agent。

    借鉴 AI-Researcher：从形式化方法中抽取可验证的 Claim，并为每个 Claim
    关联 Paper/Experiment 证据，状态置为 EVIDENCE_LINKED。

    设计要点：
    - Claim 是论文的核心组成单元，每条 Claim 必须可验证（一句话陈述）
    - 从方法内容抽取的 Claim 角色：contribution / method / assumption / result
    - evidence_refs 指向 Paper（research 阶段入库）或 Experiment（experiment 阶段产出）
    - 非 DRAFT 状态的 Claim 必须有 evidence_refs（由 Claim schema 硬约束）
    """

    node_type = "design_claim_evidence_link"
    task_type = "design_claim_extract"
    input_schema = ClaimEvidenceLinkInput
    output_schema = ClaimEvidenceLinkOutput
    output_keys = {
        "claim_ids": DESIGN_CLAIM_IDS,
    }

    def _build_input(self, ctx: ExecutionContext) -> ClaimEvidenceLinkInput:
        return ClaimEvidenceLinkInput(
            method_content=ctx.get(DESIGN_METHOD_CONTENT, ""),
            atom_concepts=ctx.get(DESIGN_ATOM_CONCEPTS, []),
        )

    def _execute(
        self, input_obj: ClaimEvidenceLinkInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 AI-Researcher：从形式化方法中抽取 Claim 并关联证据
        # from pydantic import BaseModel, Field
        # class ClaimExtractSchema(BaseModel):
        #     statement: str
        #     role: str  # contribution / method / assumption / result
        #     evidence_refs: list[dict[str, str]] = Field(default_factory=list)
        # class ClaimBatchSchema(BaseModel):
        #     claims: list[ClaimExtractSchema]
        # result = registry.structured_output(
        #     task_type=self.task_type,
        #     output_schema=ClaimBatchSchema,
        #     system=(
        #         "你是科研方法分析助手。从形式化方法中抽取可验证的 Claim，"
        #         "并为每个 Claim 关联 Paper/Experiment 证据。"
        #         "每条 evidence_ref 形如 {\"type\": \"paper\"/\"experiment\", \"id\": \"...\"}。"
        #     ),
        #     prompt=(
        #         f"方法内容：\n{input_obj.method_content}\n"
        #         f"原子概念：\n{input_obj.atom_concepts}"
        #     ),
        # )
        # claim_ids = []
        # for c in result.claims:
        #     claim_id = KnowledgeStore.new_id()
        #     claim = Claim(
        #         claim_id=claim_id,
        #         statement=c.statement,
        #         role=c.role,
        #         evidence_refs=c.evidence_refs,
        #         status=ClaimStatus.EVIDENCE_LINKED,
        #         source_stage="design",
        #     )
        #     store.save_claim(claim)
        #     claim_ids.append(claim_id)

        # 占位数据：从方法中抽取 3 个 Claim，用 new_id 生成合法 ID
        claim_ids = [KnowledgeStore.new_id() for _ in range(3)]

        output = ClaimEvidenceLinkOutput(claim_ids=claim_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"抽取 {len(claim_ids)} 个 Claim 并关联证据",
        )


# ===== MethodArtifactAgent =====

class MethodArtifactAgent(AgentNode):
    """方法 Artifact 生成 Agent。

    创建 METHOD_DOC 类型 Artifact，cites_claim_ids。
    作为 design 阶段的最终产出物，供 experiment/writing 阶段引用。
    """

    node_type = "design_method_artifact"
    task_type = "design_method_formalize"
    input_schema = MethodArtifactInput
    output_schema = MethodArtifactOutput
    output_keys = {
        "method_artifact_id": DESIGN_METHOD_ARTIFACT_ID,
    }

    def _build_input(self, ctx: ExecutionContext) -> MethodArtifactInput:
        return MethodArtifactInput(
            claim_ids=ctx.get(DESIGN_CLAIM_IDS, []),
            method_content=ctx.get(DESIGN_METHOD_CONTENT, ""),
        )

    def _execute(
        self, input_obj: MethodArtifactInput, ctx: ExecutionContext
    ) -> NodeResult:
        manager: Optional[ArtifactManager] = ctx.get(ARTIFACT_MANAGER)

        # === Artifact 创建范式（占位，实际未执行）===
        # artifact: Artifact = manager.create_artifact(
        #     artifact_type=ArtifactType.METHOD_DOC,
        #     title="方法文档",
        #     content=input_obj.method_content,
        #     cites_claim_ids=input_obj.claim_ids,
        #     source_stage="design",
        #     created_by="design_method_artifact",
        # )
        # method_artifact_id = artifact.artifact_id

        # 占位数据：用 new_id 生成合法 ID（静态方法，无需 DB 实例）
        method_artifact_id = KnowledgeStore.new_id()

        output = MethodArtifactOutput(method_artifact_id=method_artifact_id)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"创建方法 Artifact: {method_artifact_id[:8]}",
        )
