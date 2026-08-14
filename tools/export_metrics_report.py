"""汇报用：导出系统级指标为 4 种格式（JSON / Markdown / CSV / 简化 HTML）。

用法：
    # 1) 标准三件套（默认输出到 reports/）
    python tools/export_metrics_report.py

    # 2) 指定输出目录 + 前缀
    python tools/export_metrics_report.py --output ./reports --prefix metrics_2026-08-15

    # 3) 只导出某几类指标
    python tools/export_metrics_report.py --only json,markdown

输出文件：
    <output>/<prefix>.json        —— 完整 JSON（含所有 9 类指标）
    <output>/<prefix>.md          —— Markdown（可直接贴汇报 PPT / 文档）
    <output>/<prefix>.csv         —— KV 字段覆盖率 + 文献抓取率 + 5 维度分布（汇总成一张表）
    <output>/<prefix>.html        —— 简化版带表格与样式，邮件发送可直接看

实现：直接复用 core.observability.metrics，不重复逻辑。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.observability.metrics import SystemMetricsCollector, to_markdown_table


def render_csv(m) -> str:
    """把 9 类指标压成一张 CSV（适合 Excel / pandas 进一步处理）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["category", "key", "value"])
    # 摘要
    w.writerow(["summary", "project_count", m.project_count])
    w.writerow(["summary", "completed_count", m.completed_count])
    w.writerow(["summary", "failed_count", m.failed_count])
    # 节点完成率
    for nid, rate in m.node_completion.items():
        w.writerow(["node_completion", nid, f"{rate:.4f}"])
    # KV 覆盖率
    for k, rate in m.kv_coverage.items():
        w.writerow(["kv_coverage", k, f"{rate:.4f}"])
    # 文献抓取
    for src, d in m.paper_fetch.items():
        for k, v in d.items():
            w.writerow([f"paper_fetch.{src}", k, v])
    # 5 维度评分
    for d, s in m.reliability_dims.items():
        for k, v in s.items():
            w.writerow([f"reliability_dims.{d}", k, v])
    # Gap 质量
    for d, s in m.gap_quality.items():
        for k, v in s.items():
            w.writerow([f"gap_quality.{d}", k, v])
    # CV 一致性
    for k, v in m.cv_consistency.items():
        w.writerow(["cv_consistency", k, v])
    # 证据链
    for stage, n in m.evidence_chain.items():
        w.writerow(["evidence_chain", stage, n])
    # 降级
    for k, v in m.degradation.items():
        w.writerow(["degradation", k, v])
    # 效率
    for k, v in m.efficiency.items():
        w.writerow(["efficiency", k, v])
    return buf.getvalue()


def render_html(m, md: str) -> str:
    """简化 HTML（邮件发送 / 嵌入 Wiki 用）。"""
    # 把 Markdown 转 HTML（简单实现：用 <pre> 包裹 + 替换标题 / 表格）
    html = "<!doctype html><html><head><meta charset='utf-8'>"
    html += "<title>系统级指标</title>"
    html += "<style>"
    html += "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:1080px;margin:24px auto;padding:0 16px;color:#222}"
    html += "h1{font-size:1.6em;border-bottom:2px solid #4e54c8;padding-bottom:8px}"
    html += "h2{font-size:1.3em;margin-top:24px;color:#4e54c8}"
    html += "table{border-collapse:collapse;width:100%;margin:8px 0;font-size:14px}"
    html += "th,td{padding:6px 10px;border:1px solid #ddd;text-align:left}"
    html += "th{background:#f4f6fb;font-weight:600}"
    html += ".meta{color:#666;font-size:12px;margin:8px 0 16px}"
    html += "</style></head><body>"
    html += "<h1>系统级指标</h1>"
    html += f"<div class='meta'>生成时间：{m.generated_at} · schema={m.schema_version} · 项目数={m.project_count}</div>"

    # 把 Markdown 转 HTML（极简实现，足够给评委看）
    lines = md.split("\n")
    in_table = False
    table_rows = []
    for line in lines:
        if line.startswith("## "):
            html += f"<h2>{line[3:]}</h2>"
        elif line.startswith("### "):
            html += f"<h3 style='color:#4e54c8'>{line[4:]}</h3>"
        elif line.startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not in_table:
                html += "<table>"
                in_table = True
                # header row
                html += "<thead><tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr></thead><tbody>"
            else:
                if all(set(c) <= set("-: ") for c in cells):
                    continue  # skip separator
                html += "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
        elif line.startswith("- "):
            if in_table:
                html += "</tbody></table>"
                in_table = False
            html += f"<div>• {line[2:]}</div>"
        else:
            if in_table and line.strip() == "":
                html += "</tbody></table>"
                in_table = False
            if line.strip():
                html += f"<p>{line}</p>"
    if in_table:
        html += "</tbody></table>"
    html += "</body></html>"
    return html


def main() -> int:
    ap = argparse.ArgumentParser(description="导出系统级指标为 4 种格式")
    ap.add_argument("--output", default="./reports", help="输出目录（默认 ./reports）")
    ap.add_argument("--prefix", default=None, help="=")
    ap.add_argument(
        "--only", default="json,markdown,csv,html",
        help="导出哪些格式（逗号分隔），默认全 4 种"
    )
    ap.add_argument("--projects-dir", default=None,
                    help="项目目录（默认走 core.config.paths.projects）")
    args = ap.parse_args()

    # 1. 收集
    if args.projects_dir:
        collector = SystemMetricsCollector(Path(args.projects_dir))
    else:
        from core.config import get_config
        collector = SystemMetricsCollector(get_config().paths.projects)
    m = collector.collect()

    # 2. 准备输出目录 + 前缀
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = args.prefix or f"metrics_{ts}"

    # 3. 导出
    formats = set(args.only.split(","))
    written = []
    if "json" in formats:
        from dataclasses import asdict
        p = output_dir / f"{prefix}.json"
        p.write_text(json.dumps(asdict(m), ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)
    if "markdown" in formats:
        p = output_dir / f"{prefix}.md"
        p.write_text(to_markdown_table(m), encoding="utf-8")
        written.append(p)
    if "csv" in formats:
        p = output_dir / f"{prefix}.csv"
        p.write_text(render_csv(m), encoding="utf-8")
        written.append(p)
    if "html" in formats:
        md = to_markdown_table(m)
        p = output_dir / f"{prefix}.html"
        p.write_text(render_html(m, md), encoding="utf-8")
        written.append(p)

    print(f"[OK] 生成 {len(written)} 个报告文件：")
    for p in written:
        print(f"  - {p}  ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())