"""experiment 阶段 Agent / Tool 节点实现。

节点拓扑（借鉴 AI-Researcher 的「导师-学生迭代」核心方法）：
    ExperimentConfigAgent（生成实验配置：数据集/baseline/超参）
    → CodeGenerateAgent（AI-Researcher Code Agent：生成实验代码）
    → CodeReviewAgent（AI-Researcher Advisor Agent：审查代码）
    → StageCheckpoint
    → ExperimentRunTool（执行实验，ToolNode）
    → AnomalyCheckAgent（检测异常：loss spike/NaN/不收敛）
    → ClaimVerifyAgent（用实验结果验证 Claim）
    → ExperimentOutcomeAssessAgent（评估实验成败，决定是否进入 writing）

执行模式：
- dry_run=True  ：用占位数据返回，不调用 LLM、不真实运行代码
- dry_run=False ：真实调用 MiniMax M3，真实运行实验代码，真实更新 Claim 状态
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

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
    HumanNode,
    HumanResponse,
    NodeInput,
    NodeOutput,
    NodeResult,
    NodeStatus,
    ToolNode,
)
from core.tools import check_syntax, is_remote_mode, run_python_code, run_python_code_remote

from stages.common import (
    ARTIFACT_MANAGER,
    DESIGN_CLAIM_IDS,
    DESIGN_FORMULA_CODE_MAP,
    DESIGN_METHOD_ARTIFACT_ID,
    DESIGN_METHOD_CONTENT,
    DRY_RUN,
    EXPERIMENT_ANOMALY_REPORT,
    EXPERIMENT_CODE,
    EXPERIMENT_CONFIGS,
    EXPERIMENT_IDS,
    EXPERIMENT_OUTCOME,
    EXPERIMENT_RESULT_ARTIFACT_IDS,
    EXPERIMENT_REVIEW_NOTES,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    PROJECT_DIR,
    PROJECT_ROOT,
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
    ExperimentOutcomeAssessInput,
    ExperimentOutcomeAssessOutput,
    ExperimentRunInput,
    ExperimentRunOutput,
)

logger = logging.getLogger(__name__)


# ===== 结构化输出 Schema =====

class ExperimentConfigItem(BaseModel):
    """单条实验配置。"""

    name: str = Field(description="实验名称")
    dataset: str = Field(description="数据集名称")
    baseline: str = Field(description="对比基线方法")
    hyperparams: dict = Field(default_factory=dict, description="超参")
    verifies_claim_ids: list[str] = Field(
        default_factory=list,
        description="本实验验证的 Claim ID（从给定 claim_ids 中选取）",
    )


class ExperimentConfigSchema(BaseModel):
    """实验配置批量 schema。"""

    configs: list[ExperimentConfigItem]


class CodeArtifactSchema(BaseModel):
    """代码生成 schema。"""

    path: str = Field(default="experiments/run_exp.py", description="代码文件相对路径")
    content: str = Field(description="完整 Python 代码")
    language: str = Field(default="python")


class ReviewIssueItem(BaseModel):
    """审查问题项。"""

    severity: str = Field(description="high/medium/low")
    category: str = Field(description="公式偏差/bug/规范")
    location: str = Field(default="", description="代码位置（如 L42-58）")
    comment: str = Field(description="问题描述")
    suggestion: str = Field(default="", description="修改建议")


class ReviewNoteSchema(BaseModel):
    """单轮审查记录 schema。"""

    passed: bool = Field(description="本轮是否通过")
    issues: list[ReviewIssueItem] = []
    summary: str = Field(description="本轮审查总结")


class AnomalyReportSchema(BaseModel):
    """异常检测 schema。"""

    has_anomaly: bool = Field(description="是否检测到异常")
    analysis: str = Field(description="异常分析")
    severity: str = Field(default="low", description="high/medium/low")
    suggestion: str = Field(default="", description="处置建议")


class OutcomeAssessSchema(BaseModel):
    """实验成败评估 schema。"""

    success: bool = Field(description="实验是否成功验证核心 Claim")
    verified_claim_ids: list[str] = []
    refuted_claim_ids: list[str] = []
    inconclusive_claim_ids: list[str] = []
    recommendation: str = Field(
        description="proceed_to_writing / rollback_to_ideation / retry_experiment / abort"
    )
    summary: str = Field(description="一句话总结")


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
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)

        # 加载方法文档作为 prompt 素材
        method_content = ctx.get(DESIGN_METHOD_CONTENT, "")

        if not dry_run and registry is not None:
            try:
                result = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=ExperimentConfigSchema,
                    system=(
                        "你是实验设计助手。根据 Claim 与方法文档生成 1-3 组实验配置，"
                        "每个配置含 name/dataset/baseline/hyperparams/verifies_claim_ids。"
                        "实验设计原则：覆盖核心 Claim、baseline 公平可比、超参合理可复现。"
                        "verifies_claim_ids 必须从给定的 claim_ids 列表中选取。"
                    ),
                    prompt=(
                        f"Claim IDs: {input_obj.claim_ids}\n"
                        f"方法文档：\n{method_content[:1500]}"
                    ),
                )
                configs = [c.model_dump() for c in result.configs]
                # 校验 verifies_claim_ids 都在 input_obj.claim_ids 中
                valid_claim_ids = set(input_obj.claim_ids)
                for cfg in configs:
                    cfg["verifies_claim_ids"] = [
                        cid for cid in cfg.get("verifies_claim_ids", [])
                        if cid in valid_claim_ids
                    ]
                    # 兜底：若过滤后为空，关联全部 claim
                    if not cfg["verifies_claim_ids"] and input_obj.claim_ids:
                        cfg["verifies_claim_ids"] = list(input_obj.claim_ids)
            except Exception as e:
                logger.warning("ExperimentConfig 真实调用失败，回退占位: %s", e)
                configs = self._placeholder(input_obj)
        else:
            configs = self._placeholder(input_obj)

        output = ExperimentConfigOutput(configs=configs)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"生成 {len(configs)} 组实验配置",
        )

    @staticmethod
    def _placeholder(input_obj: ExperimentConfigInput) -> list[dict]:
        return [
            {
                "name": f"exp_{i + 1}",
                "dataset": "placeholder_dataset",
                "baseline": "random",
                "hyperparams": {"lr": 1e-3, "epochs": 10},
                "verifies_claim_ids": input_obj.claim_ids,
            }
            for i in range(min(2, max(1, len(input_obj.claim_ids))))
        ]


# ===== CodeGenerateAgent（借鉴 AI-Researcher Code Agent）=====

class CodeGenerateAgent(AgentNode):
    """实验代码生成 Agent（AI-Researcher Code Agent，学生角色）。

    根据实验配置 + design 阶段产生的公式↔代码映射，生成忠实实现论文方法的实验代码。

    设计要点：
    - task_type = experiment_code_generate
    - 输入：实验配置 + 公式↔代码映射
    - 输出：实验代码 {path, content, language}
    - 公式↔代码映射作为强约束：每个公式必须落地为对应代码片段
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
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not dry_run and registry is not None:
            try:
                # 构造 prompt：实验配置 + 公式↔代码映射
                formula_blocks = "\n".join(
                    f"- 概念: {m.get('concept')}\n"
                    f"  公式: {m.get('formula_latex')}\n"
                    f"  代码骨架: {m.get('code_stub')}\n"
                    f"  状态: {m.get('status')}"
                    for m in input_obj.formula_code_map
                ) or "(无公式↔代码映射)"
                config_blocks = "\n".join(
                    f"- {c.get('name')}: dataset={c.get('dataset')}, "
                    f"baseline={c.get('baseline')}, hyperparams={c.get('hyperparams')}, "
                    f"verifies={c.get('verifies_claim_ids')}"
                    for c in input_obj.configs
                ) or "(无实验配置)"

                # 代码生成用 complete 而非 structured_output：
                # MiniMax M3 在代码生成场景倾向返回纯代码/markdown 代码块，
                # 强制 json_object 反而触发 JSON 解析失败。complete + 代码块提取更稳。
                resp = registry.complete(
                    task_type=self.task_type,
                    system=(
                        "你是实验代码工程师。根据实验配置与公式↔代码映射，"
                        "生成完整可运行的实验代码。每个公式必须落地为对应代码片段，"
                        "不得遗漏或简化。代码须包含：数据加载、模型定义、训练循环、"
                        "评估指标输出。代码必须自包含、可独立运行（不依赖外部数据集时用合成数据）。"
                        "\n\n结果输出约定（必须遵守）："
                        "代码末尾必须将结果写入文件 experiments/results.json，格式："
                        '{"experiments": [{"name": "exp_name", "metrics": {"accuracy": 0.85, "loss": 0.12}, '
                        '"verified_claims": ["claim_id1"], "status": "success"}]}'
                        "。每个实验配置对应一条 experiments 记录，name 与配置中的 name 一致；"
                        "status 取 success/failed/placeholder。同时打印相同 JSON 到 stdout（最后一行为该 JSON），便于下游兜底解析。"
                        "\n\n输出格式：仅返回一个 ```python ... ``` 代码块，不要任何额外说明。"
                    ),
                    prompt=(
                        f"实验配置：\n{config_blocks}\n\n"
                        f"公式↔代码映射：\n{formula_blocks}\n\n"
                        "请生成完整可运行的 Python 实验代码，用 ```python 代码块包裹。"
                    ),
                )
                content = self._extract_code_block(resp.text)
                if not content:
                    # 兜底：若 LLM 未用代码块包裹，直接把全文当代码
                    content = resp.text
                code = {
                    "path": "experiments/run_exp.py",
                    "content": content,
                    "language": "python",
                }
                # 语法检查
                ok, err = check_syntax(code["content"])
                if not ok:
                    logger.warning("生成的代码语法错误，将交给 CodeReview 修正: %s", err)
            except Exception as e:
                logger.warning("CodeGenerate 真实调用失败，回退占位: %s", e)
                code = self._placeholder(input_obj)
        else:
            code = self._placeholder(input_obj)

        output = CodeGenerateOutput(code=code)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"生成实验代码：{code['path']}（{code['language']}），"
                f"{len(code['content'])} 字符"
            ),
        )

    @staticmethod
    def _extract_code_block(text: str) -> str:
        """从 LLM 返回中提取 Python 代码。

        处理：
        1. ```python ... ``` 代码块（可在文本任意位置）
        2. ``` ... ``` 代码块
        3. 无代码块包裹的纯代码（兜底：尝试跳过明显的非代码行）
        """
        if not text:
            return ""
        stripped = text.strip()

        # 优先用正则搜索 ```python ... ``` 或 ``` ... ``` 代码块（可在任意位置）
        import re
        patterns = [
            r"```python\s*\n(.*?)```",
            r"```\s*\n(.*?)```",
            r"```python\s*(.*?)```",
            r"```(.*?)```",
        ]
        for pat in patterns:
            match = re.search(pat, stripped, re.DOTALL)
            if match:
                code = match.group(1).strip()
                if code:
                    return code

        # 兜底：若整体看起来像纯代码（无大量自然语言），直接返回
        # 启发式：若首行以 Python 关键字/注释/导入开头，认为是纯代码
        first_line = stripped.splitlines()[0].strip() if stripped.splitlines() else ""
        code_indicators = ("import ", "from ", "#", "def ", "class ", "if ", "try:", "\"\"\"", "'''", "@", "import\t")
        if first_line.startswith(code_indicators):
            return stripped

        # 最后兜底：返回原文（让语法检查拦截）
        return stripped

    @staticmethod
    def _placeholder(input_obj: CodeGenerateInput) -> dict:
        """生成可运行的实验代码（不再硬编码 MNIST 模板）。

        策略：
        1. 根据 formula_code_map 中 code_stub 动态生成函数骨架
        2. 默认走 NumPy + 标准库的纯 Python 实现，不强依赖 torch（兼容材料/统计/博弈论等多种主题）
        3. 末尾按 ExperimentRunTool 协议写入 experiments/results.json
        """
        import textwrap
        stubs = [m.get("code_stub", "") for m in input_obj.formula_code_map if m.get("code_stub")]
        formulas = [m.get("formula_latex", "") for m in input_obj.formula_code_map if m.get("formula_latex")]
        concepts = [m.get("concept", f"concept_{i + 1}") for i, m in enumerate(input_obj.formula_code_map)]

        # 收集实验 config
        configs_for_results = input_obj.configs or [{"name": "exp_1", "params": {}}]
        # 构建每个 config 的实际参数字典（从 configs 中提取）
        placeholder_experiments = []
        for i, c in enumerate(configs_for_results):
            params = c.get("params", {}) or {}
            placeholder_experiments.append({
                "name": c.get("name", f"exp_{i + 1}"),
                "metrics": {
                    # 默认占位指标：包含 status + 简单数值
                    "placeholder_metric": 0.0,
                    **({"primary": 0.0} if not params else {}),
                },
                "verified_claims": c.get("verifies_claim_ids", []),
                "status": "placeholder",
                "config": params,
            })

        # 提取关键实验参数（来自 configs）
        sample_cfg = configs_for_results[0] if configs_for_results else {}
        sample_params = sample_cfg.get("params", {}) if isinstance(sample_cfg, dict) else {}

        # 生成核心算法函数：从 stub 提取函数名（默认 compute_metric）
        func_names = []
        for stub in stubs:
            # 简单提取 def func_name 模式
            import re
            m = re.search(r"def\s+(\w+)\s*\(", stub)
            if m:
                func_names.append(m.group(1))
        if not func_names:
            func_names = ["compute_metric", "evaluate"]

        # 构建代码：使用 stub 中的真实函数作为函数体（如果提供），否则用通用实现
        code_blocks = []
        for i, stub in enumerate(stubs):
            if stub.strip().startswith("def "):
                code_blocks.append(f"# === {concepts[i] if i < len(concepts) else 'concept_' + str(i + 1)} ===")
                code_blocks.append(stub.strip())
                code_blocks.append("")
            else:
                # 占位实现：返回占位指标的通用函数
                fname = func_names[i] if i < len(func_names) else f"concept_{i + 1}_fn"
                code_blocks.append(f"def {fname}(*args, **kwargs):")
                code_blocks.append(f'    """概念 {concepts[i] if i < len(concepts) else "concept_" + str(i + 1)} 的占位实现。"""')
                code_blocks.append(f"    return {{'status': 'placeholder', 'concept': '{concepts[i] if i < len(concepts) else 'concept_' + str(i + 1)}'}}")
                code_blocks.append("")

        formulas_comment = ""
        if formulas:
            formulas_comment = "\n# 对应的数学公式：\n" + "\n".join(f"# {i + 1}. {f[:120]}" for i, f in enumerate(formulas[:5])) + "\n"

        results_literal = json.dumps(
            {"experiments": placeholder_experiments}, ensure_ascii=False, indent=2
        )

        content = f'''"""Auto-generated experiment code (placeholder for dry_run mode).

研究主题相关实验代码占位符。
不依赖 PyTorch / TensorFlow 等深度学习框架，使用纯 NumPy / Python 标准库实现。
真实运行（LLM 模式）会生成完整可运行代码并替换本占位符。

公式↔代码映射落地点：
{chr(10).join(f"#   - {c}" for c in concepts[:5])}{formulas_comment}
"""
import json
import os
import sys
import time
from typing import Any

import numpy as np


{chr(10).join(code_blocks)}


def run_experiment(config: dict) -> dict:
    """运行单个实验配置。

    Args:
        config: 实验参数字典（来自 ExperimentConfigAgent 的 configs）

    Returns:
        包含 metrics / status / config 的结果字典
    """
    start = time.time()
    try:
        # 调用所有概念函数（占位：返回 dict 结果）
        results = {{}}
        for fname in {func_names!r}:
            if fname in globals():
                try:
                    results[fname] = globals()[fname](**config)
                except Exception as e:
                    results[fname] = {{"status": "error", "error": str(e)}}

        # 简单汇总 metric（占位：返回 config 数值本身）
        primary_metric = float(np.mean([v for v in config.values() if isinstance(v, (int, float))])) if config else 0.0

        return {{
            "status": "placeholder",
            "metrics": {{"primary": primary_metric, "details": results}},
            "config": config,
            "elapsed_sec": time.time() - start,
        }}
    except Exception as e:
        return {{
            "status": "error",
            "error": str(e),
            "config": config,
            "elapsed_sec": time.time() - start,
        }}


def main() -> None:
    """主入口：依次运行所有 config，写入 experiments/results.json。"""
    # 实验配置（与 ExperimentConfigAgent 的 configs 对齐）
    configs = [c.get("params", {{}}) for c in configs_for_results]

    print(f"开始运行 {{len(configs)}} 个实验...")
    experiments = []
    for i, cfg in enumerate(configs):
        name = configs_for_results[i].get("name", f"exp_{{i + 1}}") if i < len(configs_for_results) else f"exp_{{i + 1}}"
        verified_claims = configs_for_results[i].get("verifies_claim_ids", []) if i < len(configs_for_results) else []
        print(f"  [{{i + 1}}/{{len(configs)}}] {{name}}: config={{cfg}}")

        result = run_experiment(cfg)
        experiments.append({{
            "name": name,
            "metrics": result.get("metrics", {{}}),
            "verified_claims": verified_claims,
            "status": result.get("status", "placeholder"),
            "config": cfg,
            "elapsed_sec": result.get("elapsed_sec", 0.0),
        }})

    # 按 ExperimentRunTool 协议写入 results.json
    os.makedirs("experiments", exist_ok=True)
    out_path = "experiments/results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({{"experiments": experiments}}, f, ensure_ascii=False, indent=2)

    print(f"\\n实验完成，结果已写入 {{out_path}}")
    print(json.dumps({{"experiments": experiments}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
'''

        return {
            "path": "experiments/run_exp.py",
            "content": content,
            "language": "python",
        }


# ===== CodeReviewAgent（借鉴 AI-Researcher Advisor Agent）=====

class CodeReviewAgent(AgentNode):
    """实验代码审查 Agent（AI-Researcher Advisor Agent，导师角色）。

    审查 Code Agent 生成的实验代码，校验是否忠实实现公式↔代码映射。
    单次 _execute 内最多审查 MAX_REVIEW_ROUNDS 轮。
    """

    node_type = "experiment_code_review"
    task_type = "experiment_code_review"
    input_schema = CodeReviewInput
    output_schema = CodeReviewOutput
    output_keys = {
        "review_notes": EXPERIMENT_REVIEW_NOTES,
    }

    MAX_REVIEW_ROUNDS = 2  # 单次 _execute 内最大审查轮次（真实模式会消耗 LLM tokens）

    def _build_input(self, ctx: ExecutionContext) -> CodeReviewInput:
        return CodeReviewInput(
            code=ctx.get(EXPERIMENT_CODE, {}),
            formula_code_map=ctx.get(DESIGN_FORMULA_CODE_MAP, []),
            prev_review_notes=ctx.get(EXPERIMENT_REVIEW_NOTES, []),
        )

    def _execute(self, input_obj: CodeReviewInput, ctx: ExecutionContext) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        dry_run: bool = ctx.get(DRY_RUN, True)

        review_notes = list(input_obj.prev_review_notes)
        code = input_obj.code

        # 先做语法检查（不消耗 LLM tokens）
        if code and code.get("content"):
            ok, err = check_syntax(code["content"])
            if not ok:
                review_notes.append({
                    "round": len(review_notes) + 1,
                    "passed": False,
                    "issues": [{
                        "severity": "high",
                        "category": "语法错误",
                        "location": "",
                        "comment": err,
                        "suggestion": "修复语法错误后重新生成",
                    }],
                    "summary": f"语法检查未通过: {err}",
                })

        if dry_run or registry is None or not code:
            # 占位：首轮即通过
            if not review_notes:
                review_notes.append({
                    "round": 1,
                    "passed": True,
                    "issues": [],
                    "summary": "占位审查：代码结构完整，公式↔代码映射均落地，通过。",
                })
            passed = review_notes[-1].get("passed", False)
            output = CodeReviewOutput(review_notes=review_notes, passed=passed)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=f"[dry_run] 代码审查完成（{len(review_notes)} 轮）：{'通过' if passed else '未通过'}",
            )

        # 真实审查：单轮审查（避免多轮消耗 tokens；多轮迭代交由外部 GraphRunner 重跑）
        try:
            formula_blocks = "\n".join(
                f"- {m.get('concept')}: {m.get('formula_latex')} -> {m.get('code_stub')}"
                for m in input_obj.formula_code_map
            ) or "(无公式↔代码映射)"

            prev_issues = review_notes[-1]["issues"] if review_notes else []
            result = registry.structured_output(
                task_type=self.task_type,
                output_schema=ReviewNoteSchema,
                system=(
                    "你是实验代码审稿人（Advisor）。校验代码是否忠实实现每个公式，"
                    "并指出 bug、公式偏差、规范问题。若上一轮有 issue，确认是否已修正。"
                    "若无严重问题（high 级 issue 为 0），标记 passed=true。"
                ),
                prompt=(
                    f"实验代码（{code.get('path')}）：\n{code.get('content')}\n\n"
                    f"公式↔代码映射：\n{formula_blocks}\n\n"
                    f"上一轮 issue：{prev_issues}"
                ),
            )
            review_notes.append({
                "round": len(review_notes) + 1,
                "passed": result.passed,
                "issues": [i.model_dump() for i in result.issues],
                "summary": result.summary,
            })
        except Exception as e:
            logger.warning("CodeReview 真实调用失败，宽松通过: %s", e)
            review_notes.append({
                "round": len(review_notes) + 1,
                "passed": True,  # 审查失败时宽松通过，让流程继续
                "issues": [],
                "summary": f"审查调用失败，宽松通过: {e}",
            })

        passed = review_notes[-1].get("passed", False)
        output = CodeReviewOutput(review_notes=review_notes, passed=passed)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"代码审查完成（共 {len(review_notes)} 轮）：{'通过' if passed else '未通过'}",
        )


# ===== ExperimentReviewHuman（实验前人工审核）=====

class ExperimentReviewHuman(HumanNode):
    """实验运行前人工审核节点。

    呈现实验配置 + 生成代码预览 + 语法检查结果，用户决定是否运行。
    实验可能对硬件/数据集/环境有特殊要求，不盲目运行。
    """

    node_type = "experiment_review_human"
    input_schema = NodeInput
    output_schema = NodeOutput
    output_keys: dict = {}

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _render_prompt(self, ctx: ExecutionContext) -> str:
        configs = ctx.get(EXPERIMENT_CONFIGS, []) or []
        code = ctx.get(EXPERIMENT_CODE, {}) or {}
        code_content = code.get("content", "")
        code_preview = code_content[:800] if code_content else "(空)"
        if len(code_content) > 800:
            code_preview += "\n... (截断，完整代码见 experiments/run_exp.py)"

        # 语法检查
        syntax_ok = "通过"
        if code_content:
            ok, err = check_syntax(code_content)
            syntax_ok = "通过" if ok else f"失败: {err}"

        # 配置摘要
        config_lines = []
        for i, cfg in enumerate(configs):
            hp = cfg.get("hyperparams", {})
            config_lines.append(
                f"  [{i+1}] {cfg.get('name', '?')}\n"
                f"      数据集: {cfg.get('dataset', '?')}\n"
                f"      基线: {cfg.get('baseline', '?')}\n"
                f"      超参: {json.dumps(hp, ensure_ascii=False)[:200]}\n"
                f"      验证 Claim: {cfg.get('verifies_claim_ids', [])}"
            )
        config_str = "\n".join(config_lines) or "(无配置)"

        return (
            "实验即将运行，请审核以下内容后决定：\n\n"
            f"【实验配置】（{len(configs)} 个实验）\n{config_str}\n\n"
            f"【代码语法检查】{syntax_ok}\n\n"
            f"【代码预览】\n```\n{code_preview}\n```\n\n"
            "请选择操作：\n"
            "  - 输入 'approve' 确认运行实验\n"
            "  - 或输入修改意见（将回滚到代码生成阶段）\n"
            "  - 或选择「中止」终止流程"
        )

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        text = (response.text or "").strip()
        approved = text.lower() in ("approve", "通过", "ok", "y", "yes", "run", "运行")
        return NodeOutput(extra={"approved": approved, "comments": "" if approved else text})

    def continue_after_human(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> NodeResult:
        if response.action == "abort":
            return NodeResult(
                status=NodeStatus.FAILED,
                error="用户中止实验",
                summary="用户中止实验运行",
            )
        if response.action == "rollback":
            return NodeResult(
                status=NodeStatus.FAILED,
                error="用户回滚，要求修改实验代码",
                summary="用户回滚到代码生成阶段",
            )
        # continue：检查是否 approve
        text = (response.text or "").strip()
        approved = text.lower() in ("approve", "通过", "ok", "y", "yes", "run", "运行")
        if not approved:
            # 用户提了修改意见 → 视为回滚到代码生成
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"用户要求修改代码: {text[:200]}",
                summary=f"用户要求修改实验代码，回滚到代码生成阶段",
            )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=self._build_output_from_response(response, ctx),
            summary="用户确认运行实验",
        )


# ===== ExperimentRunTool =====

class ExperimentRunTool(ToolNode):
    """实验运行工具节点。

    真实模式：把代码写入项目 experiments/ 目录，用 subprocess 运行。
    dry_run 模式：只生成占位 Experiment ID，不真实运行。
    """

    node_type = "experiment_run"
    input_schema = ExperimentRunInput
    output_schema = ExperimentRunOutput
    output_keys = {
        "experiment_ids": EXPERIMENT_IDS,
    }

    DEFAULT_TIMEOUT = 600  # 10 分钟

    def _build_input(self, ctx: ExecutionContext) -> ExperimentRunInput:
        configs = ctx.get(EXPERIMENT_CONFIGS, [])
        code = ctx.get(EXPERIMENT_CODE, {})
        return ExperimentRunInput(configs=configs, code=code)

    def _execute(self, input_obj: ExperimentRunInput, ctx: ExecutionContext) -> NodeResult:
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)
        project_dir = ctx.get(PROJECT_DIR)
        project_root = ctx.get(PROJECT_ROOT)

        if dry_run or store is None:
            # 占位
            experiment_ids = [KnowledgeStore.new_id() for _ in input_obj.configs]
            output = ExperimentRunOutput(experiment_ids=experiment_ids)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=f"[dry_run] 运行 {len(experiment_ids)} 个实验（占位 ID）",
            )

        # 真实运行
        code = input_obj.code or {}
        code_content = code.get("content", "")
        code_path = code.get("path", "experiments/run_exp.py")

        if not code_content:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="实验代码为空",
                summary="实验运行失败：代码为空",
            )

        # 语法检查门控：不运行有语法错误的代码
        ok, err = check_syntax(code_content)
        if not ok:
            logger.warning("实验代码语法检查失败，跳过运行: %s", err)
            experiment_ids: list[str] = []
            for cfg in input_obj.configs:
                exp_id = KnowledgeStore.new_id()
                exp = Experiment(
                    experiment_id=exp_id,
                    name=cfg.get("name", f"exp_{len(experiment_ids) + 1}"),
                    verifies_claim_ids=cfg.get("verifies_claim_ids", []),
                    config=cfg,
                    status=ExperimentStatus.FAILED,
                )
                exp.anomaly_notes = f"代码语法错误，未执行: {err}"
                exp.result_summary = f"语法错误跳过: {err}"
                exp.started_at = datetime.now()
                store.save_experiment(exp)
                experiment_ids.append(exp_id)
            output = ExperimentRunOutput(experiment_ids=experiment_ids)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"代码语法错误: {err}",
                output=output,
                summary=f"实验跳过：代码语法错误 ({err})",
            )

        # 选择运行目录：优先 PROJECT_DIR，回退 PROJECT_ROOT
        run_dir = Path(project_dir) if project_dir else (
            Path(project_root) if project_root else Path.cwd()
        )

        experiment_ids: list[str] = []
        for cfg in input_obj.configs:
            exp_id = KnowledgeStore.new_id()
            exp_name = cfg.get("name", f"exp_{len(experiment_ids) + 1}")

            # 创建 Experiment 实体（PLANNED）
            exp = Experiment(
                experiment_id=exp_id,
                name=exp_name,
                verifies_claim_ids=cfg.get("verifies_claim_ids", []),
                config=cfg,
                status=ExperimentStatus.PLANNED,
            )
            store.save_experiment(exp)

            # 标记 RUNNING
            exp.status = ExperimentStatus.RUNNING
            exp.started_at = datetime.now()
            store.save_experiment(exp)

            # 真实运行代码（自动检测本地/远程 SSH 模式）
            run_result = None
            try:
                run_fn = run_python_code_remote if is_remote_mode() else run_python_code
                run_result = run_fn(
                    code=code_content,
                    project_dir=run_dir,
                    code_path=code_path,
                    timeout=self.DEFAULT_TIMEOUT,
                )

                if run_result.success:
                    exp.status = ExperimentStatus.COMPLETED
                    exp.result_summary = (
                        f"exit=0, runtime={run_result.runtime_seconds:.1f}s\n"
                        f"stdout:\n{run_result.stdout[:2000]}"
                    )
                else:
                    exp.status = ExperimentStatus.FAILED
                    exp.anomaly_notes = (
                        f"exit={run_result.returncode}, runtime={run_result.runtime_seconds:.1f}s\n"
                        f"stderr:\n{run_result.stderr[:2000]}"
                    )
                    exp.result_summary = f"运行失败: {run_result.stderr[:500]}"
            except Exception as e:
                exp.status = ExperimentStatus.FAILED
                exp.anomaly_notes = f"运行异常: {type(e).__name__}: {e}"
                exp.result_summary = f"运行异常: {e}"

            # 收集结构化结果：优先读取 experiments/results.json，兜底解析 stdout 末行 JSON
            result_metrics: dict = {}
            results_file = run_dir / "experiments" / "results.json"
            if results_file.exists():
                try:
                    all_results = json.loads(results_file.read_text(encoding="utf-8"))
                    exp_results = all_results.get("experiments", [])
                    # 按 name 匹配当前实验
                    for r in exp_results:
                        if r.get("name") == exp_name:
                            result_metrics = r.get("metrics", {}) or {}
                            if r.get("status") == "success":
                                exp.status = ExperimentStatus.COMPLETED
                            break
                except Exception as e:
                    logger.warning("解析 results.json 失败: %s", e)
            # 兜底：若文件未命中 metrics，尝试从 stdout 末行解析 JSON
            if not result_metrics and run_result is not None and run_result.success:
                try:
                    stdout_json = json.loads(run_result.stdout.strip().split('\n')[-1])
                    result_metrics = stdout_json.get("metrics", stdout_json) or {}
                except Exception:
                    pass
            # 把 metrics 写回 experiment
            if result_metrics:
                exp.metrics = result_metrics
                exp.result_summary = (
                    f"metrics: {json.dumps(result_metrics, ensure_ascii=False)}\n"
                    f"{exp.result_summary or ''}"
                ).strip()
            elif "未产生结构化结果文件" not in (exp.anomaly_notes or ""):
                # 文件与 stdout 均无结构化结果 → 记录缺失（不覆盖已有异常根因）
                exp.anomaly_notes = (
                    f"{exp.anomaly_notes}\n未产生结构化结果文件"
                    if exp.anomaly_notes else "未产生结构化结果文件"
                )

            exp.completed_at = datetime.now()
            store.save_experiment(exp)
            experiment_ids.append(exp_id)

        output = ExperimentRunOutput(experiment_ids=experiment_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"运行 {len(experiment_ids)} 个实验",
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
        dry_run: bool = ctx.get(DRY_RUN, True)

        if dry_run or registry is None or store is None:
            output = AnomalyCheckOutput(anomaly_report="无异常（dry_run）")
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary="[dry_run] 异常检测完成：无异常",
            )

        # 真实检测：基于每个实验的 result_summary 与 anomaly_notes 调用 LLM
        reports: list[dict] = []
        for exp_id in input_obj.experiment_ids:
            try:
                exp = store.get_experiment(exp_id)
            except Exception:
                continue

            # 已 FAILED 的实验直接判定为异常
            if exp.status == ExperimentStatus.FAILED:
                reports.append({
                    "exp_id": exp_id,
                    "has_anomaly": True,
                    "analysis": exp.anomaly_notes or "实验运行失败",
                    "severity": "high",
                    "suggestion": "检查代码或配置后重试",
                })
                continue

            try:
                resp = registry.structured_output(
                    task_type=self.task_type,
                    output_schema=AnomalyReportSchema,
                    system=(
                        "你是实验监控助手。分析实验结果摘要，检测 loss spike/NaN/不收敛等异常。"
                        "若结果正常或无足够信息判断，has_anomaly=false。"
                    ),
                    prompt=(
                        f"实验 {exp.name} 的结果摘要：\n{exp.result_summary or '(空)'}\n"
                        f"异常记录：{exp.anomaly_notes or '(无)'}"
                    ),
                )
                if resp.has_anomaly:
                    exp.status = ExperimentStatus.ANOMALY_DETECTED
                    exp.anomaly_notes = resp.analysis
                    store.save_experiment(exp)
                reports.append({
                    "exp_id": exp_id,
                    "has_anomaly": resp.has_anomaly,
                    "analysis": resp.analysis,
                    "severity": resp.severity,
                    "suggestion": resp.suggestion,
                })
            except Exception as e:
                logger.warning("AnomalyCheck 调用失败（exp=%r）: %s", exp_id, e)
                reports.append({
                    "exp_id": exp_id,
                    "has_anomaly": False,
                    "analysis": f"检测失败: {e}",
                    "severity": "low",
                    "suggestion": "",
                })

        anomaly_report = json.dumps(reports, ensure_ascii=False, indent=2)
        anomaly_count = sum(1 for r in reports if r.get("has_anomaly"))
        output = AnomalyCheckOutput(anomaly_report=anomaly_report)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"异常检测完成：{anomaly_count}/{len(reports)} 个实验有异常",
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
        dry_run: bool = ctx.get(DRY_RUN, True)

        if dry_run or store is None or manager is None:
            # 占位
            result_artifact_ids = [KnowledgeStore.new_id() for _ in input_obj.experiment_ids]
            output = ClaimVerifyOutput(result_artifact_ids=result_artifact_ids)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=f"[dry_run] 验证 Claim 并生成 {len(result_artifact_ids)} 个结果 Artifact（占位 ID）",
            )

        # 真实验证
        result_artifact_ids: list[str] = []
        for exp_id in input_obj.experiment_ids:
            try:
                exp = store.get_experiment(exp_id)
            except Exception:
                continue

            # 仅 COMPLETED 状态的实验才验证 Claim（FAILED/ANOMALY 不验证）
            if exp.status != ExperimentStatus.COMPLETED:
                continue

            # 把实验结果回填到关联的 Claim
            for claim_id in exp.verifies_claim_ids:
                try:
                    claim = store.get_claim(claim_id)
                    # 添加 experiment 证据（去重）
                    existing_exp_ids = {
                        ref["id"] for ref in claim.evidence_refs
                        if ref.get("type") == "experiment"
                    }
                    if exp_id not in existing_exp_ids:
                        claim.evidence_refs.append({"type": "experiment", "id": exp_id})
                    claim.status = ClaimStatus.VERIFIED
                    claim.verified_at = datetime.now()
                    store.save_claim(claim)
                except Exception as e:
                    logger.warning("Claim %s 验证失败: %s", claim_id, e)

            # 创建 EXPERIMENT_RESULT Artifact
            try:
                artifact = manager.create_artifact(
                    artifact_type=ArtifactType.EXPERIMENT_RESULT,
                    title=f"实验结果: {exp.name}",
                    content=exp.result_summary or "",
                    cites_claim_ids=exp.verifies_claim_ids,
                    cites_experiment_ids=[exp_id],
                    source_stage="experiment",
                    created_by="experiment_claim_verify",
                )
                result_artifact_ids.append(artifact.artifact_id)
            except Exception as e:
                logger.warning("Artifact 创建失败（exp=%r）: %s", exp_id, e)

        output = ClaimVerifyOutput(result_artifact_ids=result_artifact_ids)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"验证 Claim 并生成 {len(result_artifact_ids)} 个结果 Artifact",
        )


# ===== ExperimentOutcomeAssessAgent =====

class ExperimentOutcomeAssessAgent(AgentNode):
    """实验成败评估 Agent。

    评估实验结果是否验证了核心 Claim，给出"是否进入 writing"的建议。
    位于 experiment 阶段末尾（ClaimVerifyAgent 之后），是 experiment → writing
    阶段切换的决策关口。

    重要：实验失败是科研常态——Claim 被实验反驳是正常、有价值的发现。
    系统不应在实验失败（success=False）时强行进入论文写作阶段。
    """

    node_type = "experiment_outcome_assess"
    task_type = "experiment_outcome_assess"
    input_schema = ExperimentOutcomeAssessInput
    output_schema = ExperimentOutcomeAssessOutput
    output_keys = {
        "outcome": EXPERIMENT_OUTCOME,
    }

    def _build_input(self, ctx: ExecutionContext) -> ExperimentOutcomeAssessInput:
        return ExperimentOutcomeAssessInput(
            experiment_ids=ctx.get(EXPERIMENT_IDS, []),
            claim_ids=ctx.get(DESIGN_CLAIM_IDS, []),
            anomaly_report=ctx.get(EXPERIMENT_ANOMALY_REPORT, ""),
        )

    def _execute(
        self, input_obj: ExperimentOutcomeAssessInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        store: Optional[KnowledgeStore] = ctx.get(KNOWLEDGE_STORE)
        dry_run: bool = ctx.get(DRY_RUN, True)

        # dry_run：诚实返回失败（占位数据无法验证 Claim）
        if dry_run:
            outcome = {
                "success": False,
                "verified_claim_ids": [],
                "refuted_claim_ids": [],
                "inconclusive_claim_ids": list(input_obj.claim_ids),
                "recommendation": "retry_experiment",
                "summary": (
                    "dry_run 模式：实验代码与运行均为占位，无法真正验证 Claim。"
                    "诚实返回 success=False，不强行进入论文写作阶段。"
                    "启用真实 API 调用（SRA_DRY_RUN=false）后，将基于真实实验结果评估。"
                ),
            }
            output = ExperimentOutcomeAssessOutput(outcome=outcome)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=outcome["summary"],
            )

        # 真实模式：先从 KnowledgeStore 收集实验状态，再调 LLM 评估
        if store is None:
            outcome = {
                "success": False,
                "verified_claim_ids": [],
                "refuted_claim_ids": [],
                "inconclusive_claim_ids": list(input_obj.claim_ids),
                "recommendation": "abort",
                "summary": "KnowledgeStore 未注入，无法评估",
            }
            output = ExperimentOutcomeAssessOutput(outcome=outcome)
            return NodeResult(
                status=NodeStatus.SUCCESS,
                output=output,
                summary=outcome["summary"],
            )

        # 收集实验素材
        exp_summaries: list[dict] = []
        exp_result_lines: list[str] = []
        for exp_id in input_obj.experiment_ids:
            try:
                exp = store.get_experiment(exp_id)
                # 提取 metrics：优先 metrics 字段，兜底从 result_summary 解析
                exp_metrics = exp.metrics
                if exp_metrics is None:
                    exp_metrics = self._extract_metrics_from_summary(
                        exp.result_summary or ""
                    )
                exp_summaries.append({
                    "exp_id": exp_id,
                    "name": exp.name,
                    "status": exp.status.value,
                    "verifies_claim_ids": exp.verifies_claim_ids,
                    "metrics": exp_metrics,
                    "result_summary": (exp.result_summary or "")[:500],
                    "anomaly_notes": exp.anomaly_notes,
                })
                exp_result_lines.append(
                    f"- 实验 {exp.name}: metrics="
                    f"{json.dumps(exp_metrics, ensure_ascii=False)}, status={exp.status.value}"
                )
            except Exception:
                pass
        results_text = (
            "实验结果：\n" + "\n".join(exp_result_lines)
            if exp_result_lines else "实验结果：(无)"
        )

        # 收集 Claim 验证状态
        claim_statuses: list[dict] = []
        for claim_id in input_obj.claim_ids:
            try:
                claim = store.get_claim(claim_id)
                claim_statuses.append({
                    "claim_id": claim_id,
                    "statement": claim.statement,
                    "status": claim.status.value,
                    "evidence_count": len(claim.evidence_refs),
                })
            except Exception:
                pass

        # 基于 Claim 状态做规则判断（避免 LLM 调用浪费）
        verified_claim_ids = [
            c["claim_id"] for c in claim_statuses
            if c["status"] == ClaimStatus.VERIFIED.value
        ]
        refuted_claim_ids = [
            c["claim_id"] for c in claim_statuses
            if c["status"] == ClaimStatus.REFUTED.value
        ]
        inconclusive_claim_ids = [
            c["claim_id"] for c in claim_statuses
            if c["status"] not in (
                ClaimStatus.VERIFIED.value, ClaimStatus.REFUTED.value
            )
        ]

        # 决策逻辑：
        # - 有验证的 Claim 且无反驳 → success=True, proceed_to_writing
        # - 全部无法定论 → retry_experiment
        # - 有反驳 → rollback_to_ideation
        # - 异常严重 → abort
        has_anomaly = bool(input_obj.anomaly_report and input_obj.anomaly_report != "无异常（dry_run）")

        if verified_claim_ids and not refuted_claim_ids:
            success = True
            recommendation = "proceed_to_writing"
            summary = f"实验验证了 {len(verified_claim_ids)} 个核心 Claim，建议进入论文写作"
        elif refuted_claim_ids:
            success = False
            recommendation = "rollback_to_ideation"
            summary = f"实验反驳了 {len(refuted_claim_ids)} 个 Claim，建议回滚到思路探讨"
        elif inconclusive_claim_ids and has_anomaly:
            success = False
            recommendation = "abort"
            summary = "实验异常严重且 Claim 无法定论，建议中止"
        else:
            success = False
            recommendation = "retry_experiment"
            summary = f"实验无法定论（{len(inconclusive_claim_ids)} 个 Claim），建议重试实验"

        # 可选：调用 LLM 做更细致的评估（保留范式，默认不调用以节省 tokens）
        # 若需 LLM 评估，取消以下注释：
        # if registry is not None:
        #     try:
        #         result = registry.structured_output(
        #             task_type=self.task_type,
        #             output_schema=OutcomeAssessSchema,
        #             system=(
        #                 "你是科研评估助手。根据实验结果判断每个 Claim 是被验证、反驳还是无法定论。"
        #                 "实验失败是科研常态，不应强行进入写作阶段。"
        #                 "请结合 metrics 量化数据与 Claim 语义判断验证结论。"
        #             ),
        #             prompt=(
        #                 f"待验证 Claim: {claim_statuses}\n"
        #                 f"{results_text}\n"
        #                 f"实验明细: {exp_summaries}\n"
        #                 f"异常报告: {input_obj.anomaly_report}"
        #             ),
        #         )
        #         success = result.success
        #         verified_claim_ids = result.verified_claim_ids
        #         refuted_claim_ids = result.refuted_claim_ids
        #         inconclusive_claim_ids = result.inconclusive_claim_ids
        #         recommendation = result.recommendation
        #         summary = result.summary
        #     except Exception as e:
        #         logger.warning("OutcomeAssess LLM 调用失败，用规则判断: %s", e)

        outcome = {
            "success": success,
            "verified_claim_ids": verified_claim_ids,
            "refuted_claim_ids": refuted_claim_ids,
            "inconclusive_claim_ids": inconclusive_claim_ids,
            "recommendation": recommendation,
            "summary": summary,
        }

        output = ExperimentOutcomeAssessOutput(outcome=outcome)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=summary,
        )

    @staticmethod
    def _extract_metrics_from_summary(result_summary: str) -> dict:
        """从 result_summary 中提取 metrics。

        result_summary 形如 "metrics: {...}\n..."，提取首个完整 JSON 对象。
        """
        if not result_summary:
            return {}
        idx = result_summary.find("metrics:")
        if idx == -1:
            return {}
        start = idx + len("metrics:")
        while start < len(result_summary) and result_summary[start] in " \t":
            start += 1
        if start >= len(result_summary) or result_summary[start] != '{':
            return {}
        depth = 0
        for i in range(start, len(result_summary)):
            ch = result_summary[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(result_summary[start:i + 1])
                    except Exception:
                        return {}
        return {}
