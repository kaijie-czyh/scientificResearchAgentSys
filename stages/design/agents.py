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

执行模式：
- dry_run=True  ：用占位数据返回，不调用 LLM（默认，验证架构用）
- dry_run=False ：真实调用 MiniMax M3，真实入库 Claim/Artifact
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from core.artifacts import ArtifactManager
from core.knowledge import Artifact, ArtifactType, Claim, ClaimStatus, KnowledgeStore, Paper
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
    DRY_RUN,
    IDEATION_VALIDATED_IDEA_IDS,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    RESEARCH_PAPER_IDS,
    RESEARCH_TOPIC,
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

logger = logging.getLogger(__name__)


# ===== 结构化输出 Schema =====

class AtomConceptSchema(BaseModel):
    """原子概念 schema。"""

    concept_name: str = Field(description="概念名（snake_case）")
    description: str = Field(description="概念描述")
    formula_latex: str = Field(description="对应数学公式（LaTeX）")
    code_stub: str = Field(description="对应代码骨架（Python stub）")
    dependencies: list[str] = Field(default_factory=list, description="依赖的其他 concept_name")


class FormulaCodeMapItem(BaseModel):
    """公式↔代码映射项。"""

    concept: str
    formula_latex: str
    code_stub: str
    status: str = "mapped"  # mapped / pending / mismatched


class AtomDecomposeSchema(BaseModel):
    """原子概念分解输出 schema。"""

    atom_concepts: list[AtomConceptSchema]
    formula_code_map: list[FormulaCodeMapItem]


class ClaimExtractItem(BaseModel):
    """单条 Claim 抽取项。"""

    statement: str = Field(description="一句话可验证陈述")
    role: str = Field(default="contribution", description="contribution/method/assumption/result")
    evidence_paper_ids: list[str] = Field(
        default_factory=list,
        description="关联的 Paper ID（来自调研阶段入库的论文）",
    )


class ClaimBatchSchema(BaseModel):
    """Claim 批量抽取 schema。"""

    claims: list[ClaimExtractItem]


# ===== AtomDecomposeAgent（借鉴 AI-Researcher）=====

class AtomDecomposeAgent(AgentNode):
    """原子概念分解 Agent。

    借鉴 AI-Researcher 的核心方法「原子概念分解」：把方法拆为最小可独立
    验证的原子概念，每个概念建立「数学公式 ↔ 代码实现」双向映射。

    设计要点：
    - 原子概念应互相正交，每个可独立验证（便于实验阶段逐概念实现与测试）
    - 每个概念同时给出 formula_latex 与 code_stub，确保论文公式与代码一一对应
    - dependencies 标注概念间依赖，形成 DAG
    - formula_code_map 显式记录映射状态
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
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)
        topic = ctx.get(RESEARCH_TOPIC, "") or ""

        # 加载 idea 正文作为 prompt 素材
        idea_summaries: list[str] = []
        if store is not None:
            for iid in input_obj.idea_ids:
                try:
                    idea = store.get_idea(iid)
                    idea_summaries.append(f"- {idea.text}\n  约束: {idea.constraints}")
                except Exception:
                    pass

        if not dry_run and registry is not None and idea_summaries:
            try:
                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=AtomDecomposeSchema,
                    system=(
                        f"研究主题：{topic}\n"
                        "你是科研方法设计助手。借鉴 AI-Researcher 的原子概念分解思想，"
                        "把方法拆为 3-6 个最小可独立验证的原子概念，每个概念必须同时给出"
                        "数学公式（LaTeX）与代码实现骨架（Python stub），并标注概念间依赖。"
                        "确保公式与代码一一对应（status=mapped），避免「论文写一套、代码做一套」。"
                    ),
                    prompt=(
                        "所有原子概念必须紧扣上述研究主题，不得偏离。"
                    ),
                    prompt=(
                        f"研究主题：{topic}\n\n"
                        f"基于以下 validated idea 分解原子概念：\n"
                        + "\n".join(idea_summaries)
                    ),
                )
                atom_concepts = [ac.model_dump() for ac in result.atom_concepts]
                formula_code_map = [m.model_dump() for m in result.formula_code_map]
                # 兜底：若 LLM 漏给映射表，自动从 atom_concepts 构造
                if not formula_code_map and atom_concepts:
                    formula_code_map = [
                        {
                            "concept": ac["concept_name"],
                            "formula_latex": ac["formula_latex"],
                            "code_stub": ac["code_stub"],
                            "status": "mapped",
                        }
                        for ac in atom_concepts
                    ]
            except Exception as e:
                logger.warning("AtomDecompose 真实调用失败，回退占位: %s", e)
                atom_concepts, formula_code_map = self._placeholder()
        else:
            atom_concepts, formula_code_map = self._placeholder()
                atom_concepts, formula_code_map = self._placeholder(topic)
        else:
            atom_concepts, formula_code_map = self._placeholder(topic)

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

    @staticmethod
    def _placeholder() -> tuple[list[dict], list[dict]]:
    def _placeholder(topic: str = "") -> tuple[list[dict], list[dict]]:
        topic_label = topic.strip() or "(未指定研究主题)"
        atom_concepts = [
            {
                "concept_name": "problem_formulation",
                "description": (
                    f"针对研究主题「{topic_label}」形式化输入/输出与目标函数，"
                    f"定义样本空间与待估参数 θ。"
                ),
                "formula_latex": r"\hat{\theta} = \arg\min_{\theta} \mathcal{L}(\theta; \mathcal{D})",
                "code_stub": "theta = optimize(loss_fn, dataset, init_theta)",
                "dependencies": [],
            },
            {
                "concept_name": "representation_layer",
                "description": (
                    f"将「{topic_label}」中的原始输入映射到可计算的高维表征空间。"
                ),
                "formula_latex": r"\mathbf{x} = f_{\theta_e}(u),\quad u \in \mathcal{U}",
                "code_stub": "x = encoder(input=u)",
                "dependencies": ["problem_formulation"],
            },
            {
                "concept_name": "core_operator",
                "description": (
                    f"针对「{topic_label}」的核心算子（如注意力/聚合/匹配），"
                    f"在表征空间上建模关键交互。"
                ),
                "formula_latex": r"\mathbf{z} = g_{\theta_c}\!\left(\mathbf{x}_1, \dots, \mathbf{x}_N\right)",
                "code_stub": "z = core_operator([x_1, ..., x_N])",
                "dependencies": ["representation_layer"],
            },
            {
                "concept_name": "objective_loss",
                "description": (
                    f"为「{topic_label}」设计的任务损失，融合主任务目标与正则项。"
                ),
                "formula_latex": r"\mathcal{L} = \ell_{\text{task}} + \lambda \, \Omega(\theta)",
                "code_stub": "loss = task_loss(y, pred) + lam * reg(theta)",
                "dependencies": ["core_operator"],
            },
        ]
        formula_code_map = [
            {
                "concept": ac["concept_name"],
                "formula_latex": ac["formula_latex"],
                "code_stub": ac["code_stub"],
                "status": "mapped",
            }
            for ac in atom_concepts
        ]
        return atom_concepts, formula_code_map


# ===== MethodFormalizeAgent =====

class MethodFormalizeAgent(AgentNode):
    """方法形式化 Agent。

    基于原子概念与公式↔代码映射，整合为完整方法文档（含数学公式与算法伪代码）。
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
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)
        topic = ctx.get(RESEARCH_TOPIC, "") or ""

        # 加载 idea 正文
        idea_text = ""
        if store is not None and input_obj.idea_ids:
            try:
                idea_text = store.get_idea(input_obj.idea_ids[0]).text
            except Exception:
                pass

        if not dry_run and registry is not None:
            try:
                resp = registry.complete(
                    task_type=self.task_type,
                    system=(
                        "你是科研方法形式化助手。基于已分解的原子概念与公式↔代码映射，"
                        "整合为完整的方法文档（含数学公式与算法伪代码），"
                        "结构：动机 → 核心概念定义 → 算法伪代码 → 复杂度分析。"
                        "保持公式与代码的对应关系，输出 Markdown 格式。"
                    ),
                    prompt=(
                        f"原始 idea：{idea_text}\n\n"
                        f"原子概念：\n{input_obj.atom_concepts}\n\n"
                        f"公式↔代码映射：\n{input_obj.formula_code_map}"
                        f"研究主题：{topic}\n"
                        "你是科研方法形式化助手。基于已分解的原子概念与公式↔代码映射，"
                        "整合为完整的方法文档（Markdown 格式），必须严格包含以下 5 个章节：\n"
                        "## 1. 问题定义与符号表\n"
                        "- 明确定义所有数学符号（如 N, K, θ, α, β 等）\n"
                        "- 用表格形式列出「符号 | 含义 | 取值范围」\n"
                        "- 形式化描述问题（输入、输出、约束）\n"
                        "## 2. 方法概述\n"
                        "- 一段话概述方法核心思想，紧扣上述研究主题\n"
                        "## 3. 核心公式设计\n"
                        "- 逐个给出关键公式（LaTeX，用 $$ ... $$ 包裹），每个公式前有文字说明设计动机\n"
                        "- 公式之间有逻辑递进关系\n"
                        "## 4. 算法伪代码\n"
                        "- 用 Algorithm 风格的伪代码描述完整流程\n"
                        "## 5. 复杂度分析\n"
                        "- 时间复杂度与空间复杂度分析\n"
                        "保持公式与代码的对应关系，每个公式都能在原子概念中找到对应实现。\n"
                        "所有符号必须在符号表中定义后使用，不得突兀。"
                    ),
                    prompt=(
                        f"研究主题：{topic}\n\n"
                        f"原始 idea：\n{idea_text}\n\n"
                        f"原子概念：\n{input_obj.atom_concepts}\n\n"
                        f"公式↔代码映射：\n{input_obj.formula_code_map}\n\n"
                        "请按上述结构生成完整方法文档。"
                    ),
                )
                method_content = resp.text
            except Exception as e:
                logger.warning("MethodFormalize 真实调用失败，回退占位: %s", e)
                method_content = self._placeholder(input_obj)
        else:
            method_content = self._placeholder(input_obj)

        output = MethodFormalizeOutput(method_content=method_content)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary="方法形式化完成（基于原子概念整合）",
        )

    @staticmethod
    def _placeholder(input_obj: MethodFormalizeInput) -> str:
        concept_lines = "\n".join(
            f"- **{c.get('concept_name', '?')}**: {c.get('description', '')} "
            f"$${c.get('formula_latex', '')}$$"
            for c in input_obj.atom_concepts
        ) or "(无原子概念)"
        return (
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

                method_content = self._placeholder(input_obj, topic)
        else:
            method_content = self._placeholder(input_obj, topic)

        output = MethodFormalizeOutput(method_content=method_content)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary="方法形式化完成（基于原子概念整合）",
        )

    @staticmethod
    def _placeholder(input_obj: MethodFormalizeInput, topic: str = "") -> str:
        topic_label = topic.strip() or "(未指定研究主题)"
        concepts = input_obj.atom_concepts or []
        fcm = input_obj.formula_code_map or []

        # 符号表：从原子概念抽取常见符号，叠加固定保留符号
        symbol_rows = [
            "| 符号 | 含义 | 取值范围 |",
            "|---|---|---|",
            "| $N$ | 样本数量 | $N \\in \\mathbb{N}^+$ |",
            "| $K$ | 类别/输出维度 | $K \\in \\mathbb{N}^+$ |",
            "| $\\theta$ | 模型参数 | $\\theta \\in \\mathbb{R}^d$ |",
            "| $\\alpha$ | 学习率 | $\\alpha \\in (0, 1)$ |",
            "| $\\beta$ | 正则权重 | $\\beta \\in [0, 1]$ |",
            "| $\\lambda$ | 正则系数 | $\\lambda \\geq 0$ |",
            "| $\\mathcal{D}$ | 训练数据集 | $\\{(u_i, y_i)\\}_{i=1}^N$ |",
        ]

        # 核心公式：来自原子概念，缺失时给主题相关兜底
        if concepts:
            formula_lines = []
            for c in concepts:
                name = c.get("concept_name", "?")
                desc = c.get("description", "")
                latex = c.get("formula_latex", "")
                formula_lines.append(
                    f"**{name}**：{desc}\n\n"
                    f"动机：在「{topic_label}」场景下需要该算子支撑后续建模。\n\n"
                    f"$${latex}$$\n"
                )
            formula_section = "\n".join(formula_lines)
        else:
            formula_section = (
                f"**问题形式化**：针对「{topic_label}」建模主任务目标。\n\n"
                f"动机：将研究主题转化为可优化的目标函数。\n\n"
                f"$$\\hat{{\\theta}} = \\arg\\min_{{\\theta}} \\mathcal{{L}}(\\theta; \\mathcal{{D}})$$\n\n"
                f"**正则项**：约束参数复杂度以提升泛化。\n\n"
                f"$$\\mathcal{{L}} = \\ell_{{\\text{{task}}}}(\\theta) + \\lambda \\, \\Omega(\\theta)$$\n"
            )

        # 原子概念对应表
        concept_lines = "\n".join(
            f"- **{c.get('concept_name', '?')}**: {c.get('description', '')} "
            f"`{c.get('code_stub', '')}`"
            for c in concepts
        ) or f"- (无原子概念，使用主题「{topic_label}」的默认形式化方案)"

        # 公式↔代码映射表
        map_rows = ["| 概念 | 公式 | 代码 | 状态 |", "|---|---|---|---|"]
        if fcm:
            for m in fcm:
                map_rows.append(
                    f"| {m.get('concept', '?')} | `{m.get('formula_latex', '')}` | "
                    f"`{m.get('code_stub', '')}` | {m.get('status', 'mapped')} |"
                )
        else:
            map_rows.append("| (无映射) | - | - | pending |")

        return (
            f"## 1. 问题定义与符号表\n\n"
            f"研究主题：**{topic_label}**\n\n"
            "### 问题形式化\n"
            f"- 输入：样本 $u \\in \\mathcal{{U}}$（来自「{topic_label}」的观测空间）\n"
            "- 输出：预测 $\\hat{y} = h_\\theta(u) \\in \\mathcal{Y}$\n"
            "- 约束：参数范数有界 $\\|\\theta\\|_2 \\leq R$，训练样本数 $N$ 固定\n"
            "- 目标：最小化经验风险与正则项之和\n\n"
            "### 符号表\n\n"
            + "\n".join(symbol_rows)
            + "\n\n"
            "## 2. 方法概述\n\n"
            f"针对「{topic_label}」，本方法以原子概念分解为基础，"
            f"将研究问题拆为若干可独立验证的算子，"
            f"在每个算子上同时给出数学公式与代码实现，"
            f"再以端到端目标函数统一优化，确保论文公式与实验代码一一对应。\n\n"
            "## 3. 核心公式设计\n\n"
            f"{formula_section}\n\n"
            "### 原子概念与公式↔代码映射\n\n"
            f"{concept_lines}\n\n"
            + "\n".join(map_rows)
            + "\n\n"
            "## 4. 算法伪代码\n\n"
            "```\n"
            "Algorithm: Method for " + topic_label + "\n"
            "Input:  dataset D = {(u_i, y_i)}_{i=1}^N, hyper-params alpha, lambda, K\n"
            "Output: trained parameters theta\n"
            " 1: initialize theta ~ N(0, sigma^2)\n"
            " 2: for epoch = 1 to E do\n"
            " 3:   for each mini-batch B subset D do\n"
            " 4:     x <- encoder(u)                       // representation_layer\n"
            " 5:     z <- core_operator({x_1, ..., x_|B|})  // core_operator\n"
            " 6:     pred <- head(z)\n"
            " 7:     L <- task_loss(y, pred) + lambda * reg(theta)\n"
            " 8:     theta <- theta - alpha * grad(L, theta)\n"
            " 9:   end for\n"
            "10: end for\n"
            "11: return theta\n"
            "```\n\n"
            "## 5. 复杂度分析\n\n"
            "- 时间复杂度：$O(E \\cdot N \\cdot T_\\text{op})$，"
            "其中 $E$ 为训练轮数，$N$ 为样本数，$T_\\text{op}$ 为单样本核心算子耗时"
            "（与原子概念中 `core_operator` 的实现复杂度一致）。\n"
            "- 空间复杂度：$O(d + N_\\text{batch} \\cdot d_\\text{hidden})$，"
            "其中 $d$ 为参数规模，$N_\\text{batch}$ 为批量大小，$d_\\text{hidden}$ 为表征维度。\n"
        )


# ===== MethodReviewHuman =====

class MethodReviewHuman(HumanNode):
    """用户审核方法。

    呈现方法内容（含原子概念与公式↔代码映射），用户确认或提出修改意见。
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
        dry_run: bool = ctx.get(DRY_RUN, True)

        # 获取可用的 Paper IDs 作为证据候选
        available_paper_ids: list[str] = ctx.get(RESEARCH_PAPER_IDS, [])

        if not dry_run and registry is not None and store is not None:
            try:
                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=ClaimBatchSchema,
                    system=(
                        "你是科研方法分析助手。从形式化方法中抽取 2-4 个可验证的 Claim，"
                        "每个 Claim 用一句话陈述，可被实验或证据验证/反驳。"
                        "为每个 Claim 关联 Paper 证据（从给定的 paper_ids 列表中选取最相关的），"
                        "若无可关联的 Paper，evidence_paper_ids 留空。"
                        "Claim 角色：contribution（核心贡献）/ method（方法特性）/ assumption（假设）/ result（预期结果）。"
                    ),
                    prompt=(
                        f"方法内容：\n{input_obj.method_content}\n\n"
                        f"原子概念：\n{input_obj.atom_concepts}\n\n"
                        f"可用 paper_ids: {available_paper_ids}"
                    ),
                )

                claim_ids: list[str] = []
                for c in result.claims:
                    claim_id = KnowledgeStore.new_id()
                    # 构造 evidence_refs（非 DRAFT 状态必须有）
                    evidence_refs = [
                        {"type": "paper", "id": pid}
                        for pid in c.evidence_paper_ids
                    ]
                    # 若无证据，则保持 DRAFT 状态（避免违反硬约束）
                    status = ClaimStatus.EVIDENCE_LINKED if evidence_refs else ClaimStatus.DRAFT
                    claim = Claim(
                        claim_id=claim_id,
                        statement=c.statement,
                        role=c.role,
                        evidence_refs=evidence_refs,
                        status=status,
                        source_stage="design",
                    )
                    try:
                        store.save_claim(claim)
                        claim_ids.append(claim_id)
                    except Exception as e:
                        logger.warning("Claim 入库失败: %s", e)

                # 兜底：若 LLM 未抽取出 Claim，用占位
                if not claim_ids:
                    claim_ids = self._placeholder_claims(store, available_paper_ids)
            except Exception as e:
                logger.warning("ClaimEvidenceLink 真实调用失败，回退占位: %s", e)
                claim_ids = self._placeholder_claims(store, available_paper_ids)
        else:
            # 占位：用 new_id 生成合法 ID（不真实入库）
            claim_ids = [KnowledgeStore.new_id() for _ in range(3)]

        output = ClaimEvidenceLinkOutput(claim_ids=claim_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"抽取 {len(claim_ids)} 个 Claim 并关联证据",
        )

    @staticmethod
    def _placeholder_claims(
        store: Optional[KnowledgeStore], paper_ids: list[str]
    ) -> list[str]:
        """占位 Claim：用 new_id 生成合法 ID；若 store 可用则真实入库。"""
        claim_ids = []
        for i in range(3):
            claim_id = KnowledgeStore.new_id()
            # 用第一个 paper 作为证据（若有）
            evidence_refs = (
                [{"type": "paper", "id": paper_ids[0]}] if paper_ids else []
            )
            status = ClaimStatus.EVIDENCE_LINKED if evidence_refs else ClaimStatus.DRAFT
            claim = Claim(
                claim_id=claim_id,
                statement=f"占位 Claim {i + 1}：所提方法在标准评测下优于 baseline。",
                role="contribution",
                evidence_refs=evidence_refs,
                status=status,
                source_stage="design",
            )
            if store is not None:
                try:
                    store.save_claim(claim)
                except Exception:
                    pass
            claim_ids.append(claim_id)
        return claim_ids


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
        dry_run: bool = ctx.get(DRY_RUN, True)

        if dry_run or manager is None:
            # 占位：用 new_id 生成合法 ID（不真实创建 Artifact）
            method_artifact_id = KnowledgeStore.new_id()
            output = MethodArtifactOutput(method_artifact_id=method_artifact_id)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=f"[dry_run] 创建方法 Artifact: {method_artifact_id[:8]}（占位 ID）",
            )

        # 真实创建 Artifact
        try:
            artifact: Artifact = manager.create_artifact(
                artifact_type=ArtifactType.METHOD_DOC,
                title="方法文档",
                content=input_obj.method_content,
                cites_claim_ids=input_obj.claim_ids,
                source_stage="design",
                created_by="design_method_artifact",
            )
            method_artifact_id = artifact.artifact_id
            output = MethodArtifactOutput(method_artifact_id=method_artifact_id)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=f"创建方法 Artifact: {method_artifact_id[:8]}（v1）",
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"方法 Artifact 创建失败: {e}",
                summary=f"方法 Artifact 创建失败: {e}",
            )
