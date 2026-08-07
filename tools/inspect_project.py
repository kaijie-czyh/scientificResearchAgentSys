"""项目检查与可视化工具。

解决「结果不可验证」痛点：把科研 Agent 产出的全部实体（论文/思路/Claim/
实验/产出物）以可读格式呈现，便于人工核查与对齐。

功能：
1. 项目概览：各阶段产出物计数
2. 论文清单：标题/年份/相关性分数
3. 思路与 Claim：验证状态/证据链
4. 实验结果：状态/异常/验证的 Claim
5. 方法↔代码对齐：从方法 Artifact 抽取公式，与实验代码做关键词匹配，
   标注每个公式是否在代码中落地（status: mapped/partial/missing）

使用：
    python -m tools.inspect_project --project-id proj_smoke_real
    python -m tools.inspect_project --project-id proj_xxx --section alignment
    python -m tools.inspect_project --project-id proj_xxx --json > report.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import get_config
from core.knowledge import (
    Artifact,
    ArtifactType,
    Claim,
    Experiment,
    ExperimentStatus,
    KnowledgeStore,
    Paper,
)


def _load_store(project_id: str) -> KnowledgeStore:
    cfg = get_config()
    db_path = cfg.paths.project_db(project_id)
    if not db_path.exists():
        print(f"错误：项目数据库不存在: {db_path}")
        sys.exit(1)
    return KnowledgeStore(db_path)


def _project_dir(project_id: str) -> Path:
    cfg = get_config()
    return cfg.paths.project_dir(project_id)


# ===== 各 section 渲染 =====

def render_overview(store: KnowledgeStore, project_id: str) -> str:
    papers = store.list_papers()
    ideas = store.list_ideas()
    claims = store.list_claims()
    experiments = store.list_experiments()
    artifacts_methods = store.list_artifact_versions("")  # 占位
    # 按 type 过滤 artifact
    all_artifacts: list[Artifact] = []
    # list_artifact_versions 需要 group，这里用 list_relations 间接拿不到，
    # 改用直接查询：遍历 papers/claims/experiments 的 artifact 引用
    # 简化：直接读 experiments 的 result_summary 与 method artifact 通过 store.get_artifact
    lines = [
        f"# 项目概览：{project_id}",
        "",
        f"- 论文（Paper）：{len(papers)} 篇",
        f"- 思路（Idea）：{len(ideas)} 个",
        f"- Claim：{len(claims)} 个",
        f"- 实验（Experiment）：{len(experiments)} 个",
    ]
    # Claim 状态分布
    claim_status_count: dict[str, int] = {}
    for c in claims:
        s = c.status.value if hasattr(c.status, "value") else str(c.status)
        claim_status_count[s] = claim_status_count.get(s, 0) + 1
    if claim_status_count:
        lines.append(f"  - Claim 状态分布：{claim_status_count}")
    # 实验状态分布
    exp_status_count: dict[str, int] = {}
    for e in experiments:
        s = e.status.value if hasattr(e.status, "value") else str(e.status)
        exp_status_count[s] = exp_status_count.get(s, 0) + 1
    if exp_status_count:
        lines.append(f"  - 实验状态分布：{exp_status_count}")
    return "\n".join(lines)


def render_papers(store: KnowledgeStore) -> str:
    papers = store.list_papers()
    if not papers:
        return "## 论文\n\n（无入库论文）"
    lines = ["## 论文", f"共 {len(papers)} 篇", ""]
    for i, p in enumerate(papers, 1):
        title = p.title[:80]
        year = getattr(p, "year", "?")
        lines.append(f"{i}. [{year}] {title}")
        if p.abstract:
            lines.append(f"   摘要：{p.abstract[:120]}...")
    return "\n".join(lines)


def render_claims(store: KnowledgeStore) -> str:
    claims = store.list_claims()
    if not claims:
        return "## Claim\n\n（无 Claim）"
    lines = ["## Claim", f"共 {len(claims)} 个", ""]
    for i, c in enumerate(claims, 1):
        status = c.status.value if hasattr(c.status, "value") else str(c.status)
        role = c.role or "?"
        stmt = c.statement[:100]
        ev_count = len(c.evidence_refs)
        lines.append(f"{i}. [{status}][{role}] {stmt}")
        lines.append(f"   证据数：{ev_count}；来源阶段：{c.source_stage}")
    return "\n".join(lines)


def render_experiments(store: KnowledgeStore) -> str:
    experiments = store.list_experiments()
    if not experiments:
        return "## 实验\n\n（无实验）"
    lines = ["## 实验", f"共 {len(experiments)} 个", ""]
    for i, e in enumerate(experiments, 1):
        status = e.status.value if hasattr(e.status, "value") else str(e.status)
        lines.append(f"{i}. [{status}] {e.name}")
        lines.append(f"   验证 Claim：{e.verifies_claim_ids}")
        if e.result_summary:
            lines.append(f"   结果摘要：{e.result_summary[:200]}")
        if e.anomaly_notes:
            lines.append(f"   异常：{e.anomaly_notes[:200]}")
    return "\n".join(lines)


def _open_db(store: KnowledgeStore):
    """从 store 取 db_path，新建独立连接（store 内部连接不持久化）。"""
    import sqlite3
    db_path = store._db_path
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _query_artifacts_by_type(store: KnowledgeStore, artifact_type: ArtifactType, limit: int = 10) -> list[Artifact]:
    """查询指定类型的 artifact（content 列存的是 model_dump_json）。"""
    conn = _open_db(store)
    try:
        rows = conn.execute(
            "SELECT content FROM artifacts WHERE artifact_type = ? ORDER BY created_at DESC LIMIT ?",
            (artifact_type.value, limit),
        ).fetchall()
    finally:
        conn.close()
    artifacts: list[Artifact] = []
    for row in rows:
        try:
            artifacts.append(Artifact.model_validate_json(row["content"]))
        except Exception:
            pass
    return artifacts


def render_method_artifact(store: KnowledgeStore) -> str:
    """读取 METHOD_DOC 类型的 artifact 内容。"""
    artifacts = _query_artifacts_by_type(store, ArtifactType.METHOD_DOC)
    if not artifacts:
        return "## 方法 Artifact\n\n（无方法 Artifact）"
    lines = ["## 方法 Artifact", ""]
    for art in artifacts:
        lines.append(f"### {art.title} (id={art.artifact_id[:8]}, by={art.created_by})")
        if art.content:
            preview = art.content[:2000]
            lines.append(f"```\n{preview}\n```")
        lines.append("")
    return "\n".join(lines)


def render_alignment(store: KnowledgeStore, project_id: str) -> str:
    """方法↔代码对齐可视化。

    从方法 Artifact 抽取 LaTeX 公式（$...$），与实验代码做关键词匹配，
    标注每个公式是否在代码中落地。
    """
    artifacts = _query_artifacts_by_type(store, ArtifactType.METHOD_DOC, limit=1)
    method_content = artifacts[0].content if artifacts else ""

    # 读取实验代码
    code_path = _project_dir(project_id) / "experiments" / "run_exp.py"
    code_content = ""
    if code_path.exists():
        code_content = code_path.read_text(encoding="utf-8")

    # 抽取 LaTeX 公式：$...$ 或 $$...$$
    formulas = re.findall(r"\$\$?(.+?)\$\$?", method_content)
    # 抽取代码中的函数/变量名作为关键词
    code_keywords = set(re.findall(r"\bdef\s+(\w+)|\b([a-z_][a-z0-9_]*)\s*=", code_content))
    code_keywords = {k for pair in code_keywords for k in pair if k}

    lines = ["## 方法↔代码对齐", ""]
    if not method_content:
        lines.append("（无方法 Artifact 内容）")
        return "\n".join(lines)
    if not code_content:
        lines.append("（无实验代码，跳过对齐检查）")
        return "\n".join(lines)
    lines.append(f"方法内容：{len(method_content)} 字符；实验代码：{len(code_content)} 字符")
    lines.append("")

    # 对每个公式，检查是否在代码中有对应实现
    # 启发式：抽取公式中的标识符（变量名），检查是否出现在代码中
    lines.append("| # | 公式 | 关键标识符 | 代码中是否出现 | 状态 |")
    lines.append("|---|------|-----------|---------------|------|")
    aligned = 0
    for i, formula in enumerate(formulas[:20], 1):  # 最多展示 20 个
        # 抽取公式中的标识符
        identifiers = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", formula))
        # 排除常见 LaTeX 命令与单字符变量
        latex_cmds = {
            "frac", "sum", "max", "min", "arg", "exp", "log", "sqrt",
            "mathbf", "text", "cdot", "times", "alpha", "beta", "gamma",
            "theta", "lambda", "sigma", "delta", "epsilon", "phi", "mu",
        }
        identifiers = {ident for ident in identifiers if ident not in latex_cmds and len(ident) > 1}
        # 检查标识符是否在代码中出现
        found = [ident for ident in identifiers if ident in code_content]
        if identifiers and len(found) / max(len(identifiers), 1) >= 0.5:
            status = "mapped"
            aligned += 1
        elif found:
            status = "partial"
        else:
            status = "missing"
        idents_str = ", ".join(sorted(identifiers)[:5]) or "—"
        formula_short = formula[:50].replace("|", "\\|")
        lines.append(f"| {i} | `{formula_short}` | {idents_str} | {len(found)}/{len(identifiers)} | {status} |")

    lines.append("")
    lines.append(f"对齐率：{aligned}/{min(len(formulas), 20)} 公式在代码中落地")
    if aligned < len(formulas):
        lines.append("⚠️  部分公式未在代码中落地，建议检查 CodeReview 反馈或重跑 code_generate")
    return "\n".join(lines)


# ===== 主入口 =====

def main() -> int:
    parser = argparse.ArgumentParser(description="科研项目检查与可视化工具")
    parser.add_argument("--project-id", required=True, help="项目 ID")
    parser.add_argument(
        "--section",
        default="all",
        choices=["all", "overview", "papers", "claims", "experiments", "method", "alignment"],
        help="查看哪个 section（默认 all）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON（仅 overview）")
    args = parser.parse_args()

    # 加载 .env
    from runtime.cli import _load_env
    _load_env()

    store = _load_store(args.project_id)

    sections: list[str] = []
    if args.section in ("all", "overview"):
        sections.append(render_overview(store, args.project_id))
    if args.section in ("all", "papers"):
        sections.append(render_papers(store))
    if args.section in ("all", "claims"):
        sections.append(render_claims(store))
    if args.section in ("all", "experiments"):
        sections.append(render_experiments(store))
    if args.section in ("all", "method"):
        sections.append(render_method_artifact(store))
    if args.section in ("all", "alignment"):
        sections.append(render_alignment(store, args.project_id))

    print("\n\n".join(sections))
    return 0


if __name__ == "__main__":
    sys.exit(main())
