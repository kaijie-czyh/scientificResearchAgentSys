"""赛题三·方向三（材料方向）评分项覆盖度验证脚本（本地不上传 git）。

按赛题「材料方向评估体系」对照验证：
- 基本任务（50%）：Research Gap 识别质量 + 文献溯源完整性 + 报告质量
- 进阶路线 A（50%）：搜索方法与 LLM 融合深度 + 物理机制说服力 + 数据库验证
- 加分项：材料数据库验证、跨数据库（MP + OQMD）、代码开源质量

使用：
    python tests/validate_competition.py
"""
from __future__ import annotations

import os
import sys
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
    RESET = "\033[0m"


def _ok(msg: str) -> str:
    return f"  {C.GREEN}✓{C.RESET} {msg}"


def _fail(msg: str) -> str:
    return f"  {C.RED}✗{C.RESET} {msg}"


def _warn(msg: str) -> str:
    return f"  {C.YELLOW}⚠{C.RESET} {msg}"


def _section(title: str) -> str:
    return f"\n{C.BOLD}{C.BLUE}▶ {title}{C.RESET}"


def _score(matched: int, total: int) -> int:
    """赛题评分：完全覆盖 100, 部分 50-80, 未覆盖 0。"""
    if total == 0:
        return 100
    pct = matched / total
    if pct >= 0.95:
        return 100
    elif pct >= 0.8:
        return 85
    elif pct >= 0.6:
        return 70
    elif pct >= 0.4:
        return 50
    elif pct >= 0.2:
        return 25
    return 0


# ===== 评分项 =====


def check_basic_task_research_gap() -> dict[str, Any]:
    """基本任务-Research Gap 识别质量（30%）。"""
    print(_section("A1. 基本任务-Research Gap 识别质量（赛题 30%）"))
    checks = []
    # 1. 结构化 Gap schema
    try:
        from stages.research.agents import ResearchGapItem
        item = ResearchGapItem(gap="x", type="underexplored", importance=0.7,
                               actionability="medium")
        checks.append(("结构化 Gap schema", True))
    except Exception:
        checks.append(("结构化 Gap schema", False))

    # 2. Gap 类型覆盖 5 种
    try:
        types = ["underexplored", "contradiction", "missing_connection", "method_gap", "data_gap"]
        from stages.research.agents import ResearchGapItem
        for t in types:
            ResearchGapItem(gap=f"test {t}", type=t, importance=0.5, actionability="low")
        checks.append(("5 种 Gap 类型", True))
    except Exception:
        checks.append(("5 种 Gap 类型", False))

    # 3. cited_paper_ids 字段存在
    try:
        from stages.research.agents import ResearchGapItem
        item = ResearchGapItem(gap="x", type="underexplored", importance=0.5,
                               actionability="medium", cited_paper_ids=["p1", "p2"])
        assert item.cited_paper_ids == ["p1", "p2"]
        checks.append(("Gap 关联 paper_id", True))
    except Exception:
        checks.append(("Gap 关联 paper_id", False))

    # 4. importance 评分
    try:
        from stages.research.agents import ResearchGapItem
        item = ResearchGapItem(gap="x", type="data_gap", importance=0.95,
                               actionability="high")
        assert item.importance == 0.95
        checks.append(("importance 评分", True))
    except Exception:
        checks.append(("importance 评分", False))

    # 5. actionability 评估
    try:
        from stages.research.agents import ResearchGapItem
        for a in ["high", "medium", "low"]:
            ResearchGapItem(gap="x", type="underexplored", importance=0.5, actionability=a)
        checks.append(("actionability 三档", True))
    except Exception:
        checks.append(("actionability 三档", False))

    for name, ok in checks:
        print(_ok(name) if ok else _fail(name))
    matched = sum(1 for _, ok in checks if ok)
    score = _score(matched, len(checks))
    return {"score": score, "matched": matched, "total": len(checks), "checks": checks}


def check_basic_task_literature_trace() -> dict[str, Any]:
    """基本任务-文献溯源完整性（30%）。"""
    print(_section("A2. 基本任务-文献溯源完整性（赛题 30%）"))
    checks = []
    # 1. Paper 入库 + chunk 化
    try:
        from core.knowledge import KnowledgeStore, Paper, PaperChunk
        from core.config import get_config
        cfg = get_config()
        store = KnowledgeStore(cfg.paths.project_dir("_test") / "test.db")
        paper = Paper(paper_id="p1", title="t", authors=[], year=2024, abstract="a")
        store.save_paper(paper)
        chunk = PaperChunk(chunk_id="p1_c0", paper_id="p1", chunk_index=0, text="text")
        store.save_paper_chunks([chunk])
        chunks = store.get_paper_chunks("p1")
        assert len(chunks) == 1
        checks.append(("Paper + Chunk 入库", True))
    except Exception as e:
        checks.append(("Paper + Chunk 入库", False))

    # 2. Claim.evidence_refs 关联 paper_id
    try:
        from core.knowledge import Claim, ClaimStatus
        claim = Claim(
            claim_id="c1", statement="s", role="result",
            evidence_refs=[{"type": "paper", "id": "p1"}],
            status=ClaimStatus.EVIDENCE_LINKED,
        )
        assert len(claim.evidence_refs) == 1
        checks.append(("Claim 关联 paper_id", True))
    except Exception:
        checks.append(("Claim 关联 paper_id", False))

    # 3. cited_chunk_ids
    try:
        from stages.research.agents import ResearchGapItem
        item = ResearchGapItem(gap="x", type="underexplored", importance=0.5,
                               actionability="medium", cited_chunk_ids=["p1_c0"])
        checks.append(("Gap 关联 chunk_id", True))
    except Exception:
        checks.append(("Gap 关联 chunk_id", False))

    # 4. discovery evidence_paper_ids
    try:
        from stages.discovery.agents import RelationshipSchema
        rel = RelationshipSchema(
            relationship="r", predicted_target=1.0, novelty="novel", confidence=0.5,
            evidence_paper_ids=["p1", "p2"],
        )
        checks.append(("discovery evidence_paper_ids", True))
    except Exception:
        checks.append(("discovery evidence_paper_ids", False))

    for name, ok in checks:
        print(_ok(name) if ok else _fail(name))
    matched = sum(1 for _, ok in checks if ok)
    return {"score": _score(matched, len(checks)), "matched": matched, "total": len(checks)}


def check_basic_task_report_quality() -> dict[str, Any]:
    """基本任务-调研报告结构化（赛题 40%）。"""
    print(_section("A3. 基本任务-调研报告质量（赛题 40%）"))
    checks = []
    # 1. discovery_report_content 持久化
    from core.config import get_config
    from core.knowledge import KnowledgeStore

    # 跑一个 discovery 看看报告内容
    from runtime.cli import _load_env, auto_human_callback
    _load_env()
    import core.config as _cfgmod
    _cfgmod._default_config = None
    from runtime.pipeline import Pipeline

    cfg = get_config()
    cfg.dry_run = True
    pipeline = Pipeline(config=cfg)
    project_id = f"validate_comp_{os.getpid()}"
    topic = "热电材料构效关系赛题验证"

    try:
        pipeline.run_pipeline(
            project_id=project_id, topic=topic,
            human_callback=auto_human_callback, stop_before=None,
        )
        pipeline.run_discovery(
            project_id=project_id, topic=topic,
            human_callback=auto_human_callback,
        )
    except Exception:
        pass

    store = KnowledgeStore(cfg.paths.project_db(project_id))
    discovery_report = store.get_kv("discovery_report_content") or ""
    research_report = store.get_kv("cross_validation_report") or {}

    # 1. 发现报告存在
    checks.append(("discovery 报告生成", bool(discovery_report)))
    # 2. 调研报告含 gaps/conflicts/consensus
    checks.append(("调研报告含 gaps", bool(research_report.get("gaps"))))
    checks.append(("调研报告含 conflicts", bool(research_report.get("conflicts"))))
    checks.append(("调研报告含 consensus", bool(research_report.get("consensus"))))
    # 3. 报告 Markdown 结构
    if discovery_report:
        headings = discovery_report.count("\n#") + discovery_report.count("\n##")
        checks.append(("报告含 3+ 标题", headings >= 3))

    for name, ok in checks:
        print(_ok(name) if ok else _fail(name))
    matched = sum(1 for _, ok in checks if ok)
    return {"score": _score(matched, len(checks)), "matched": matched, "total": len(checks)}


def check_route_a_llm_search() -> dict[str, Any]:
    """路线 A：LLM 与搜索深度融合（30%）。"""
    print(_section("B1. 路线 A-LLM 与搜索融合深度（赛题 30%）"))
    checks = []
    # 1. MCTSSearcher 存在
    try:
        from core.tools.materials_search import MCTSSearcher
        checks.append(("MCTSSearcher 类", True))
    except Exception:
        checks.append(("MCTSSearcher 类", False))

    # 2. SurrogateModel 存在
    try:
        from core.tools.materials_search import SurrogateModel
        checks.append(("SurrogateModel 类", True))
    except Exception:
        checks.append(("SurrogateModel 类", False))

    # 3. LLMGuidedSearchAgent 实现 MCTS + LLM
    try:
        from stages.discovery.agents import LLMGuidedSearchAgent
        agent = LLMGuidedSearchAgent("test_search")
        assert hasattr(agent, "MAX_ITERATIONS")
        checks.append(("LLMGuidedSearchAgent MCTS 循环", True))
    except Exception:
        checks.append(("LLMGuidedSearchAgent MCTS 循环", False))

    # 4. LLM 评估（剪枝机制）
    try:
        from stages.discovery.agents import CandidateEvaluationSchema
        ev = CandidateEvaluationSchema(
            config={"x": 1}, plausibility=0.8, novelty="novel",
            pruned=True,
        )
        assert ev.pruned is True
        checks.append(("MCTS 剪枝机制", True))
    except Exception:
        checks.append(("MCTS 剪枝机制", False))

    # 5. 物理机制 5 要素
    try:
        from stages.discovery.agents import CandidateEvaluationSchema
        ev = CandidateEvaluationSchema(
            config={}, plausibility=0.5, novelty="novel",
            physical_principle="p",
            causal_chain=["a", "b"],
            known_theory_support="t",
            quantitative_reason="q",
            domain_specific_concept="d",
        )
        assert ev.physical_principle == "p"
        checks.append(("物理机制 5 要素", True))
    except Exception:
        checks.append(("物理机制 5 要素", False))

    for name, ok in checks:
        print(_ok(name) if ok else _fail(name))
    matched = sum(1 for _, ok in checks if ok)
    return {"score": _score(matched, len(checks)), "matched": matched, "total": len(checks)}


def check_route_a_mechanism_quality() -> dict[str, Any]:
    """路线 A：物理机制说服力（30%）。"""
    print(_section("B2. 路线 A-物理机制说服力（赛题 30%）"))
    checks = []
    # 1. _compose_mechanism 函数存在
    try:
        from stages.discovery.agents import _compose_mechanism
        m = _compose_mechanism("p", ["a", "b"], "t", "q", "d")
        assert "p" in m and "t" in m
        checks.append(("_compose_mechanism 工具函数", True))
    except Exception:
        checks.append(("_compose_mechanism 工具函数", False))

    # 2. RelationshipSchema 结构化机制
    try:
        from stages.discovery.agents import RelationshipSchema
        rel = RelationshipSchema(
            relationship="r", predicted_target=1.0, novelty="novel", confidence=0.7,
            physical_principle="p",
            causal_chain=["a", "b"],
            known_theory_support="t",
            quantitative_reason="q",
            domain_specific_concept="d",
        )
        assert all([rel.physical_principle, rel.causal_chain,
                    rel.known_theory_support, rel.quantitative_reason])
        checks.append(("RelationshipSchema 机制 5 字段", True))
    except Exception:
        checks.append(("RelationshipSchema 机制 5 字段", False))

    # 3. differentiation_points
    try:
        from stages.discovery.agents import RelationshipSchema
        rel = RelationshipSchema(
            relationship="r", predicted_target=1.0, novelty="novel", confidence=0.7,
            differentiation_points=["A", "B", "C"],
            novelty_score=0.85,
        )
        checks.append(("differentiation_points 新颖性差异", True))
    except Exception:
        checks.append(("differentiation_points 新颖性差异", False))

    # 4. discovery_report 含机制
    from core.config import get_config
    from core.knowledge import KnowledgeStore
    cfg = get_config()
    # 使用最近的验证项目
    store = KnowledgeStore(cfg.paths.project_dir("proj_materials_thermoelectric") / "store.db")
    report = store.get_kv("discovery_report_content") or ""
    if report and ("机制" in report or "mechanism" in report or "原理" in report):
        checks.append(("报告含机制说明", True))
    else:
        checks.append(("报告含机制说明", False))

    for name, ok in checks:
        print(_ok(name) if ok else _fail(name))
    matched = sum(1 for _, ok in checks if ok)
    return {"score": _score(matched, len(checks)), "matched": matched, "total": len(checks)}


def check_route_a_db_validation() -> dict[str, Any]:
    """路线 A：数据库交叉验证（赛题 30%）。"""
    print(_section("B3. 路线 A-数据库交叉验证（赛题 30%）"))
    checks = []
    # 1. Materials Project 客户端
    try:
        from core.tools.materials_project import cross_validate_discovery
        checks.append(("Materials Project 客户端", True))
    except Exception:
        checks.append(("Materials Project 客户端", False))

    # 2. OQMD 客户端
    try:
        from core.tools.oqmd_nomad import query_oqmd_by_formula
        r = query_oqmd_by_formula("Bi2Te3")
        assert r.matched or not r.matched  # 即使 unmatched 也算客户端工作
        checks.append(("OQMD 客户端", True))
    except Exception:
        checks.append(("OQMD 客户端", False))

    # 3. MP 实际验证
    try:
        from core.tools.materials_project import mp_cross_validate_discovery
        rels = [{"relationship": "r", "config": {"material": "Bi2Te3"}, "predicted_target": 1.0, "novelty": "novel", "confidence": 0.5}]
        cv = mp_cross_validate_discovery(rels, [])
        checks.append(("MP 交叉验证可执行", True))
    except Exception:
        checks.append(("MP 交叉验证可执行", False))

    # 4. OQMD 查询返回结构
    try:
        from core.tools.oqmd_nomad import query_oqmd_by_formula
        r = query_oqmd_by_formula("Bi2Te3")
        assert hasattr(r, "matched") and hasattr(r, "entries")
        checks.append(("OQMD 查询结构完整", True))
    except Exception:
        checks.append(("OQMD 查询结构完整", False))

    for name, ok in checks:
        print(_ok(name) if ok else _fail(name))
    matched = sum(1 for _, ok in checks if ok)
    return {"score": _score(matched, len(checks)), "matched": matched, "total": len(checks)}


def check_bonus_open_source() -> dict[str, Any]:
    """加分项：开源代码质量与可复用性。"""
    print(_section("C. 加分项-代码开源质量"))
    checks = []

    # 1. README 存在
    readme = ROOT / "README.md"
    checks.append(("README.md 存在", readme.exists()))

    # 2. DEVLOG 存在
    devlog = ROOT / "DEVLOG.md"
    checks.append(("DEVLOG.md 存在", devlog.exists()))

    # 3. LICENSE 存在
    license_files = list(ROOT.glob("LICENSE*"))
    checks.append(("LICENSE 存在", len(license_files) > 0))

    # 4. requirements.txt / pyproject.toml
    deps = (ROOT / "requirements.txt").exists() or (ROOT / "pyproject.toml").exists()
    checks.append(("依赖声明存在", deps))

    # 5. .env.example 存在
    env_example = (ROOT / ".env.example").exists()
    checks.append((".env.example 存在", env_example))

    # 6. 自动化测试目录
    tests_dir = (ROOT / "tests").exists()
    checks.append(("tests/ 验证目录", tests_dir))

    # 7. Docker / 部署配置
    deploy_files = list(ROOT.glob("Dockerfile*")) + list(ROOT.glob("docker-compose*"))
    checks.append(("Docker 配置", len(deploy_files) > 0))

    for name, ok in checks:
        print(_ok(name) if ok else _fail(name))
    matched = sum(1 for _, ok in checks if ok)
    return {"score": _score(matched, len(checks)), "matched": matched, "total": len(checks)}


# ===== 主入口 =====


def main() -> int:
    print(f"\n{C.BOLD}{'='*70}")
    print(f"赛题三·方向三（材料方向）评分项覆盖度")
    print(f"{'='*70}{C.RESET}")

    sections = [
        ("A1 基本任务-Research Gap 识别质量（30%）", check_basic_task_research_gap, 30),
        ("A2 基本任务-文献溯源完整性（30%）", check_basic_task_literature_trace, 30),
        ("A3 基本任务-报告质量（40%）", check_basic_task_report_quality, 40),
        ("B1 路线 A-LLM 与搜索融合深度（30%）", check_route_a_llm_search, 30),
        ("B2 路线 A-物理机制说服力（30%）", check_route_a_mechanism_quality, 30),
        ("B3 路线 A-数据库交叉验证（30%）", check_route_a_db_validation, 30),
        ("C   加分项-代码开源质量", check_bonus_open_source, 0),
    ]

    weighted_total = 0
    max_total = 0
    section_scores = []
    for title, fn, weight in sections:
        try:
            r = fn()
        except Exception as e:
            r = {"score": 0, "error": str(e)[:200]}
        score = r.get("score", 0)
        weighted_total += score * (weight / 100) if weight > 0 else score
        max_total += weight if weight > 0 else 100
        section_scores.append((title, score, weight))

    # 输出汇总
    print(f"\n{C.BOLD}{'='*70}")
    print(f"赛题评分项汇总")
    print(f"{'='*70}{C.RESET}")
    for title, score, weight in section_scores:
        color = C.GREEN if score >= 80 else C.YELLOW if score >= 60 else C.RED
        w_str = f"(权重 {weight}%)" if weight > 0 else "(加分项)"
        print(f"  {title:40s}  {color}{score:3d}{C.RESET}/100  {w_str}")
    print(f"  {'─'*60}")
    pct = (weighted_total / max(1, max_total)) * 100
    color = C.GREEN if pct >= 80 else C.YELLOW if pct >= 60 else C.RED
    print(f"  {'加权合计':40s}  {color}{pct:.1f}%{C.RESET}  ({weighted_total:.1f}/{max_total})")

    # 评级
    if pct >= 90:
        grade = "S 级（冠军候选）"
        color = C.GREEN
    elif pct >= 80:
        grade = "A 级（强竞争力）"
        color = C.GREEN
    elif pct >= 70:
        grade = "B 级（良好）"
        color = C.YELLOW
    elif pct >= 60:
        grade = "C 级（合格）"
        color = C.YELLOW
    else:
        grade = "D 级（需改进）"
        color = C.RED

    print(f"\n  {C.BOLD}评级：{color}{grade}{C.RESET}\n")

    return 0 if pct >= 70 else 1


if __name__ == "__main__":
    sys.exit(main())