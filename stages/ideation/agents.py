"""ideation 阶段 Agent / Human 节点实现。

节点拓扑（借鉴 LangGraph 人在回路）：
    BrainstormAgent（基于调研产出 + 交叉验证报告，针对 gaps/conflicts 生成 3-5 个候选思路）
    → IdeaDiscussHuman（用户交互式探讨：可否决/修正/补充思路）
    → StageCheckpoint
    → IdeaValidateAgent（三维度评估：可行性/新颖性/贡献度）
    → ClaimDraftAgent（从验证通过思路派生 draft Claim，status=DRAFT）

说明：_execute 内 LLM 调用以完整注释范式给出，实际执行用占位数据返回，
既能验证 IO 闭环，又不会产生 API 费用。
敏感思路的内部推理（ideation_private_reasoning）路由到本地/隔离 provider，避免数据外流。
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
    DRY_RUN,
    IDEATION_DISCUSSION_NOTES,
    IDEATION_DRAFT_CLAIM_IDS,
    IDEATION_IDEA_IDS,
    IDEATION_VALIDATED_IDEA_IDS,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    RESEARCH_CROSS_VALIDATION_REPORT,
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

    借鉴 LangGraph 人在回路：Agent 先基于调研可信证据提出候选方案，再交由
    用户审核/修正。思路生成聚焦于：
    - 针对 cross_validation_report.gaps（证据缺口）提出 hypothesis
    - 针对 cross_validation_report.conflicts（未解决冲突）提出调和假设
    - 基于 consensus（共识）提出扩展方向

    敏感思路的内部推理（ideation_private_reasoning）路由到 local provider，
    避免未公开想法外流；交互式发散用 ideation_brainstorm。
    """

    node_type = "ideation_brainstorm"
    task_type = "ideation_brainstorm"
    input_schema = BrainstormInput
    output_schema = BrainstormOutput
    output_keys = {
        "idea_ids": IDEATION_IDEA_IDS,
        # ideas_meta 不写回 context（下游按 id 从 KnowledgeStore 读取正文）
    }

    def _build_input(self, ctx: ExecutionContext) -> BrainstormInput:
        return BrainstormInput(
            paper_ids=ctx.get(RESEARCH_PAPER_IDS, []),
            cross_validation_report=ctx.get(RESEARCH_CROSS_VALIDATION_REPORT, {}),
        )

    def _execute(self, input_obj: BrainstormInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)  # dry_run 时跳过真实 LLM，用占位数据

        report = input_obj.cross_validation_report or {}
        gaps = report.get("gaps", []) or []
        conflicts = report.get("conflicts", []) or []
        consensus = report.get("consensus", []) or []
        paper_ids = input_obj.paper_ids or []

        # === LLM 调用范式（占位，实际未执行）===
        # 思路生成涉及未公开想法，敏感推理先走 ideation_private_reasoning（路由到 local provider）：
        # private_resp = registry.complete(
        #     task_type="ideation_private_reasoning",
        #     prompt=(
        #         f"基于以下调研证据进行内部推演：\n"
        #         f"paper_ids: {paper_ids}\n"
        #         f"gaps: {gaps}\n"
        #         f"conflicts: {conflicts}\n"
        #         f"consensus: {consensus}\n"
        #         "请围绕证据缺口与未解决冲突，推演 3-5 个候选研究假设。"
        #     ),
        # )
        # 再用 ideation_brainstorm 做交互式发散，输出结构化 Idea 列表：
        # from core.llm.base import StructuredOutputRequest
        # class IdeaDraftSchema(BaseModel):
        #     text: str
        #     constraints: list[str]
        #     source_paper_ids: list[str]
        # class BrainstormSchema(BaseModel):
        #     ideas: list[IdeaDraftSchema]
        # result = registry.structured_output(
        #     task_type=self.task_type,
        #     output_schema=BrainstormSchema,
        #     system=(
        #         "你是科研思路生成助手。基于调研的交叉验证报告，"
        #         "针对证据缺口（gaps）与未解决冲突（conflicts）提出可验证的研究假设，"
        #         "每个假设给出约束条件与来源论文。"
        #     ),
        #     prompt=(
        #         f"gaps: {gaps}\n"
        #         f"conflicts: {conflicts}\n"
        #         f"consensus: {consensus}\n"
        #         f"paper_ids: {paper_ids}"
        #     ),
        # )
        # drafts = result.ideas

        # 占位数据：针对 gaps/conflicts/consensus 各提出一个 hypothesis
        # 每条：(text, constraints, source_paper_ids)
        idea_drafts: list[tuple[str, list[str], list[str]]] = []

        if gaps:
            gap0 = gaps[0] if isinstance(gaps[0], str) else str(gaps[0])
            idea_drafts.append((
                f"针对证据缺口「{gap0}」提出假设：设计新方法填补该缺口，"
                "并设计对照实验验证其有效性。",
                ["需在现有公开数据集上可复现", "方法改动应可消融分析"],
                paper_ids[:2],
            ))
        if conflicts:
            c0 = conflicts[0]
            cclaim = c0.get("claim", "某冲突") if isinstance(c0, dict) else str(c0)
            idea_drafts.append((
                f"针对未解决冲突「{cclaim}」提出调和假设：设计统一实验框架，"
                "在相同评测协议下重新检验冲突双方的结论。",
                ["需严格控制变量", "评测协议须公开可复现"],
                paper_ids[:2],
            ))
        if consensus:
            cons0 = consensus[0] if isinstance(consensus[0], str) else str(consensus[0])
            idea_drafts.append((
                f"基于共识「{cons0}」的扩展假设：在已有共识基础上引入新模块，"
                "验证是否能进一步提升性能。",
                ["新模块须有理论依据", "不得破坏原共识成立条件"],
                paper_ids[:3],
            ))
        # 兜底：若报告缺失关键字段，补足至 3 个候选思路
        while len(idea_drafts) < 3:
            idx = len(idea_drafts)
            idea_drafts.append((
                f"占位候选思路 {idx + 1}：基于 {len(paper_ids)} 篇调研论文的扩展研究方向。",
                ["需进一步文献确认新颖性"],
                paper_ids[:2],
            ))

        # 生成 Idea 实体并持久化（下游 IdeaDiscussHuman/IdeaValidateAgent 按 id 读取正文）
        idea_ids: list[str] = []
        ideas_meta: list[dict] = []
        for text, constraints, src_pids in idea_drafts:
            idea_id = KnowledgeStore.new_id()
            idea = Idea(
                idea_id=idea_id,
                text=text,
                constraints=constraints,
                source_paper_ids=src_pids,
                status="draft",
                created_by="agent",
                source_stage="ideation",
            )
            if store is not None:
                try:
                    store.save_idea(idea)
                except Exception:
                    pass
            idea_ids.append(idea_id)
            ideas_meta.append({
                "idea_id": idea_id,
                "text": text,
                "constraints": constraints,
                "source_paper_ids": src_pids,
            })

        output = BrainstormOutput(idea_ids=idea_ids, ideas_meta=ideas_meta)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"生成 {len(idea_ids)} 个候选思路"
                f"（gaps={len(gaps)}, conflicts={len(conflicts)}, consensus={len(consensus)}）"
            ),
        )


# ===== IdeaDiscussHuman =====

class IdeaDiscussHuman(HumanNode):
    """与用户交互式探讨思路（借鉴 LangGraph 人在回路）。

    呈现每个候选思路的 text 与 constraints，用户可：
    - 'ok' 确认全部思路
    - 'reject: <序号>' 否决某个思路（1-based，对应提示中的序号）
    - 'add: <思路描述>' 补充新思路（将创建新 Idea 入库）
    - 自由文本作为讨论笔记（追加到 discussion_notes）

    多条操作可分行输入。否决/补充会更新 idea_ids 列表并写回 context。
    """

    node_type = "ideation_discuss"
    input_schema = NodeInput
    output_schema = IdeaDiscussOutput
    output_keys = {
        "discussion_notes": IDEATION_DISCUSSION_NOTES,
        "idea_ids": IDEATION_IDEA_IDS,  # reject/add 后会变化，需写回
    }

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _fetch_ideas(self, ctx: ExecutionContext) -> list[Idea]:
        """从 KnowledgeStore 按 id 读取思路正文；失败时返回空列表。"""
        idea_ids = ctx.get(IDEATION_IDEA_IDS, [])
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        if not idea_ids or store is None:
            return []
        ideas: list[Idea] = []
        for iid in idea_ids:
            try:
                ideas.append(store.get_idea(iid))
            except Exception:
                pass
        return ideas

    def _render_prompt(self, ctx: ExecutionContext) -> str:
        idea_ids = ctx.get(IDEATION_IDEA_IDS, [])
        ideas = self._fetch_ideas(ctx)
        idea_map = {idea.idea_id: idea for idea in ideas}
        lines: list[str] = []
        for i, iid in enumerate(idea_ids):
            idea = idea_map.get(iid)
            if idea is not None:
                cons = idea.constraints or []
                cons_str = ("；约束：" + " / ".join(cons)) if cons else ""
                lines.append(f"  {i + 1}. {idea.text}{cons_str}")
            else:
                lines.append(f"  {i + 1}. [idea_id={iid}]（正文读取失败）")
        ideas_block = "\n".join(lines) if lines else "  （无候选思路）"
        return (
            f"已生成 {len(idea_ids)} 个候选思路：\n{ideas_block}\n\n"
            "请与系统交互式探讨（可多行输入）：\n"
            "  - 输入 'ok' 确认全部思路\n"
            "  - 输入 'reject: <序号>' 否决某个思路（如 'reject: 2'）\n"
            "  - 输入 'add: <思路描述>' 补充新思路（如 'add: 探索轻量化变体'）\n"
            "  - 其他文本将作为讨论笔记保留"
        )

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        paper_ids = ctx.get(RESEARCH_PAPER_IDS, [])
        idea_ids = list(ctx.get(IDEATION_IDEA_IDS, []))

        text = (response.text or "").strip()
        notes_parts: list[str] = []
        reject_indices: set[int] = set()  # 1-based

        if not text or text.lower() in ("ok", "确认", "y", "yes"):
            notes_parts.append("用户确认全部思路。")
        else:
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                low = line.lower()
                if low.startswith("reject:"):
                    body = line[len("reject:"):].strip()
                    try:
                        idx = int(body)
                        if 1 <= idx <= len(idea_ids):
                            reject_indices.add(idx)
                            notes_parts.append(f"用户否决了思路 #{idx}。")
                        else:
                            notes_parts.append(f"（reject 序号越界：{body}）")
                    except ValueError:
                        notes_parts.append(f"（reject 格式错误：{body}）")
                elif low.startswith("add:"):
                    desc = line[len("add:"):].strip()
                    if desc:
                        new_id = KnowledgeStore.new_id()
                        new_idea = Idea(
                            idea_id=new_id,
                            text=desc,
                            constraints=[],
                            source_paper_ids=paper_ids,
                            status="draft",
                            created_by="user",
                            source_stage="ideation",
                        )
                        if store is not None:
                            try:
                                store.save_idea(new_idea)
                            except Exception:
                                pass
                        idea_ids.append(new_id)
                        notes_parts.append(f"用户补充新思路：{desc}")
                else:
                    notes_parts.append(line)

        # 应用 reject：按 1-based 序号剔除（从大到小删以避免索引错位）
        for idx in sorted(reject_indices, reverse=True):
            try:
                removed = idea_ids.pop(idx - 1)
                # 标记被否决思路状态（便于追溯）
                if store is not None:
                    try:
                        idea = store.get_idea(removed)
                        idea.status = "rejected"
                        store.save_idea(idea)
                    except Exception:
                        pass
            except IndexError:
                pass

        discussion_notes = "\n".join(notes_parts)
        return IdeaDiscussOutput(
            discussion_notes=discussion_notes,
            idea_ids=idea_ids,
        )


# ===== IdeaValidateAgent =====

class IdeaValidateAgent(AgentNode):
    """思路验证 Agent。

    借鉴 LangGraph 人在回路中「Agent 推进」环节：对用户讨论后保留的思路
    做三维度量化评估：
    - feasibility（可行性）：是否有可落地的方法路径与资源
    - novelty（新颖性）：相对已有工作的差异度
    - contribution（贡献度）：潜在学术/工程价值

    三维度均 >= DEFAULT_THRESHOLD 视为通过，写入 validated_idea_ids，
    并把评分写回 Idea.validation_notes、状态置为 'validated'。
    """

    node_type = "ideation_validate"
    task_type = "ideation_validate"
    input_schema = IdeaValidateInput
    output_schema = IdeaValidateOutput
    output_keys = {
        "validated_idea_ids": IDEATION_VALIDATED_IDEA_IDS,
    }

    DEFAULT_THRESHOLD = 0.5

    def _build_input(self, ctx: ExecutionContext) -> IdeaValidateInput:
        return IdeaValidateInput(
            idea_ids=ctx.get(IDEATION_IDEA_IDS, []),
            discussion_notes=ctx.get(IDEATION_DISCUSSION_NOTES, ""),
        )

    def _execute(
        self, input_obj: IdeaValidateInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)  # dry_run 时跳过真实 LLM，用占位数据

        # === LLM 调用范式（占位，实际未执行）===
        # from core.llm.base import StructuredOutputRequest
        # class IdeaValidationSchema(BaseModel):
        #     feasibility: float    # 0~1
        #     novelty: float         # 0~1
        #     contribution: float    # 0~1
        #     reason: str
        # validation_reports = []
        # for idea_id in input_obj.idea_ids:
        #     idea = store.get_idea(idea_id)
        #     resp = registry.structured_output(
        #         task_type=self.task_type,
        #         output_schema=IdeaValidationSchema,
        #         system=(
        #             "你是科研思路评审助手。从可行性、新颖性、贡献度三个维度"
        #             "评估研究思路，各维度给出 0~1 的分数与理由。"
        #         ),
        #         prompt=(
        #             f"思路：{idea.text}\n"
        #             f"约束：{idea.constraints}\n"
        #             f"讨论笔记：{input_obj.discussion_notes}"
        #         ),
        #     )
        #     passed = (
        #         resp.feasibility >= self.DEFAULT_THRESHOLD
        #         and resp.novelty >= self.DEFAULT_THRESHOLD
        #         and resp.contribution >= self.DEFAULT_THRESHOLD
        #     )
        #     validation_reports.append({
        #         "idea_id": idea_id,
        #         "feasibility": resp.feasibility,
        #         "novelty": resp.novelty,
        #         "contribution": resp.contribution,
        #         "passed": passed,
        #         "reason": resp.reason,
        #     })
        #     idea.status = "validated" if passed else "rejected"
        #     idea.validation_notes = {
        #         "feasibility": resp.feasibility,
        #         "novelty": resp.novelty,
        #         "contribution": resp.contribution,
        #         "reason": resp.reason,
        #     }
        #     store.save_idea(idea)

        # 占位数据：每个思路给 0.7/0.6/0.5 的分数（feasibility/novelty/contribution）
        # 三维度均 >= 0.5，全部通过
        validation_reports: list[dict] = []
        validated_idea_ids: list[str] = []
        for idea_id in input_obj.idea_ids:
            feasibility = 0.7
            novelty = 0.6
            contribution = 0.5
            passed = (
                feasibility >= self.DEFAULT_THRESHOLD
                and novelty >= self.DEFAULT_THRESHOLD
                and contribution >= self.DEFAULT_THRESHOLD
            )
            report = {
                "idea_id": idea_id,
                "feasibility": feasibility,
                "novelty": novelty,
                "contribution": contribution,
                "passed": passed,
                "reason": "占位评估：方法路径清晰、与已有工作有差异、具一定贡献度。",
            }
            validation_reports.append(report)
            if passed:
                validated_idea_ids.append(idea_id)
                # 更新 Idea 状态与验证记录
                if store is not None:
                    try:
                        idea = store.get_idea(idea_id)
                        idea.status = "validated"
                        idea.validation_notes = {
                            "feasibility": feasibility,
                            "novelty": novelty,
                            "contribution": contribution,
                            "reason": report["reason"],
                        }
                        store.save_idea(idea)
                    except Exception:
                        pass

        output = IdeaValidateOutput(
            validated_idea_ids=validated_idea_ids,
            validation_reports=validation_reports,
        )
        summary = (
            f"思路验证完成：通过 {len(validated_idea_ids)}/{len(input_obj.idea_ids)}"
            f"（阈值 {self.DEFAULT_THRESHOLD}）"
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=summary,
        )


# ===== ClaimDraftAgent =====

class ClaimDraftAgent(AgentNode):
    """Claim 草稿生成 Agent。

    从验证通过的 Idea 派生 draft Claim（每个 Idea 派生 1-2 个）。
    - statement 为一句话可验证陈述
    - status=ClaimStatus.DRAFT，evidence_refs=[]（草稿阶段无证据，由 design 阶段关联）
    - 派生关系记录到 KnowledgeStore（source_idea_id）

    调用 design_claim_extract task（与 design 阶段共享，但此处产出 DRAFT）。
    """

    node_type = "ideation_claim_draft"
    task_type = "design_claim_extract"
    input_schema = ClaimDraftInput
    output_schema = ClaimDraftOutput
    output_keys = {
        "draft_claim_ids": IDEATION_DRAFT_CLAIM_IDS,
    }

    def _build_input(self, ctx: ExecutionContext) -> ClaimDraftInput:
        return ClaimDraftInput(
            validated_idea_ids=ctx.get(IDEATION_VALIDATED_IDEA_IDS, []),
        )

    def _execute(self, input_obj: ClaimDraftInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)  # dry_run 时跳过真实 LLM，用占位数据

        # === LLM 调用范式（占位，实际未执行）===
        # from core.llm.base import StructuredOutputRequest
        # class ClaimDraftSchema(BaseModel):
        #     statement: str  # 一句话可验证陈述
        # class ClaimDraftListSchema(BaseModel):
        #     claims: list[ClaimDraftSchema]
        # for idea_id in input_obj.validated_idea_ids:
        #     idea = store.get_idea(idea_id)
        #     resp = registry.structured_output(
        #         task_type=self.task_type,
        #         output_schema=ClaimDraftListSchema,
        #         system=(
        #             "你是科研论点提炼助手。从研究思路中派生 1-2 个可验证的 Claim，"
        #             "每个 Claim 用一句话陈述，须可被实验或证据验证/反驳。"
        #         ),
        #         prompt=f"思路：{idea.text}\n约束：{idea.constraints}",
        #     )
        #     for c in resp.claims:
        #         claim_id = KnowledgeStore.new_id()
        #         claim = Claim(
        #             claim_id=claim_id,
        #             statement=c.statement,
        #             source_idea_id=idea_id,
        #             evidence_refs=[],  # 草稿阶段无证据
        #             status=ClaimStatus.DRAFT,
        #         )
        #         store.save_claim(claim)

        # 占位数据：每个验证通过思路派生 1-2 个 draft Claim
        draft_claim_ids: list[str] = []
        claims_meta: list[dict] = []
        for i, idea_id in enumerate(input_obj.validated_idea_ids):
            # 读取思路正文（失败则用占位文本）
            idea_text = f"思路 {idea_id}"
            if store is not None:
                try:
                    idea_text = store.get_idea(idea_id).text
                except Exception:
                    pass
            # 第一个思路派生 2 个 Claim，其余派生 1 个（体现 1-2 范围）
            claim_count = 2 if i == 0 else 1
            for j in range(claim_count):
                claim_id = KnowledgeStore.new_id()
                statement = (
                    f"基于「{idea_text[:40]}」的可验证论断 {j + 1}："
                    "所提方法在标准评测协议下优于现有 baseline。"
                )
                claim = Claim(
                    claim_id=claim_id,
                    statement=statement,
                    source_idea_id=idea_id,
                    evidence_refs=[],  # 草稿阶段无证据
                    status=ClaimStatus.DRAFT,
                )
                if store is not None:
                    try:
                        store.save_claim(claim)
                    except Exception:
                        pass
                draft_claim_ids.append(claim_id)
                claims_meta.append({
                    "claim_id": claim_id,
                    "statement": statement,
                    "source_idea_id": idea_id,
                })

        output = ClaimDraftOutput(
            draft_claim_ids=draft_claim_ids,
            claims_meta=claims_meta,
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"从 {len(input_obj.validated_idea_ids)} 个验证思路派生 "
                f"{len(draft_claim_ids)} 个 draft Claim"
            ),
        )
