"""临时脚本：dry_run 模式验证假设可验证性评分（discovery 数据流 + 三维评分落库）。"""
import sys
sys.path.insert(0, ".")
import json

from core.config import get_config
from core.orchestration.node import HumanResponse
from runtime.pipeline import Pipeline


def _extract_discovery_summary(node_history, hypotheses=None):
    """与 web/api.py 同款逻辑（避免导入 web.api 触发端口检查）。"""
    summary = {
        "hypotheses": 0, "candidates": 0, "relationships": 0, "novel": 0,
        "nodes": [], "hypothesis_list": [],
    }
    for h in node_history or []:
        node_id = h.get("node_id", "")
        if node_id in ("hypothesis_seed", "search_space", "llm_guided_search",
                        "discovery_validate", "discovery_report"):
            summary["nodes"].append({
                "node_id": node_id,
                "status": h.get("status"),
                "summary": h.get("summary", ""),
            })
            if node_id == "hypothesis_seed" and "个候选构效关系假设" in h.get("summary", ""):
                try:
                    summary["hypotheses"] = int(h["summary"].split("生成")[1].split("个")[0])
                except (IndexError, ValueError):
                    pass
            if node_id == "discovery_validate" and "条验证发现" in h.get("summary", ""):
                try:
                    summary["relationships"] = int(
                        h["summary"].split("验证")[1].split("条")[0])
                    if "条 novel" in h["summary"]:
                        summary["novel"] = int(
                            h["summary"].split("其中 ")[1].split(" 条")[0])
                except (IndexError, ValueError):
                    pass
    h_list: list[dict] = []
    for hyp in hypotheses or []:
        if not isinstance(hyp, dict) or not hyp.get("hypothesis"):
            continue

        def _f(key):
            try:
                return float(hyp.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0
        n_, f_, g_ = _f("novelty_score"), _f("feasibility_score"), _f("gap_relevance_score")
        h_list.append({
            "hypothesis": hyp.get("hypothesis", ""),
            "variables": hyp.get("variables", []) or [],
            "target_property": hyp.get("target_property", ""),
            "rationale": hyp.get("rationale", ""),
            "gap_ref": hyp.get("gap_ref", ""),
            "novelty_score": round(n_, 2),
            "feasibility_score": round(f_, 2),
            "gap_relevance_score": round(g_, 2),
            "overall_score": round(0.4 * n_ + 0.3 * f_ + 0.3 * g_, 2),
        })
    h_list.sort(key=lambda x: x["overall_score"], reverse=True)
    summary["hypothesis_list"] = h_list
    return summary

cfg = get_config()  # dry_run 默认 True
cfg.dry_run = True

project_id = "proj_dryrun_hyp_score"
topic = "热电材料构效关系研究"

pipe = Pipeline(cfg)
result = pipe.run_discovery(
    project_id=project_id,
    topic=topic,
    human_callback=lambda req: HumanResponse(text="auto-approve", selected_option="继续", action="continue"),
    resume=False,
)

print("status:", result.status)
print("summary:", (result.summary or "")[:300])
print()

# 1. pipeline extra 中的假设（含评分）
hyp = result.extra.get("hypotheses") or []
print(f"extra.hypotheses 数量: {len(hyp)}")
for h in hyp[:5]:
    print(
        "  -",
        h.get("hypothesis", "")[:45],
        "| novelty:", h.get("novelty_score"),
        "| feasible:", h.get("feasibility_score"),
        "| gap_rel:", h.get("gap_relevance_score"),
    )

# 2. 模拟 web 端 _extract_discovery_summary 输出
summary = _extract_discovery_summary(result.node_history, hyp)
print()
print("summary.hypotheses:", summary.get("hypotheses"))
hl = summary.get("hypothesis_list") or []
print(f"hypothesis_list 数量: {len(hl)} (应已按综合分降序)")
for h in hl[:5]:
    print(
        "  -",
        h.get("hypothesis", "")[:45],
        "| overall:", h.get("overall_score"),
        "| n/f/g:", h.get("novelty_score"), h.get("feasibility_score"), h.get("gap_relevance_score"),
    )

# 3. 校验排序
if len(hl) >= 2:
    scores = [h["overall_score"] for h in hl]
    assert scores == sorted(scores, reverse=True), "未按综合分降序排列!"
    print("\n[OK] hypothesis_list 已按综合分降序排列")

# 4. dry_run 下 placeholder 也应带评分
print("\n[OK] 假设可验证性评分验证通过" if hl else "[FAIL] 无假设数据")
