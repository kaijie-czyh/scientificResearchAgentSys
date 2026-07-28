"""experiment 阶段 Agent / Tool 节点实现。

节点拓扑（借鉴 AI-Researcher 的「导师-学生迭代」核心方法）：
    ExperimentConfigAgent（生成实验配置：数据集/baseline/超参）
    → CodeGenerateAgent（AI-Researcher Code Agent：DeepSeek 生成实验代码）
    → CodeReviewAgent（AI-Researcher Advisor Agent：审查代码，可多轮迭代）
    → StageCheckpoint
    → ExperimentRunTool（执行实验，ToolNode）
    → AnomalyCheckAgent（检测异常：loss spike/NaN/不收敛）
    → ClaimVerifyAgent（用实验结果验证 Claim）

说明：
- CodeGenerateAgent 调用 experiment_code_generate（provider=deepseek，编程最强）。
- CodeReviewAgent 调用 experiment_code_review（provider=minimax）。
- 导师-学生迭代语义：CodeReviewAgent 审查不通过时，理论上应回到 CodeGenerateAgent
  重新生成。由于 graph 是 DAG 不支持环，实际多轮迭代由 GraphRunner 外部循环驱动
  （重跑 CodeGenerate→CodeReview 子链），或在 CodeReviewAgent 内部循环 MAX_REVIEW_ROUNDS 次。
  此处采用「内部循环」范式：_execute 内对当前代码循环审查，未通过时模拟 Code Agent
  修正（占位），最终输出累计 review_notes + passed 标志。
- _execute 内 LLM 调用以完整注释范式给出，实际执行用占位数据返回，
  既能验证 IO 闭环，又不会产生 API 费用。
"""
from __future__ import annotations

from typing import Optional

from core.artifacts import ArtifactManager
from core.knowledge import (
    ArtifactType,
    Claim,
    ClaimStatus,
    Experiment,
    ExperimentStatus,
    KnowledgeStore,
)
from core.llm import LLMRegistry
from core.orchestration.context import ExecutionContext
from core.orchestration.node import (
    AgentNode,
    NodeResult,
    NodeStatus,
    ToolNode,
)

from stages.common import (
    ARTIFACT_MANAGER,
    DESIGN_CLAIM_IDS,
    DESIGN_FORMULA_CODE_MAP,
    DESIGN_METHOD_ARTIFACT_ID,
    EXPERIMENT_ANOMALY_REPORT,
    EXPERIMENT_CODE,
    EXPERIMENT_CONFIGS,
    EXPERIMENT_IDS,
    EXPERIMENT_RESULT_ARTIFACT_IDS,
    EXPERIMENT_REVIEW_NOTES,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
)
from stages.experiment.io_schema import (
    AnomalyCheckInput,
    AnomalyCheckOutput,
    ClaimVerifyInput,
    ClaimVerifyOutput,
    CodeGenerateInput,
    CodeGenerateOutput,
    CodeReviewInput,
    CodeReviewOutput,
    ExperimentConfigInput,
    ExperimentConfigOutput,
    ExperimentRunInput,
    ExperimentRunOutput,
)


# ===== ExperimentConfigAgent =====

class ExperimentConfigAgent(AgentNode):
    """实验配置生成 Agent。

    调用 experiment_config_generate，根据 claim_ids + method artifact 生成实验配置
    （数据集、baseline、超参）。配置后续传给 CodeGenerateAgent 作为代码生成的上下文。
    """

    node_type = "experiment_config"
    task_type = "experiment_config_generate"
    input_schema = ExperimentConfigInput
    output_schema = ExperimentConfigOutput
    output_keys = {
        "configs": EXPERIMENT_CONFIGS,
    }

    def _build_input(self, ctx: ExecutionContext) -> ExperimentConfigInput:
        claim_ids = ctx.get(DESIGN_CLAIM_IDS, [])
        method_artifact_id = ctx.get(DESIGN_METHOD_ARTIFACT_ID, "")
        return ExperimentConfigInput(
            claim_ids=claim_ids,
            method_artifact_id=method_artifact_id,
        )

    def _execute(self, input_obj: ExperimentConfigInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)

        # === LLM 调用范式（占位，实际未执行）===
        # from core.llm.base import StructuredOutputRequest
        # class ExperimentConfigSchema(BaseModel):
        #     configs: list[dict]
        # result = registry.structured_output(
        #     task_type=self.task_type,
        #     output_schema=ExperimentConfigSchema,
        #     system=(
        #         "你是实验设计助手。根据 Claim 与方法 Artifact 生成实验配置，"
        #         "每个配置含 name/dataset/baseline/hyperparams/verifies_claim_ids。"
        #     ),
        #     prompt=(
        #         f"Claim IDs: {input_obj.claim_ids}\n"
        #         f"Method Artifact: {input_obj.method_artifact_id}"
        #     ),
        # )
        # configs = result.configs

        # 占位数据
        configs = [
            {
                "name": f"exp_{i + 1}",
                "dataset": "placeholder_dataset",
                "baseline": "random",
                "hyperparams": {"lr": 1e-3, "epochs": 10},
                "verifies_claim_ids": input_obj.claim_ids,
            }
            for i in range(min(2, max(1, len(input_obj.claim_ids))))
        ]

        output = ExperimentConfigOutput(configs=configs)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"生成 {len(configs)} 组实验配置",
        )


# ===== CodeGenerateAgent（借鉴 AI-Researcher Code Agent）=====

class CodeGenerateAgent(AgentNode):
    """实验代码生成 Agent（AI-Researcher Code Agent，学生角色）。

    借鉴 AI-Researcher 的 Code Agent：根据实验配置 + design 阶段产生的公式↔代码映射
    （DESIGN_FORMULA_CODE_MAP），生成忠实实现论文方法的实验代码。

    设计要点：
    - task_type = experiment_code_generate，provider=deepseek（编程最强）
    - 输入：实验配置（数据集/baseline/超参） + 公式↔代码映射
      （Code Agent 据此把每个公式落地为代码片段，确保代码忠实实现方法）
    - 输出：实验代码 {path, content, language}
    - 在导师-学生迭代中扮演「学生」：根据 Advisor 的审查意见修正代码

    迭代语义：本节点为单次生成；多轮修正由 GraphRunner 外部循环驱动重跑，
    或在 CodeReviewAgent 内部模拟。本节点只负责「根据当前输入生成代码」。
    """

    node_type = "experiment_code_generate"
    task_type = "experiment_code_generate"
    input_schema = CodeGenerateInput
    output_schema = CodeGenerateOutput
    output_keys = {
        "code": EXPERIMENT_CODE,
    }

    def _build_input(self, ctx: ExecutionContext) -> CodeGenerateInput:
        return CodeGenerateInput(
            configs=ctx.get(EXPERIMENT_CONFIGS, []),
            formula_code_map=ctx.get(DESIGN_FORMULA_CODE_MAP, []),
        )

    def _execute(self, input_obj: CodeGenerateInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 AI-Researcher Code Agent：把公式↔代码映射作为强约束喂给 LLM
        # from core.llm.base import StructuredOutputRequest
        # class CodeArtifactSchema(BaseModel):
        #     path: str
        #     content: str
        #     language: str = "python"
        # formula_blocks = "\n".join(
        #     f"- 概念: {m.get('concept')}\n"
        #     f"  公式: {m.get('formula_latex')}\n"
        #     f"  代码骨架: {m.get('code_stub')}\n"
        #     f"  状态: {m.get('status')}"
        #     for m in input_obj.formula_code_map
        # )
        # config_blocks = "\n".join(
        #     f"- {c.get('name')}: dataset={c.get('dataset')}, "
        #     f"baseline={c.get('baseline')}, hyperparams={c.get('hyperparams')}"
        #     for c in input_obj.configs
        # )
        # result = registry.structured_output(
        #     task_type=self.task_type,
        #     output_schema=CodeArtifactSchema,
        #     system=(
        #         "你是实验代码工程师。根据实验配置与公式↔代码映射，"
        #         "生成完整可运行的实验代码。每个公式必须落地为对应代码片段，"
        #         "不得遗漏或简化。代码须包含数据加载、模型定义、训练循环、"
        #         "评估指标输出。"
        #     ),
        #     prompt=(
        #         f"实验配置：\n{config_blocks}\n\n"
        #         f"公式↔代码映射：\n{formula_blocks}\n\n"
        #         "请生成实验代码。"
        #     ),
        # )
        # code = {"path": result.path, "content": result.content, "language": result.language}

        # 占位数据：用公式↔代码映射的 code_stub 拼出最小可读骨架
        stubs = [m.get("code_stub", "") for m in input_obj.formula_code_map if m.get("code_stub")]
        if not stubs:
            stubs = ["# placeholder: no formula_code_map available"]
        content_lines = [
            '"""Auto-generated experiment code (placeholder)."""',
            "import torch",
            "from torch import nn",
            "",
            "",
            "def build_model():",
            "    # 公式↔代码映射落地点：",
        ]
        for i, stub in enumerate(stubs):
            content_lines.append(f"    # concept {i + 1}: {stub}")
        content_lines.extend([
            "    return nn.Sequential(nn.Linear(784, 128), nn.ReLU(), nn.Linear(128, 10))",
            "",
            "",
            "def train(config):",
            "    model = build_model()",
            "    # 占位训练循环",
            "    return {'final_loss': 0.1, 'accuracy': 0.9}",
            "",
            "",
            "if __name__ == '__main__':",
            "    cfg = {'lr': 1e-3, 'epochs': 10}",
            "    print(train(cfg))",
        ])
        code = {
            "path": "experiments/run_exp.py",
            "content": "\n".join(content_lines),
            "language": "python",
        }

        output = CodeGenerateOutput(code=code)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"生成实验代码：{code['path']}（{code['language']}），"
                f"覆盖 {len(stubs)} 个公式概念"
            ),
        )


# ===== CodeReviewAgent（借鉴 AI-Researcher Advisor Agent）=====

class CodeReviewAgent(AgentNode):
    """实验代码审查 Agent（AI-Researcher Advisor Agent，导师角色）。

    借鉴 AI-Researcher 的 Advisor Agent：审查 Code Agent 生成的实验代码，
    校验代码是否忠实实现公式↔代码映射中的每个公式，并给出修改建议。
    审查不通过时，由外部循环驱动回到 CodeGenerateAgent 重新生成。

    设计要点：
    - task_type = experiment_code_review，provider=minimax
    - 输入：实验代码 + 公式↔代码映射（用于校验） + 上一轮审查记录
    - 输出：累计审查记录列表 + 是否通过标志
    - 内部 MAX_REVIEW_ROUNDS = 3：限制单次 _execute 内的最大审查轮次，
      避免无限循环；超过仍未通过则交由外部 GraphRunner 决策是否回滚。
    - 多轮迭代语义：图是 DAG 不支持环，故本节点采用「内部循环」范式——
      _execute 内对当前代码循环审查 N 次（N <= MAX_REVIEW_ROUNDS），
      每轮若未通过则模拟 Code Agent 修正（占位），直到通过或达上限。
      实际生产中可改为「单轮审查 + 由 GraphRunner 外部循环重跑子链」。
    """

    node_type = "experiment_code_review"
    task_type = "experiment_code_review"
    input_schema = CodeReviewInput
    output_schema = CodeReviewOutput
    output_keys = {
        "review_notes": EXPERIMENT_REVIEW_NOTES,
        # passed 不写回 context（仅供 graph 决策与下游节点参考，避免污染域键）
    }

    # 单次 _execute 内的最大审查轮次（导师-学生迭代上限）
    MAX_REVIEW_ROUNDS = 3

    def _build_input(self, ctx: ExecutionContext) -> CodeReviewInput:
        return CodeReviewInput(
            code=ctx.get(EXPERIMENT_CODE, {}),
            formula_code_map=ctx.get(DESIGN_FORMULA_CODE_MAP, []),
            prev_review_notes=ctx.get(EXPERIMENT_REVIEW_NOTES, []),
        )

    def _execute(self, input_obj: CodeReviewInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)

        # === LLM 调用范式（占位，实际未执行）===
        # 借鉴 AI-Researcher Advisor Agent：以审稿人视角校验代码与公式的对应关系
        # from core.llm.base import StructuredOutputRequest
        # class ReviewNoteSchema(BaseModel):
        #     passed: bool
        #     issues: list[dict]  # [{severity, category, location, comment, suggestion}]
        #     summary: str
        # review_notes = list(input_obj.prev_review_notes)
        # code = input_obj.code
        # start_round = len(review_notes) + 1
        # for round_idx in range(start_round, start_round + self.MAX_REVIEW_ROUNDS):
        #     formula_blocks = "\n".join(
        #         f"- {m.get('concept')}: {m.get('formula_latex')} -> {m.get('code_stub')}"
        #         for m in input_obj.formula_code_map
        #     )
        #     prev_issues = review_notes[-1]["issues"] if review_notes else []
        #     result = registry.structured_output(
        #         task_type=self.task_type,
        #         output_schema=ReviewNoteSchema,
        #         system=(
        #             "你是实验代码审稿人（Advisor）。校验代码是否忠实实现每个公式，"
        #             "并指出 bug、公式偏差、规范问题。若上一轮有 issue，"
        #             "确认是否已修正。"
        #         ),
        #         prompt=(
        #             f"实验代码（{code.get('path')}）：\n{code.get('content')}\n\n"
        #             f"公式↔代码映射：\n{formula_blocks}\n\n"
        #             f"上一轮 issue：{prev_issues}"
        #         ),
        #     )
        #     review_notes.append({
        #         "round": round_idx,
        #         "passed": result.passed,
        #         "issues": result.issues,
        #         "summary": result.summary,
        #     })
        #     if result.passed:
        #         break
        #     # 未通过：模拟 Code Agent 修正（实际由 GraphRunner 重跑 CodeGenerateAgent）
        #     # code = regenerate_with_fixes(code, result.issues)
        # passed = review_notes[-1]["passed"] if review_notes else False

        # 占位数据：首轮即通过（避免占位流程卡在迭代上限）
        review_notes = list(input_obj.prev_review_notes)
        if not review_notes:
            review_notes.append({
                "round": 1,
                "passed": True,
                "issues": [],
                "summary": "占位审查：代码结构完整，公式↔代码映射均落地，通过。",
            })
        passed = review_notes[-1].get("passed", False)

        output = CodeReviewOutput(review_notes=review_notes, passed=passed)
        summary = (
            f"代码审查完成（共 {len(review_notes)} 轮记录，"
            f"MAX_REVIEW_ROUNDS={self.MAX_REVIEW_ROUNDS}）："
            f"{'通过' if passed else '未通过'}"
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=summary,
        )


# ===== ExperimentRunTool =====

class ExperimentRunTool(ToolNode):
    """实验运行工具节点（占位）。

    实际运行实验代码，此处只标记 PLANNED → RUNNING → COMPLETED。
    借鉴 AI-Researcher：运行前可对审查通过的代码做语法检查与 dry-run。
    """

    node_type = "experiment_run"
    input_schema = ExperimentRunInput
    output_schema = ExperimentRunOutput
    output_keys = {
        "experiment_ids": EXPERIMENT_IDS,
    }

    def _build_input(self, ctx: ExecutionContext) -> ExperimentRunInput:
        configs = ctx.get(EXPERIMENT_CONFIGS, [])
        code = ctx.get(EXPERIMENT_CODE, {})
        return ExperimentRunInput(configs=configs, code=code)

    def _execute(self, input_obj: ExperimentRunInput, ctx: ExecutionContext) -> NodeResult:
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)

        # === 实验运行范式（占位，实际未执行）===
        # experiment_ids = []
        # for cfg in input_obj.configs:
        #     exp_id = KnowledgeStore.new_id()
        #     exp = Experiment(
        #         experiment_id=exp_id,
        #         name=cfg["name"],
        #         verifies_claim_ids=cfg.get("verifies_claim_ids", []),
        #         config=cfg,
        #         status=ExperimentStatus.RUNNING,
        #     )
        #     store.save_experiment(exp)
        #     # 实际执行训练/评估（运行 input_obj.code）
        #     # subprocess.run([sys.executable, input_obj.code["path"], ...])
        #     exp.status = ExperimentStatus.COMPLETED
        #     exp.result_summary = "占位结果摘要"
        #     store.save_experiment(exp)
        #     experiment_ids.append(exp_id)

        # 占位数据
        experiment_ids = [KnowledgeStore.new_id() for _ in input_obj.configs]

        output = ExperimentRunOutput(experiment_ids=experiment_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"运行 {len(experiment_ids)} 个实验（占位）",
        )


# ===== AnomalyCheckAgent =====

class AnomalyCheckAgent(AgentNode):
    """异常检测 Agent。

    调用 experiment_anomaly_analyze 检测实验异常（loss spike/NaN/不收敛），
    异常时把实验状态置为 ANOMALY_DETECTED 并记录 anomaly_notes。
    """

    node_type = "experiment_anomaly_check"
    task_type = "experiment_anomaly_analyze"
    input_schema = AnomalyCheckInput
    output_schema = AnomalyCheckOutput
    output_keys = {
        "anomaly_report": EXPERIMENT_ANOMALY_REPORT,
    }

    def _build_input(self, ctx: ExecutionContext) -> AnomalyCheckInput:
        experiment_ids = ctx.get(EXPERIMENT_IDS, [])
        return AnomalyCheckInput(experiment_ids=experiment_ids)

    def _execute(self, input_obj: AnomalyCheckInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)

        # === LLM 调用范式（占位，实际未执行）===
        # from core.llm.base import StructuredOutputRequest
        # class AnomalyReportSchema(BaseModel):
        #     has_anomaly: bool
        #     analysis: str
        #     severity: str  # high/medium/low
        #     suggestion: str
        # reports = []
        # for exp_id in input_obj.experiment_ids:
        #     exp = store.get_experiment(exp_id)
        #     resp = registry.structured_output(
        #         task_type=self.task_type,
        #         output_schema=AnomalyReportSchema,
        #         system="你是实验监控助手。分析实验日志，检测 loss spike/NaN/不收敛等异常。",
        #         prompt=f"实验 {exp_id} 的日志/结果摘要：\n{exp.result_summary}",
        #     )
        #     if resp.has_anomaly:
        #         exp.status = ExperimentStatus.ANOMALY_DETECTED
        #         exp.anomaly_notes = resp.analysis
        #         store.save_experiment(exp)
        #     reports.append({"exp_id": exp_id, **resp.model_dump()})
        # anomaly_report = json.dumps(reports, ensure_ascii=False)

        # 占位数据：无异常
        anomaly_report = "无异常"

        output = AnomalyCheckOutput(anomaly_report=anomaly_report)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary="异常检测完成：无异常",
        )


# ===== ClaimVerifyAgent =====

class ClaimVerifyAgent(AgentNode):
    """Claim 验证 Agent。

    把实验结果关联到 Claim，status=VERIFIED，并创建 EXPERIMENT_RESULT Artifact。
    借鉴 AI-Researcher：验证结果回填 Claim 的 evidence_refs，形成完整证据链。
    """

    node_type = "experiment_claim_verify"
    task_type = "experiment_anomaly_analyze"
    input_schema = ClaimVerifyInput
    output_schema = ClaimVerifyOutput
    output_keys = {
        "result_artifact_ids": EXPERIMENT_RESULT_ARTIFACT_IDS,
    }

    def _build_input(self, ctx: ExecutionContext) -> ClaimVerifyInput:
        experiment_ids = ctx.get(EXPERIMENT_IDS, [])
        claim_ids = ctx.get(DESIGN_CLAIM_IDS, [])
        return ClaimVerifyInput(experiment_ids=experiment_ids, claim_ids=claim_ids)

    def _execute(self, input_obj: ClaimVerifyInput, ctx: ExecutionContext) -> NodeResult:
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        manager: Optional[ArtifactManager] = ctx.get(ARTIFACT_MANAGER)

        # === Claim 验证 + Artifact 创建范式（占位，实际未执行）===
        # result_artifact_ids = []
        # for exp_id in input_obj.experiment_ids:
        #     exp = store.get_experiment(exp_id)
        #     for claim_id in exp.verifies_claim_ids:
        #         claim = store.get_claim(claim_id)
        #         claim.evidence_refs.append({"type": "experiment", "id": exp_id})
        #         claim.status = ClaimStatus.VERIFIED
        #         claim.verified_at = datetime.utcnow()
        #         store.save_claim(claim)
        #     artifact = manager.create_artifact(
        #         artifact_type=ArtifactType.EXPERIMENT_RESULT,
        #         title=f"实验结果: {exp.name}",
        #         content=exp.result_summary,
        #         cites_claim_ids=exp.verifies_claim_ids,
        #         cites_experiment_ids=[exp_id],
        #         source_stage="experiment",
        #         created_by="experiment_claim_verify",
        #     )
        #     result_artifact_ids.append(artifact.artifact_id)

        # 占位数据：每个实验对应一个结果 Artifact
        result_artifact_ids = [KnowledgeStore.new_id() for _ in input_obj.experiment_ids]

        output = ClaimVerifyOutput(result_artifact_ids=result_artifact_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"验证 Claim 并生成 {len(result_artifact_ids)} 个结果 Artifact",
        )
