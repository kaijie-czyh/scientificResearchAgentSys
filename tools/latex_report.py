"""LaTeX 报告生成器（前后端对齐）。

职责：
1. 从项目 knowledge.db 读取 cross_validation_report / discovery_report_content / claims
2. 填充 LaTeX 模板，生成 project_id 专属的报告
3. 调用 pdflatex + bibtex + pdflatex ×2 编译 PDF
4. 输出到 projects/{id}/artifacts/latex/

调用方式：
    python tools/latex_report.py research --project-id proj_xxx
    python tools/latex_report.py discovery --project-id proj_xxx
    python tools/latex_report.py both --project-id proj_xxx
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# 数据读取
# ============================================================

def _read_kv(db_path: Path, key: str, default: Any = None) -> Any:
    """从 knowledge.db 读 KV 字段。"""
    try:
        from core.knowledge.store import KnowledgeStore
        store = KnowledgeStore(db_path)
        v = store.get_kv(key, default)
        return v
    except Exception as e:
        print(f"  [warn] 读取 {key} 失败：{e}", file=sys.stderr)
        return default


def _read_kv_raw(db_path: Path, key: str) -> str:
    return _read_kv(db_path, key, "")


def _list_claims(db_path: Path) -> list[dict]:
    try:
        from core.knowledge.store import KnowledgeStore
        store = KnowledgeStore(db_path)
        claims = [
            {
                "claim_id": c.claim_id,
                "statement": c.statement,
                "status": c.status.value if hasattr(c.status, "value") else c.status,
                "source_stage": c.source_stage,
            }
            for c in store.list_claims()
        ]
        return claims
    except Exception as e:
        print(f"  [warn] 读取 claims 失败：{e}", file=sys.stderr)
        return []


def _read_research_inputs(project_id: str) -> dict[str, Any]:
    """读取文献调研报告所需的全部数据。"""
    db = PROJECT_ROOT / "projects" / project_id / "knowledge.db"
    if not db.exists():
        raise FileNotFoundError(f"项目数据库不存在：{db}")

    cv = _read_kv(db, "cross_validation_report", {}) or {}
    # topic 提取：优先从交叉验证报告 → 搜索空间 → 兜底
    topic = (
        cv.get("research_topic")
        or cv.get("topic")
        or (_read_kv(db, "discovery_search_space", {}) or {}).get("topic", "")
        or project_id
    )
    subqueries = _read_kv(db, "research.subqueries", []) or []

    # 候选文献数（从 papers 表）
    try:
        import sqlite3
        conn = sqlite3.connect(str(db))
        n_papers = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        conn.close()
    except Exception:
        n_papers = 0

    return {
        "topic": topic,
        "subqueries": subqueries,
        "cv_report": cv,
        "n_papers": n_papers,
    }


def _read_discovery_inputs(project_id: str) -> dict[str, Any]:
    db = PROJECT_ROOT / "projects" / project_id / "knowledge.db"
    if not db.exists():
        raise FileNotFoundError(f"项目数据库不存在：{db}")

    space = _read_kv(db, "discovery_search_space", {}) or {}
    # topic 提取：搜索空间 → 兜底
    topic = space.get("topic") or project_id

    return {
        "report_content": _read_kv_raw(db, "discovery_report_content"),
        "summary": _read_kv(db, "discovery_summary", {}) or {},
        "hypotheses": _read_kv(db, "discovery_hypotheses", []) or [],
        "relationships": _read_kv(db, "discovery_relationships", []) or [],
        "search_space": space,
        "reliability": _read_kv(db, "discovery_reliability_scores", {}) or {},
        "topic": topic,
    }


# ============================================================
# LaTeX 模板生成
# ============================================================

def _escape_tex(s: str) -> str:
    """转义 LaTeX 特殊字符。"""
    if s is None:
        return ""
    s = str(s)
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
        "<": r"\textless{}",
        ">": r"\textgreater{}",
    }
    # 顺序很关键
    for k, v in repl.items():
        s = s.replace(k, v)
    return s


def _build_research_tex(inputs: dict[str, Any]) -> str:
    """根据项目数据生成文献调研报告 LaTeX 源文件。"""
    topic = _escape_tex(inputs.get("topic") or "未指定主题")
    cv = inputs.get("cv_report", {})
    subqueries = inputs.get("subqueries", [])
    n_papers = inputs.get("n_papers", 0)

    # 共识
    consensus = cv.get("consensus") or []
    # 冲突
    conflicts = cv.get("conflicts") or []
    # 缺口
    gaps = cv.get("gaps") or []
    # 整体置信度
    confidence = cv.get("confidence") or cv.get("overall_confidence") or "—"

    subq_lines = "\n".join(
        f"\\item {_escape_tex(str(sq))}" for sq in subqueries[:8]
    ) or "\\item （无子问题记录）"

    cons_lines = ""
    for c in consensus[:6]:
        title = c.get('subquery') or c.get('topic') or c.get('title') or '?'
        body = c.get('statement') or c.get('summary') or c.get('description') or ''
        cons_lines += (
            f"\\item \\textbf{{{_escape_tex(title[:80])}}}："
            f"{_escape_tex(body[:300])}\n"
        )
    if not cons_lines:
        cons_lines = "\\item （暂无共识记录）"

    conf_items = ""
    for cf in conflicts[:6]:
        title = cf.get('subquery') or cf.get('topic') or '?'
        claim = cf.get('claim') or cf.get('topic') or ''
        stance = cf.get('stance') or ''
        resolution = cf.get('resolution') or ''
        conf_items += (
            f"\\item \\textbf{{{_escape_tex(title[:60])}}}："
            f"{_escape_tex(claim[:120])}\\\\"
            f"\\textbf{{置信度}}：{_escape_tex(str(cf.get('confidence', '—')))}，"
            f"\\textbf{{立场}}：{_escape_tex(stance or '—')}\\\\"
            f"\\textbf{{处置}}：{_escape_tex(resolution[:120])}\n"
        )
    if not conf_items:
        conf_items = "\\item （暂无冲突记录）"

    gap_rows = ""
    for g in gaps[:10]:
        gid = g.get('gap_id') or g.get('id') or '—'
        gtype = g.get('gap_type') or g.get('type') or '—'
        prio = g.get('priority') or '—'
        act = g.get('actionability') or '—'
        stmt = g.get('statement') or g.get('description') or ''
        gap_rows += (
            f"{_escape_tex(str(gid)[:20])} & "
            f"{_escape_tex(str(gtype))} & "
            f"{_escape_tex(str(prio))} & "
            f"{_escape_tex(str(act))} & "
            f"{_escape_tex(stmt[:80])} \\\\\n"
        )
    if not gap_rows:
        gap_rows = "— & — & — & — & （暂无 Research Gap） \\\\\n"

    tex = r"""\documentclass[11pt,a4paper]{article}
\usepackage{ctex}
\usepackage[a4paper,margin=2.4cm]{geometry}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{array}
\usepackage{amsmath,amssymb}
\usepackage{xcolor}
\usepackage{hyperref}
\usepackage{url}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage{fancyhdr}
\usepackage{microtype}

\hypersetup{colorlinks=true,linkcolor=blue!60!black,citecolor=blue!60!black,urlcolor=blue!60!black}

\titleformat{\section}{\large\bfseries\color{blue!60!black}}{\thesection}{0.6em}{}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\small SciFinder-Agent 文献调研报告}
\fancyhead[R]{\small """ + _escape_tex(_today()) + r"""}
\fancyfoot[C]{\thepage}

\title{\textbf{""" + topic + r"""} \\[0.3em] \large 文献调研报告（自动生成）}
\author{SciFinder-Agent Team \\ \small \texttt{scifinder-agent@github.com}}
\date{\today}

\begin{document}
\maketitle

\section{概览}
\begin{itemize}[leftmargin=2em]
\item 研究主题：""" + topic + r"""
\item 子问题数：""" + str(len(subqueries)) + r"""
\item 候选文献数：""" + str(n_papers) + r"""
\item 整体调研置信度：""" + _escape_tex(str(confidence)) + r"""
\item 共识条目：""" + str(len(consensus)) + r"""
\item 冲突条目：""" + str(len(conflicts)) + r"""
\item Research Gap 数：""" + str(len(gaps)) + r"""
\end{itemize}

\section{子问题分解}
\begin{itemize}[leftmargin=2em]
""" + subq_lines + r"""
\end{itemize}

\section{文献共识}
\begin{itemize}[leftmargin=2em]
""" + cons_lines + r"""
\end{itemize}

\section{文献冲突与裁决}
\begin{itemize}[leftmargin=2em]
""" + conf_items + r"""
\end{itemize}

\section{Research Gap 清单}
\begin{longtable}{@{}p{2.5cm}p{2cm}p{1.5cm}p{2cm}p{5.5cm}@{}}
\textbf{Gap ID} & \textbf{类型} & \textbf{优先级} & \textbf{可操作性} & \textbf{核心问题} \\
\midrule
\endhead
""" + gap_rows + r"""
\end{longtable}

\section{局限性说明}
本报告由 SciFinder-Agent 系统自动从知识库生成。如需引用具体文献，
请通过项目级 \texttt{evidence\_log} 表追溯原始 paper\_id 与子问题检索记录。
报告内容取决于研究主题与流水线实际产出，可能与人工审阅结果存在差异。

\section{下一步建议}
\begin{enumerate}[leftmargin=2em]
\item 对高优先级 Gap 触发 Discovery 子模块做构效关系候选搜索。
\item 人工裁决未达成共识的冲突文献（IF / 分区 / 时间加权）。
\item 复赛阶段接入 Materials Project API 启用双路交叉验证。
\end{enumerate}

\end{document}
"""
    return tex


def _build_discovery_tex(inputs: dict[str, Any]) -> str:
    """根据项目数据生成构效分析报告 LaTeX 源文件。"""
    topic = _escape_tex(inputs.get("topic") or "未指定主题")
    summary = inputs.get("summary", {}) or {}
    rels = inputs.get("relationships", []) or []
    space = inputs.get("search_space", {}) or {}
    rel_score = inputs.get("reliability", {}) or {}
    topic_meta = space.get("topic", topic)
    n_var = len(space.get("variables", []))
    target = space.get("target_property", "—")

    # 5 维评分摘要
    scores = rel_score.get("scores", []) or []
    score_summary = ""
    if scores:
        for s in scores[:5]:
            dims = s.get("dimensions", {})
            score_summary += (
                f"\\item 可信度 \\textbf{{{_escape_tex(str(s.get('reliability_score', '—')))}}}："
                f"外推安全 {dims.get('extrapolation_safety', '—')} / "
                f"文献密度 {dims.get('literature_density', '—')} / "
                f"机制论证 {dims.get('mechanism_evidence', '—')} / "
                f"CV 一致性 {dims.get('cross_validation_consistency', '—')} / "
                f"区间合理性 {dims.get('interval_reasonability', '—')}\\\\"
                f"\\textbf{{风险标签}}：{_escape_tex(s.get('risk_label', '—'))}\\\\"
                f"\\textbf{{新颖性}}：{_escape_tex(s.get('novelty', '—'))}\n"
            )
    if not score_summary:
        score_summary = "\\item （暂无评分记录）\n"

    # 候选关系
    rel_rows = ""
    for r in rels[:5]:
        rel_rows += (
            f"{_escape_tex(str(r.get('config', {})))} & "
            f"{_escape_tex(str(r.get('predicted_target', '—')))} & "
            f"{_escape_tex(str(r.get('novelty', '—')))} & "
            f"{_escape_tex((r.get('physical_principle') or '')[:80])} \\\\\n"
        )
    if not rel_rows:
        rel_rows = "— & — & — & （暂无候选关系） \\\\\n"

    tex = r"""\documentclass[11pt,a4paper]{article}
\usepackage{ctex}
\usepackage[a4paper,margin=2.4cm]{geometry}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{array}
\usepackage{amsmath,amssymb}
\usepackage{xcolor}
\usepackage{hyperref}
\usepackage{url}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage{fancyhdr}
\usepackage{microtype}

\hypersetup{colorlinks=true,linkcolor=blue!60!black,citecolor=blue!60!black,urlcolor=blue!60!black}

\titleformat{\section}{\large\bfseries\color{blue!60!black}}{\thesection}{0.6em}{}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\small SciFinder-Agent 构效分析报告}
\fancyhead[R]{\small """ + _escape_tex(_today()) + r"""}
\fancyfoot[C]{\thepage}

\title{\textbf{""" + topic + r"""} \\[0.3em] \large 构效关系发现分析报告（自动生成）}
\author{SciFinder-Agent Team \\ \small \texttt{scifinder-agent@github.com}}
\date{\today}

\begin{document}
\maketitle

\section{概览}
\begin{itemize}[leftmargin=2em]
\item 研究主题：""" + topic + r"""
\item Discovery 子主题：""" + _escape_tex(str(topic_meta)) + r"""
\item 目标属性：""" + _escape_tex(str(target)) + r"""
\item 搜索变量数：""" + str(n_var) + r"""
\item 候选假设数：""" + str(summary.get('hypotheses', len(rels))) + r"""
\item 验证发现数：""" + str(summary.get('relationships', len(rels))) + r"""
\item 新颖发现：""" + str(summary.get('novel', '—')) + r"""
\item 部分已知：""" + str(summary.get('partially_known', '—')) + r"""
\end{itemize}

\section{搜索空间}
\begin{itemize}[leftmargin=2em]
\item 目标属性：""" + _escape_tex(str(target)) + r"""
\item 变量：""" + str(n_var) + r""" 个（含 categorical + continuous）
\item 约束条件：""" + _escape_tex(str(space.get('constraints', []) or [])) + r"""
\end{itemize}

\section{构效关系候选清单}
\begin{longtable}{@{}p{5cm}p{1.8cm}p{1.8cm}p{5cm}@{}}
\textbf{配置（材料/参数）} & \textbf{预测目标} & \textbf{新颖性} & \textbf{物理机制} \\
\midrule
\endhead
""" + rel_rows + r"""
\end{longtable}

\section{5 维可信度评分}
\begin{itemize}[leftmargin=2em]
""" + score_summary + r"""
\end{itemize}

\section{局限性分析}
\begin{enumerate}[leftmargin=2em]
\item 评分基于内置物理范围与规则校验；接入 Materials Project API 后可启用双路 CV。
\item 本报告未直接绑定单篇文献 evidence\_refs（系统级 evidence\_log 中可追溯）。
\item 演示环境若未配置 MP API Key，CV 一致性维度仅由规则单路给出。
\end{enumerate}

\section{下一步建议}
\begin{enumerate}[leftmargin=2em]
\item 复赛阶段配置 MATERIALS\_PROJECT\_API\_KEY 启用双路 CV。
\item 对高新颖性候选启动主动学习循环（DFT 仿真 + 实验验证）。
\item 持续扩展 evidence\_log 让候选关系与具体文献一一关联。
\end{enumerate}

\end{document}
"""
    return tex


def _today() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


# ============================================================
# 编译
# ============================================================

def _find_pdflatex() -> str | None:
    """查找 pdflatex 可执行文件。"""
    candidates = [
        shutil.which("pdflatex"),
        "D:/Tex live/texlive/2022/bin/win32/pdflatex",
        "D:/TeX Live/texlive/2022/bin/win32/pdflatex",
        "C:/texlive/2022/bin/win32/pdflatex",
        "/usr/bin/pdflatex",
        "/usr/local/bin/pdflatex",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def _find_bibtex() -> str | None:
    candidates = [
        shutil.which("bibtex"),
        "D:/Tex live/texlive/2022/bin/win32/bibtex",
        "D:/TeX Live/texlive/2022/bin/win32/bibtex",
        "C:/texlive/2022/bin/win32/bibtex",
        "/usr/bin/bibtex",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def _compile_latex(tex_path: Path, work_dir: Path) -> tuple[bool, str]:
    """用 pdflatex + bibtex + pdflatex ×2 编译 .tex。

    返回 (success, log_tail)
    """
    pdflatex = _find_pdflatex()
    bibtex = _find_bibtex()

    if not pdflatex:
        return False, "pdflatex 未找到（请安装 TeX Live 或 MikTeX）"

    name = tex_path.stem
    log_lines: list[str] = []

    def run(cmd: list[str]) -> str:
        try:
            r = subprocess.run(
                cmd,
                cwd=str(work_dir),
                capture_output=True,
                text=False,
                timeout=120,
                env={**os.environ, "PATH": os.path.dirname(pdflatex) + os.pathsep + os.environ.get("PATH", "")},
            )
            out = (r.stdout or b"") + (r.stderr or b"")
            return out.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return f"timeout: {cmd}"
        except Exception as e:
            return f"error: {e}"

    # 1. pdflatex
    out = run([pdflatex, "-interaction=nonstopmode", "-halt-on-error", f"{name}.tex"])
    log_lines.append(out[-400:])

    # 2. bibtex（可选）
    if bibtex:
        out = run([bibtex, name])
        log_lines.append(out[-200:])

    # 3. pdflatex × 2
    for i in range(2):
        out = run([pdflatex, "-interaction=nonstopmode", f"{name}.tex"])
        log_lines.append(out[-200:])

    pdf_path = work_dir / f"{name}.pdf"
    return pdf_path.exists(), "\n".join(log_lines)


# ============================================================
# 主入口
# ============================================================

def build_research_report(project_id: str, output_dir: Path | None = None) -> dict[str, Any]:
    """为指定项目生成文献调研报告（LaTeX + PDF）。

    Returns: {"tex_path": ..., "pdf_path": ..., "compiled": bool, "log": ...}
    """
    inputs = _read_research_inputs(project_id)
    tex_source = _build_research_tex(inputs)

    if output_dir is None:
        output_dir = PROJECT_ROOT / "projects" / project_id / "artifacts" / "latex" / "research"
    output_dir.mkdir(parents=True, exist_ok=True)

    tex_path = output_dir / "report.tex"
    tex_path.write_text(tex_source, encoding="utf-8")

    compiled, log = _compile_latex(tex_path, output_dir)
    pdf_path = output_dir / "report.pdf"

    return {
        "tex_path": str(tex_path),
        "pdf_path": str(pdf_path) if pdf_path.exists() else None,
        "compiled": compiled,
        "log": log,
    }


def build_discovery_report(project_id: str, output_dir: Path | None = None) -> dict[str, Any]:
    inputs = _read_discovery_inputs(project_id)
    tex_source = _build_discovery_tex(inputs)

    if output_dir is None:
        output_dir = PROJECT_ROOT / "projects" / project_id / "artifacts" / "latex" / "discovery"
    output_dir.mkdir(parents=True, exist_ok=True)

    tex_path = output_dir / "discover_report.tex"
    tex_path.write_text(tex_source, encoding="utf-8")

    compiled, log = _compile_latex(tex_path, output_dir)
    pdf_path = output_dir / "discover_report.pdf"

    return {
        "tex_path": str(tex_path),
        "pdf_path": str(pdf_path) if pdf_path.exists() else None,
        "compiled": compiled,
        "log": log,
    }


def main():
    ap = argparse.ArgumentParser(description="SciFinder-Agent LaTeX 报告生成器")
    sub = ap.add_subparsers(dest="kind", required=True)

    for kind in ("research", "discovery", "both"):
        p = sub.add_parser(kind, help=f"生成 {kind} 报告")
        p.add_argument("--project-id", required=True, help="项目 ID")

    args = ap.parse_args()
    if args.kind == "research":
        r = build_research_report(args.project_id)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.kind == "discovery":
        r = build_discovery_report(args.project_id)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.kind == "both":
        r1 = build_research_report(args.project_id)
        r2 = build_discovery_report(args.project_id)
        print(json.dumps({"research": r1, "discovery": r2}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()