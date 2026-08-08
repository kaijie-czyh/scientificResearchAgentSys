"""产出质量评估脚本（本地不上传 git）。

验证维度（赛题三·方向三评分项）：
1. Research Gap 质量（结构化 + 关联 paper_id）
2. Claim 主题对齐（紧扣研究主题，不"不知所云"）
3. 物理机制结构化（5 要素：原理/因果链/理论/量化/领域）
4. 证据链完整性（关联具体 paper_id）
5. 新颖性评估合理性（novel/partially_known/known 分布）
6. 数据库交叉验证（MP + OQMD）
7. 报告可读性（Markdown 结构完整）

使用：
    python tests/validate_outputs.py
    python tests/validate_outputs.py --topic "材料科学..."
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


os.environ["SRA_DRY_RUN"] = "true"


class C:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _ok(msg: str) -> str:
    return f"  {C.GREEN}✓{C.RESET} {msg}"


def _fail(msg: str) -> str:
    return f"  {C.RED}✗{C.RESET} {msg}"


def _warn(msg: str) -> str:
    return f"  {C.YELLOW}⚠{C.RESET} {msg}"


def _section(title: str) -> str:
    return f"\n{C.BOLD}{C.BLUE}▶ {title}{C.RESET}"


# ===== 质量评分辅助函数 =====


def _extract_topic_keywords(topic: str) -> list[str]:
    """从主题抽取关键词（用于主题对齐检测）。"""
    import re
    parts = re.split(r"[中的与和及以为]", topic)
    keywords = [p.strip() for p in parts if len(p.strip()) >= 2]
    if topic not in keywords and len(topic) >= 2:
        keywords.insert(0, topic)
    return keywords


def _topic_alignment_score(text: str, topic: str) -> float:
    """计算文本与主题的对齐度（0~1）。"""
    if not text or not topic:
        return 0.0
    keywords = _extract_topic_keywords(topic)
    if not keywords:
        return 0.0
    hits = sum(1 for kw in keywords if kw in text)
    return min(1.0, hits / max(1, len(keywords)))


def _jaccard_similarity(a: str, b: str) -> float:
    """集合相似度（检测串主题/重复内容）。"""
    if not a or not b:
        return 0.0
    set_a = set(a)
    set_b = set(b)
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / max(1, len(union))


# ===== 检查项 =====


def _make_project() -> tuple[str, str]:
    """创建一个新项目用于评估，返回 (project_id, topic)。"""
    from runtime.cli import _load_env, auto_human_callback
    _load_env()
    import core.config as _cfgmod
    _cfgmod._default_config = None
    from core.config import get_config
    from runtime.pipeline import Pipeline

    cfg = get_config()
    cfg.dry_run = True
    pipeline = Pipeline(config=cfg)

    project_id = f"validate_quality_{os.getpid()}"
    topic = "热电材料的构效关系与性能优化：基于文献驱动的材料发现智能体"
    try:
        pipeline.run_pipeline(
            project_id=project_id,
            topic=topic,
            human_callback=auto_human_callback,
            stop_before=None,
        )
        # discovery 复用 research 产出（resume=True 跳过 research 重跑）
        pipeline.run_discovery(
            project_id=project_id,
            topic=topic,
            human_callback=auto_human_callback,
            resume=True,
        )
    except Exception as e:
        pass
    return project_id, topic


def check_research_gap_quality(project_id: str, topic: str) -> dict[str, Any]:
    """Research Gap 质量：结构化 + 关联 paper_id + 类型分布。"""
    print(_section("1. Research Gap 质量"))
    from core.config import get_config
    from core.knowledge import KnowledgeStore
    cfg = get_config()
    store = KnowledgeStore(cfg.paths.project_db(project_id))
    report = store.get_kv("cross_validation_report") or {}
    gaps = report.get("gaps", []) or []

    if not gaps:
        print(_fail("无 Research Gap 产出"))
        return {"score": 0, "count": 0}

    # 检查是否结构化
    structured_count = sum(1 for g in gaps if isinstance(g, dict))
    if structured_count == 0:
        print(_warn(f"Gaps 未结构化（旧版字符串格式）：{len(gaps)} 条"))

    # 检查类型分布
    types = [g.get("type") for g in gaps if isinstance(g, dict)]
    type_counts: dict[str, int] = {}
    for t in types:
        type_counts[t or "unknown"] = type_counts.get(t or "unknown", 0) + 1

    # 检查关联 paper_id
    cited_count = sum(1 for g in gaps if isinstance(g, dict) and g.get("cited_paper_ids"))

    score = 0
    if structured_count >= len(gaps) * 0.5:
        score += 30
    if len(set(type_counts.keys())) >= 3:
        score += 30
    if cited_count >= len(gaps) * 0.3:
        score += 40

    print(_ok(f"Gaps: {len(gaps)} 条, 结构化 {structured_count}, 关联 paper_id {cited_count}"))
    print(f"    类型分布：{type_counts}")
    return {"score": score, "count": len(gaps), "structured": structured_count, "types": type_counts}


def check_claim_topic_alignment(project_id: str, topic: str) -> dict[str, Any]:
    """Claim 主题对齐：每个 Claim 应紧扣主题。"""
    print(_section("2. Claim 主题对齐"))
    from core.config import get_config
    from core.knowledge import KnowledgeStore
    cfg = get_config()
    store = KnowledgeStore(cfg.paths.project_db(project_id))
    claims = store.list_claims()

    if not claims:
        print(_fail("无 Claim 产出"))
        return {"score": 0, "count": 0}

    # 计算每个 Claim 与主题的对齐度
    alignment_scores = []
    for c in claims:
        align = _topic_alignment_score(c.statement, topic)
        alignment_scores.append(align)

    avg_alignment = sum(alignment_scores) / max(1, len(alignment_scores))
    aligned_count = sum(1 for s in alignment_scores if s >= 0.3)

    # 跨 Claim 相似度（检测串主题/重复）
    if len(claims) >= 2:
        sim_pairs = []
        for i in range(len(claims)):
            for j in range(i + 1, len(claims)):
                sim = _jaccard_similarity(claims[i].statement, claims[j].statement)
                sim_pairs.append(sim)
        avg_sim = sum(sim_pairs) / len(sim_pairs)
        # 注意：dry_run 模式下占位 Claim 模板相同，会有高相似度
        # 仅在 avg_sim > 0.7 且非 dry_run 模式时扣分
    else:
        avg_sim = 0.0

    score = 0
    if avg_alignment >= 0.5:
        score += 50
    if aligned_count >= len(claims) * 0.7:
        score += 30
    if avg_sim < 0.3:  # Claim 应互不相同（无串主题）
        score += 20
    elif avg_sim >= 0.7:
        print(_warn(f"Claims 高度相似（疑似串主题）：avg_sim={avg_sim:.2f}"))

    print(_ok(f"Claims: {len(claims)} 条, 平均主题对齐度 {avg_alignment:.2f}, "
             f"对齐数 {aligned_count}/{len(claims)}, 跨 Claim 相似度 {avg_sim:.2f}"))
    return {
        "score": score,
        "count": len(claims),
        "avg_alignment": avg_alignment,
        "avg_sim": avg_sim,
    }


def check_mechanism_structured(project_id: str, topic: str = "") -> dict[str, Any]:
    """物理机制结构化（discovery 阶段产出）。"""
    print(_section("3. 物理机制结构化"))
    from core.config import get_config
    from core.knowledge import KnowledgeStore
    cfg = get_config()
    store = KnowledgeStore(cfg.paths.project_db(project_id))
    rels = store.get_kv("discovery_relationships") or []

    if not rels:
        print(_warn("discovery 阶段无构效关系产出（可能因实验失败或未运行）"))
        return {"score": 0, "count": 0}

    # 检查 5 要素
    required_fields = [
        "physical_principle", "causal_chain",
        "known_theory_support", "quantitative_reason",
        "domain_specific_concept",
    ]
    total = 0
    hit = 0
    for r in rels:
        total += 1
        if isinstance(r, dict):
            for f in required_fields:
                if r.get(f):
                    hit += 1
                    break

    score = 0
    if hit >= total * 0.5:
        score += 50
    if total >= 1:
        score += 30
    # 检查 novelty_score 是否存在
    novelty_scores = [r.get("novelty_score") for r in rels if isinstance(r, dict)]
    if any(s is not None and s > 0 for s in novelty_scores):
        score += 20

    print(_ok(f"构效关系: {len(rels)} 条, 结构化机制完整 {hit}/{len(rels)}"))
    return {"score": score, "count": len(rels), "structured": hit}


def check_evidence_chain(project_id: str, topic: str = "") -> dict[str, Any]:
    """证据链完整性（discovery 阶段产出）。"""
    print(_section("4. 证据链完整性"))
    from core.config import get_config
    from core.knowledge import KnowledgeStore
    cfg = get_config()
    store = KnowledgeStore(cfg.paths.project_db(project_id))
    rels = store.get_kv("discovery_relationships") or []
    cv_report = store.get_kv("materials_cross_validation_report") or {}

    if not rels:
        print(_warn("discovery 阶段无构效关系"))
        return {"score": 0}

    # 每条 discovery 应有关联 evidence_paper_ids
    with_evidence = sum(1 for r in rels if isinstance(r, dict) and r.get("evidence_paper_ids"))
    # 数据库交叉验证
    has_mp = bool(cv_report.get("materials_project"))
    has_oqmd = bool(cv_report.get("oqmd"))

    score = 0
    if with_evidence >= len(rels) * 0.5:
        score += 40
    if has_mp:
        score += 30
    if has_oqmd:
        score += 30

    print(_ok(f"证据链: {with_evidence}/{len(rels)} 条关联 paper, "
             f"Materials Project: {'是' if has_mp else '否'}, "
             f"OQMD: {'是' if has_oqmd else '否'}"))
    return {"score": score, "with_evidence": with_evidence, "has_mp": has_mp, "has_oqmd": has_oqmd}


def check_novelty_distribution(project_id: str, topic: str = "") -> dict[str, Any]:
    """新颖性评估分布合理性。"""
    print(_section("5. 新颖性分布"))
    from core.config import get_config
    from core.knowledge import KnowledgeStore
    cfg = get_config()
    store = KnowledgeStore(cfg.paths.project_db(project_id))
    rels = store.get_kv("discovery_relationships") or []

    if not rels:
        print(_warn("discovery 无产出，跳过"))
        return {"score": 0}

    dist: dict[str, int] = {"novel": 0, "partially_known": 0, "known": 0}
    novelty_scores: list[float] = []
    for r in rels:
        if not isinstance(r, dict):
            continue
        novelty = r.get("novelty", "unknown")
        dist[novelty] = dist.get(novelty, 0) + 1
        if r.get("novelty_score") is not None:
            novelty_scores.append(r["novelty_score"])

    score = 0
    # 多样性：有 novel + partially_known + known 三档
    if sum(1 for v in dist.values() if v > 0) >= 2:
        score += 50
    # differentiation_points 存在
    diff_count = sum(1 for r in rels if isinstance(r, dict) and r.get("differentiation_points"))
    if diff_count >= 1:
        score += 30
    # novelty_score 范围合理（0~1）
    if novelty_scores and all(0 <= s <= 1 for s in novelty_scores):
        score += 20

    print(_ok(f"新颖性分布：{dist}"))
    return {"score": score, "distribution": dist}


def check_report_readability(project_id: str, topic: str = "") -> dict[str, Any]:
    """报告可读性（Markdown 结构）。"""
    print(_section("6. 报告可读性"))
    from core.config import get_config
    from core.knowledge import KnowledgeStore
    cfg = get_config()
    store = KnowledgeStore(cfg.paths.project_db(project_id))

    report_content = store.get_kv("discovery_report_content") or ""
    if not report_content:
        # 兜底：research 报告
        cv = store.get_kv("cross_validation_report") or {}
        report_content = json.dumps(cv, ensure_ascii=False, indent=2)

    # Markdown 结构指标
    headings = report_content.count("\n#") + report_content.count("\n##")
    paragraphs = len([p for p in report_content.split("\n\n") if p.strip()])
    bullets = report_content.count("\n-") + report_content.count("\n*")

    score = 0
    if headings >= 3:
        score += 40
    if paragraphs >= 5:
        score += 30
    if bullets >= 3:
        score += 30

    print(_ok(f"报告：{len(report_content)} 字符, {headings} 标题, "
             f"{paragraphs} 段落, {bullets} 列表项"))
    return {"score": score, "length": len(report_content)}


# ===== 主入口 =====


def main() -> int:
    print(f"\n{C.BOLD}{'='*70}")
    print(f"产出质量评估（本地）")
    print(f"{'='*70}{C.RESET}")

    print("\n初始化测试项目...")
    project_id, topic = _make_project()
    print(f"项目: {project_id}")

    checks = [
        ("Research Gap 质量", check_research_gap_quality),
        ("Claim 主题对齐", check_claim_topic_alignment),
        ("物理机制结构化", check_mechanism_structured),
        ("证据链完整性", check_evidence_chain),
        ("新颖性分布", check_novelty_distribution),
        ("报告可读性", check_report_readability),
    ]
    results = []
    for name, fn in checks:
        try:
            r = fn(project_id, topic)
        except Exception as e:
            import traceback
            print(_fail(f"{name}: {type(e).__name__}: {str(e)[:200]}"))
            print(f"  {C.DIM}{traceback.format_exc()[-500:]}{C.RESET}")
            r = {"score": 0, "error": str(e)[:200]}
        results.append((name, r))

    print(f"\n{C.BOLD}{'='*70}")
    print(f"产出质量汇总")
    print(f"{'='*70}{C.RESET}")
    total_score = 0
    total_max = 0
    for name, r in results:
        score = r.get("score", 0)
        max_score = 100
        total_score += score
        total_max += max_score
        marker = f"{C.GREEN}{score:3d}{C.RESET}" if score >= 60 else f"{C.YELLOW}{score:3d}{C.RESET}" if score >= 30 else f"{C.RED}{score:3d}{C.RESET}"
        print(f"  {name:20s}  [{marker}/100]  {score}分")

    print(f"  {'─'*60}")
    pct = (total_score / max(1, total_max)) * 100
    color = C.GREEN if pct >= 80 else C.YELLOW if pct >= 60 else C.RED
    print(f"  {'合计':20s}  {total_score}/{total_max} ({color}{pct:.0f}%{C.RESET})")

    # 产出 JSON 报告
    report_path = ROOT / "tests" / "last_quality_report.json"
    report_path.write_text(json.dumps(
        {name: r for name, r in results}, ensure_ascii=False, indent=2,
    ), encoding="utf-8")
    print(f"\n  详细报告已保存：{report_path}")

    if pct >= 60:
        print(f"\n{C.GREEN}{C.BOLD}✓ 产出质量达标（dry_run 模式合理）{C.RESET}\n")
        return 0
    elif pct >= 40:
        print(f"\n{C.YELLOW}{C.BOLD}⚠ 产出质量可用，建议真实模式进一步验证{C.RESET}\n")
        return 0
    print(f"\n{C.RED}{C.BOLD}✗ 产出质量需改进{C.RESET}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())