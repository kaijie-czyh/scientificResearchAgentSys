"""系统级指标聚合（不依赖单项目，跨项目统计）。

赛题 §4.2 强调"阶段性结果 + 效果分析 + 指标可视化"。
本模块提供：
- SystemMetricsCollector：跨项目扫描，聚合生成 9 大类系统指标
- to_markdown_table：导出为 Markdown 表格（赛题提交时可直接贴入 §4）
- 9 类指标：① 节点完成率 ② KV 字段覆盖率 ③ 文献抓取成功率 ④ 5 维度评分分布
            ⑤ Research Gap 质量分布 ⑥ 跨验证一致性 ⑦ 证据链完整性 ⑧ 降级路径触发率
            ⑨ Token/时间效率

设计原则：
- 只读，不修改项目数据
- 计算结果独立于具体项目，便于评审横向比较
- 所有指标可在 /api/metrics/system 直接 GET 出来
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SystemMetrics:
    """系统级指标聚合结果（9 类 + 摘要）。"""

    # 摘要
    project_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    total_papers: int = 0  # papers 表总行数（直接读 SQLite）
    total_ideas: int = 0  # ideas 表总行数
    total_claims: int = 0  # claims 表总行数
    total_gaps: int = 0  # research_gaps 表总行数
    total_evidence_entries: int = 0  # evidence_log 表总行数

    # 1. 节点完成率（41 节点流水线 + 5 检查点的实际执行情况）
    node_completion: dict[str, float] = field(default_factory=dict)  # node_id -> completed%

    # 2. KV 字段覆盖率（哪些 KV 字段被填、哪些没填）
    kv_coverage: dict[str, float] = field(default_factory=dict)  # kv_key -> fill%

    # 3. 文献抓取成功率（arxiv/S2/Sciverse）
    paper_fetch: dict[str, dict[str, float]] = field(default_factory=dict)

    # 4. 5 维度可信度评分分布（中位数 / P25 / P75）
    reliability_dims: dict[str, dict[str, float]] = field(default_factory=dict)

    # 5. Research Gap 质量分布（4 维度 + 综合分）
    gap_quality: dict[str, dict[str, float]] = field(default_factory=dict)

    # 6. 跨验证一致性（MP / 规则 / 文献三方一致）
    cv_consistency: dict[str, float] = field(default_factory=dict)

    # 7. 证据链完整性（每个阶段的 evidence_log 条目数）
    evidence_chain: dict[str, int] = field(default_factory=dict)

    # 8. 降级路径触发率（缺 key 的次数 / 总次数）
    degradation: dict[str, float] = field(default_factory=dict)

    # 9. 效率（平均耗时 / 平均 LLM 调用次数 / 平均 token 消耗）
    efficiency: dict[str, float] = field(default_factory=dict)

    # 元数据
    generated_at: str = ""
    schema_version: str = "v1.0"


class SystemMetricsCollector:
    """跨项目扫描生成系统级指标。"""

    # 关键 KV 字段（用于覆盖率计算）
    KEY_KV_FIELDS = [
        "research_gaps", "literature_cross_validation", "research_gap_scores",
        "discovery_candidates", "discovery_reliability_scores",
        "materials_cross_validation_report", "hypotheses",
        "claims", "linked_claims", "evidence_log",
        "paper_metas", "filtered_papers", "research_summary",
        "experiment_code", "experiment_results", "paper_draft",
    ]

    # 关键节点 ID（用于完成率计算）
    KEY_NODES = [
        "topic_refine", "subquery_decompose", "topic_confirm",
        "paper_fetch", "paper_filter", "paper_ingest",
        "material_extraction", "cross_validate", "research_gap",
        "brainstorm", "idea_validate", "claim_draft",
        "atom_decompose", "method_formalize", "claim_evidence_link",
        "experiment_config", "code_generate", "code_review", "experiment_run",
        "claim_verify", "experiment_outcome_assess",
        "provenance_check", "style_learn", "outline", "section_draft", "revise",
    ]

    PAPER_SOURCES = ["arxiv", "s2", "sciverse"]

    def __init__(self, projects_dir: Path):
        self.projects_dir = Path(projects_dir)

    def collect(self) -> SystemMetrics:
        """扫描 projects/ 目录，聚合所有项目指标。"""
        m = SystemMetrics()
        m.schema_version = "v1.0"
        m.generated_at = self._now_iso()

        if not self.projects_dir.exists():
            return m

        # 累计器
        node_total = {nid: 0 for nid in self.KEY_NODES}
        node_done = {nid: 0 for nid in self.KEY_NODES}
        kv_total = {k: 0 for k in self.KEY_KV_FIELDS}
        kv_filled = {k: 0 for k in self.KEY_KV_FIELDS}
        fetch_attempts = {s: 0 for s in self.PAPER_SOURCES}
        fetch_success = {s: 0 for s in self.PAPER_SOURCES}
        fetch_results = {s: 0 for s in self.PAPER_SOURCES}

        # 5 维度评分收集
        rel_scores: dict[str, list[float]] = {d: [] for d in [
            "extrapolation_safety", "literature_density",
            "mechanism_argument", "cv_consistency", "prediction_reasonableness"
        ]}

        # Gap 4 维度评分
        gap_scores: dict[str, list[float]] = {d: [] for d in [
            "literature_scarcity", "fillability",
            "action_clarity", "related_strength"
        ]}
        gap_total_scores: list[float] = []

        # CV 一致性
        cv_total = 0
        cv_both = 0
        cv_mp_only = 0
        cv_rule_only = 0

        # 证据链
        ev_by_stage: dict[str, int] = {}

        # 效率
        total_duration_s = 0.0
        total_llm_calls = 0

        # 降级
        deg_mp_missing = 0
        deg_sciverse_missing = 0
        deg_dry_run = 0

        project_dirs = [p for p in self.projects_dir.iterdir() if p.is_dir()]

        for pdir in project_dirs:
            m.project_count += 1
            project_id = pdir.name
            status = self._read_status(pdir)
            if status == "completed":
                m.completed_count += 1
            elif status == "failed":
                m.failed_count += 1

            # 直接读 SQLite 行数（汇总到 m.total_*）
            try:
                import sqlite3
                db = pdir / "knowledge.db"
                if db.exists():
                    conn = sqlite3.connect(str(db))
                    try:
                        tables = {r[0] for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()}
                        def _cnt(t):
                            return int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) \
                                if t in tables else 0
                        m.total_papers += _cnt("papers")
                        m.total_ideas += _cnt("ideas")
                        m.total_claims += _cnt("claims")
                        m.total_gaps += _cnt("research_gaps")
                        m.total_evidence_entries += _cnt("evidence_log")
                    finally:
                        conn.close()
            except Exception:
                pass

            # 节点完成
            history = self._read_history(pdir)
            if history:
                for h in history:
                    nid = h.get("node_id", "")
                    if nid in node_total:
                        node_total[nid] += 1
                        if h.get("status") == "success":
                            node_done[nid] += 1

            # KV 字段
            kv_data = self._read_kv(pdir)
            for k in self.KEY_KV_FIELDS:
                kv_total[k] += 1
                if kv_data.get(k):
                    kv_filled[k] += 1

            # 文献抓取
            paper_metas = kv_data.get("paper_metas") or []
            for pm in paper_metas:
                src = (pm or {}).get("source", "").lower()
                if src in fetch_attempts:
                    fetch_attempts[src] += 1
                    if pm.get("paper_id"):
                        fetch_success[src] += 1
            for src in self.PAPER_SOURCES:
                fetch_results[src] = fetch_success.get(src, 0)

            # 5 维度评分
            rel_kv = kv_data.get("discovery_reliability_scores") or {}
            if isinstance(rel_kv, dict):
                scores_list = rel_kv.get("scores") or []
                for s in scores_list:
                    dims = (s or {}).get("dimensions") or {}
                    for d in rel_scores:
                        v = dims.get(d)
                        if v is not None:
                            rel_scores[d].append(float(v))

            # Gap 评分
            gap_kv = kv_data.get("research_gap_scores") or {}
            if isinstance(gap_kv, dict):
                scores_list = gap_kv.get("scores") or []
                for s in scores_list:
                    dims = (s or {}).get("dimensions") or {}
                    for d in gap_scores:
                        v = dims.get(d)
                        if v is not None:
                            gap_scores[d].append(float(v))
                    if s.get("quality_score") is not None:
                        gap_total_scores.append(float(s["quality_score"]))

            # CV 一致性
            cv_report = kv_data.get("materials_cross_validation_report") or {}
            if isinstance(cv_report, dict):
                results = cv_report.get("results") or []
                for r in results:
                    cv_total += 1
                    mp_match = bool(r.get("mp_match"))
                    rule_pass = bool(r.get("rule_check_passed"))
                    if mp_match and rule_pass:
                        cv_both += 1
                    elif mp_match:
                        cv_mp_only += 1
                    elif rule_pass:
                        cv_rule_only += 1

            # 证据链（优先从 evidence_log 表直接读，回退到 KV）
            ev_log = self._read_evidence_log(pdir)
            if not ev_log:
                ev_log = kv_data.get("evidence_log") or []
            if isinstance(ev_log, list):
                for ev in ev_log:
                    # 从 match_type / source 推 stage；evidence_log 表里没有 stage 列
                    stage = (ev or {}).get("stage") or (ev or {}).get("source") or "unknown"
                    ev_by_stage[stage] = ev_by_stage.get(stage, 0) + 1

            # 直接统计 papers 表行数（更准确）
            paper_count_direct = self._read_paper_count(pdir)
            if paper_count_direct > 0:
                # 用 papers 表的真实数覆盖 paper_metas 推断的
                # （这里我们不直接替换 fetch_attempts，仅作为附加信号）
                pass

            # 效率
            timing = kv_data.get("pipeline_timing") or {}
            if isinstance(timing, dict):
                total_duration_s += float(timing.get("total_seconds", 0))
                total_llm_calls += int(timing.get("llm_calls", 0))

            # 降级
            deg = kv_data.get("degradation_flags") or {}
            if isinstance(deg, dict):
                if deg.get("mp_missing"):
                    deg_mp_missing += 1
                if deg.get("sciverse_missing"):
                    deg_sciverse_missing += 1
                if deg.get("dry_run"):
                    deg_dry_run += 1

        # 聚合
        # 1. 节点完成率
        for nid in self.KEY_NODES:
            tot = node_total[nid]
            if tot > 0:
                m.node_completion[nid] = round(node_done[nid] / tot, 3)

        # 2. KV 覆盖率
        for k in self.KEY_KV_FIELDS:
            tot = kv_total[k]
            if tot > 0:
                m.kv_coverage[k] = round(kv_filled[k] / tot, 3)

        # 3. 文献抓取
        for src in self.PAPER_SOURCES:
            att = fetch_attempts[src]
            succ = fetch_success[src]
            m.paper_fetch[src] = {
                "attempts": float(att),
                "success": float(succ),
                "rate": round(succ / att, 3) if att > 0 else 0.0,
            }

        # 4. 5 维度评分分布
        for d, vals in rel_scores.items():
            if vals:
                m.reliability_dims[d] = {
                    "n": float(len(vals)),
                    "median": round(statistics.median(vals), 3),
                    "p25": round(_percentile(vals, 0.25), 3),
                    "p75": round(_percentile(vals, 0.75), 3),
                    "mean": round(statistics.mean(vals), 3),
                }

        # 5. Gap 质量
        for d, vals in gap_scores.items():
            if vals:
                m.gap_quality[d] = {
                    "n": float(len(vals)),
                    "median": round(statistics.median(vals), 3),
                    "mean": round(statistics.mean(vals), 3),
                }
        if gap_total_scores:
            m.gap_quality["overall"] = {
                "n": float(len(gap_total_scores)),
                "median": round(statistics.median(gap_total_scores), 3),
                "mean": round(statistics.mean(gap_total_scores), 3),
            }

        # 6. CV 一致性
        if cv_total > 0:
            m.cv_consistency = {
                "total_validated": float(cv_total),
                "both_mp_rule_passed": round(cv_both / cv_total, 3),
                "mp_only": round(cv_mp_only / cv_total, 3),
                "rule_only": round(cv_rule_only / cv_total, 3),
                "consistency_rate": round(cv_both / cv_total, 3),
            }

        # 7. 证据链
        m.evidence_chain = dict(ev_by_stage)

        # 8. 降级
        if m.project_count > 0:
            m.degradation = {
                "mp_missing_rate": round(deg_mp_missing / m.project_count, 3),
                "sciverse_missing_rate": round(deg_sciverse_missing / m.project_count, 3),
                "dry_run_rate": round(deg_dry_run / m.project_count, 3),
            }

        # 9. 效率
        if m.completed_count > 0:
            m.efficiency = {
                "avg_duration_seconds": round(total_duration_s / m.completed_count, 1),
                "avg_llm_calls": round(total_llm_calls / m.completed_count, 1),
                "completed_projects": float(m.completed_count),
            }

        return m

    # ------------------- 文件读取辅助 -------------------

    def _read_status(self, pdir: Path) -> str:
        """从 project_state.json 读项目状态。"""
        sf = pdir / "project_state.json"
        if not sf.exists():
            return "unknown"
        try:
            import json
            return (json.loads(sf.read_text(encoding="utf-8")) or {}).get("status", "unknown")
        except Exception:
            return "unknown"

    def _read_history(self, pdir: Path) -> list:
        """从 snapshots/* 读 node history。"""
        snap_dir = pdir / "snapshots"
        if not snap_dir.exists():
            return []
        # 取最新 snapshot
        snaps = sorted(snap_dir.glob("*.json"))
        if not snaps:
            return []
        try:
            import json
            data = json.loads(snaps[-1].read_text(encoding="utf-8"))
            return (data or {}).get("node_history") or []
        except Exception:
            return []

    def _read_evidence_log(self, pdir: Path) -> list:
        """直接读 knowledge.db 的 evidence_log 表（不依赖 KV）。"""
        db = pdir / "knowledge.db"
        if not db.exists():
            return []
        try:
            import sqlite3
            conn = sqlite3.connect(str(db))
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
                if "evidence_log" not in tables:
                    return []
                cur = conn.execute(
                    "SELECT subquery, source, paper_id, title, snippet, match_type, created_at "
                    "FROM evidence_log"
                )
                return [
                    {
                        "subquery": r[0], "source": r[1], "paper_id": r[2],
                        "title": r[3], "snippet": r[4], "match_type": r[5],
                        "created_at": r[6],
                    } for r in cur.fetchall()
                ]
            finally:
                conn.close()
        except Exception:
            return []

    def _read_paper_count(self, pdir: Path) -> int:
        """直接读 papers 表的论文数。"""
        db = pdir / "knowledge.db"
        if not db.exists():
            return 0
        try:
            import sqlite3
            conn = sqlite3.connect(str(db))
            try:
                cur = conn.execute("SELECT COUNT(*) FROM papers")
                return int(cur.fetchone()[0])
            finally:
                conn.close()
        except Exception:
            return 0

    def _read_kv(self, pdir: Path) -> dict:
        """读 knowledge.db 的 kv 表（兼容表名 `kv` 与 `kv_store`）。"""
        db = pdir / "knowledge.db"
        if not db.exists():
            return {}
        try:
            import sqlite3
            conn = sqlite3.connect(str(db))
            try:
                # 探测表名（不同 schema 下表名可能不同）
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
                kv_table = "kv_store" if "kv_store" in tables else (
                    "kv" if "kv" in tables else None
                )
                if not kv_table:
                    return {}
                cur = conn.execute(f"SELECT key, value FROM {kv_table}")
                out = {}
                for k, v in cur.fetchall():
                    try:
                        import json
                        out[k] = json.loads(v)
                    except Exception:
                        out[k] = v
                return out
            finally:
                conn.close()
        except Exception:
            return {}

    def _now_iso(self) -> str:
        from datetime import datetime
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _percentile(values: list[float], q: float) -> float:
    """简单 percentile（不依赖 numpy，避免重依赖）。"""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


# ======================== Markdown 导出 ========================

def to_markdown_table(m: SystemMetrics) -> str:
    """导出为可贴入赛题文档的 Markdown 表格。"""
    lines = [f"## 系统级指标（{m.generated_at}, schema={m.schema_version}）", ""]
    lines.append(f"- **项目总数**：{m.project_count}")
    lines.append(f"- **已完成项目数**：{m.completed_count}")
    lines.append(f"- **失败项目数**：{m.failed_count}")
    lines.append(f"- **论文总条数**（跨项目 papers 表）：{m.total_papers}")
    lines.append(f"- **Ideas 总条数**（ideas 表）：{m.total_ideas}")
    lines.append(f"- **Claims 总条数**（claims 表）：{m.total_claims}")
    lines.append(f"- **Research Gaps 总条数**（research_gaps 表）：{m.total_gaps}")
    lines.append(f"- **证据链总条目数**（evidence_log 表）：{m.total_evidence_entries}")
    lines.append("")

    # 1. 节点完成率
    if m.node_completion:
        lines.append("### 1. 节点完成率（关键 25 节点，按完成率降序）")
        lines.append("")
        lines.append("| 节点 | 完成率 |")
        lines.append("|------|--------|")
        for nid, rate in sorted(m.node_completion.items(), key=lambda x: -x[1]):
            lines.append(f"| {nid} | {rate * 100:.1f}% |")
        lines.append("")

    # 2. KV 覆盖率
    if m.kv_coverage:
        lines.append("### 2. KV 字段覆盖率（15 关键字段）")
        lines.append("")
        lines.append("| KV 字段 | 覆盖率 |")
        lines.append("|---------|--------|")
        for k, rate in sorted(m.kv_coverage.items(), key=lambda x: -x[1]):
            lines.append(f"| {k} | {rate * 100:.1f}% |")
        lines.append("")

    # 3. 文献抓取
    if m.paper_fetch:
        lines.append("### 3. 文献抓取成功率（按源）")
        lines.append("")
        lines.append("| 源 | 尝试 | 成功 | 成功率 |")
        lines.append("|----|------|------|--------|")
        for src, d in m.paper_fetch.items():
            lines.append(f"| {src} | {int(d['attempts'])} | {int(d['success'])} | {d['rate'] * 100:.1f}% |")
        lines.append("")

    # 4. 5 维度评分
    if m.reliability_dims:
        lines.append("### 4. 5 维度可信度评分分布（聚合）")
        lines.append("")
        lines.append("| 维度 | n | 中位数 | P25 | P75 | 均值 |")
        lines.append("|------|---|--------|-----|-----|------|")
        for d, s in m.reliability_dims.items():
            lines.append(f"| {d} | {int(s['n'])} | {s['median']:.2f} | {s['p25']:.2f} | {s['p75']:.2f} | {s['mean']:.2f} |")
        lines.append("")

    # 5. Gap 质量
    if m.gap_quality:
        lines.append("### 5. Research Gap 质量分布（聚合）")
        lines.append("")
        lines.append("| 维度 | n | 中位数 | 均值 |")
        lines.append("|------|---|--------|------|")
        for d, s in m.gap_quality.items():
            lines.append(f"| {d} | {int(s['n'])} | {s['median']:.2f} | {s['mean']:.2f} |")
        lines.append("")

    # 6. CV 一致性
    if m.cv_consistency:
        lines.append("### 6. Materials Project 交叉验证一致性")
        lines.append("")
        for k, v in m.cv_consistency.items():
            lines.append(f"- **{k}** = {v:.3f}" if isinstance(v, float) else f"- **{k}** = {v}")
        lines.append("")

    # 7. 证据链
    if m.evidence_chain:
        lines.append("### 7. 证据链（按阶段落库条目数）")
        lines.append("")
        lines.append("| 阶段 | 条目数 |")
        lines.append("|------|--------|")
        for stage, n in sorted(m.evidence_chain.items(), key=lambda x: -x[1]):
            lines.append(f"| {stage} | {n} |")
        lines.append("")

    # 8. 降级
    if m.degradation:
        lines.append("### 8. 降级路径触发率")
        lines.append("")
        lines.append("| 触发条件 | 触发率 |")
        lines.append("|----------|--------|")
        for k, v in m.degradation.items():
            lines.append(f"| {k} | {v * 100:.1f}% |")
        lines.append("")

    # 9. 效率
    if m.efficiency:
        lines.append("### 9. 流水线效率")
        lines.append("")
        for k, v in m.efficiency.items():
            lines.append(f"- **{k}** = {v}")
        lines.append("")

    return "\n".join(lines)