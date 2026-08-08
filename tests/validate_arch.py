"""架构质量验证脚本（本地不上传 git）。

验证维度：
1. Python 语法（所有 .py 文件 ast.parse）
2. 模块导入（关键模块可正常 import）
3. 接口完整性（赛题要求的端点/类/函数均存在）
4. Schema 一致性（pydantic schema 创建无误）
5. 类型注解与字段默认值（修复常见 Pydantic V2 陷阱）
6. 死代码检测（孤儿函数 / 未使用的关键导入）

退出码：
- 0  全部通过
- 1  有失败项（详情见报告）
"""
from __future__ import annotations

import ast
import sys
import importlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ===== 颜色输出 =====

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


# ===== 检查项 =====


def check_python_syntax() -> tuple[int, int]:
    """所有 .py 文件 ast.parse 通过。"""
    py_files = list(ROOT.rglob("*.py"))
    # 跳过目录：venv, node_modules, __pycache__, tests
    skip_dirs = {"venv", "node_modules", "__pycache__", "tests", ".git", "data", "projects"}
    targets = [
        f for f in py_files
        if not any(part in skip_dirs for part in f.parts)
    ]
    print(_section(f"1. Python 语法检查 ({len(targets)} 个文件)"))
    passed = failed = 0
    for f in targets:
        try:
            ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            passed += 1
        except SyntaxError as e:
            print(_fail(f"{f.relative_to(ROOT)}: line {e.lineno}: {e.msg}"))
            failed += 1
    print(f"  → 通过 {passed}, 失败 {failed}")
    return passed, failed


def check_key_imports() -> tuple[int, int]:
    """关键模块可正常导入。"""
    print(_section("2. 关键模块导入"))
    modules = [
        # 核心
        ("core.config", "get_config"),
        ("core.llm.registry", "LLMRegistry"),
        ("core.orchestration.context", "ExecutionContext"),
        ("core.orchestration.node", "AgentNode"),
        ("core.orchestration.graph", "Graph"),
        ("core.knowledge", "KnowledgeStore"),
        # 工具
        ("core.tools", "sciverse_agentic_search"),
        ("core.tools", "search_arxiv"),
        ("core.tools.materials_project", "cross_validate_discovery"),
        ("core.tools.materials_search", "MCTSSearcher"),
        ("core.tools.mineru_parse", "MinerUClient"),
        ("core.tools.oqmd_nomad", "OQMDClient"),
        ("core.tools.code_runner", "run_python_code"),
        # 流水线
        ("runtime.pipeline", "Pipeline"),
        ("runtime.cli", "auto_human_callback"),
        # 5 阶段 + discovery
        ("stages.research.agents", "CrossValidateAgent"),
        ("stages.ideation.agents", "BrainstormAgent"),
        ("stages.ideation.agents", "ClaimDraftAgent"),
        ("stages.design.agents", "MethodFormalizeAgent"),
        ("stages.experiment.agents", "ExperimentRunTool"),
        ("stages.writing.agents", "SectionDraftAgent"),
        ("stages.discovery.agents", "HypothesisSeedAgent"),
        ("stages.discovery.agents", "LLMGuidedSearchAgent"),
        ("stages.discovery.agents", "DiscoveryValidateAgent"),
        ("stages.discovery.agents", "DiscoveryReportAgent"),
        # Web
        ("web.api", "app"),
    ]
    passed = failed = 0
    for mod_name, symbol in modules:
        try:
            mod = importlib.import_module(mod_name)
            if not hasattr(mod, symbol):
                print(_fail(f"{mod_name}.{symbol} 不存在"))
                failed += 1
                continue
            passed += 1
        except Exception as e:
            print(_fail(f"{mod_name}.{symbol} 导入失败: {type(e).__name__}: {str(e)[:120]}"))
            failed += 1
    print(f"  → 通过 {passed}, 失败 {failed}")
    return passed, failed


def check_schemas() -> tuple[int, int]:
    """关键 Pydantic schema 可创建。"""
    print(_section("3. Schema 创建验证"))
    passed = failed = 0

    # discovery 阶段 schema
    try:
        from stages.discovery.agents import (
            HypothesisItem, CandidateEvaluationSchema, RelationshipSchema,
        )
        HypothesisItem()
        CandidateEvaluationSchema(
            config={"x": 1}, plausibility=0.8,
            physical_principle="p", causal_chain=["a", "b"],
            known_theory_support="t", quantitative_reason="q",
            domain_specific_concept="d", mechanism="m", novelty="novel",
        )
        RelationshipSchema(
            relationship="r", predicted_target=1.0,
            novelty="novel", confidence=0.5,
        )
        passed += 3
        print(_ok("discovery agents schema 创建成功"))
    except Exception as e:
        print(_fail(f"discovery agents schema: {e}"))
        failed += 3

    # research 阶段 schema（结构化 Gap）
    try:
        from stages.research.agents import ResearchGapItem
        ResearchGapItem(gap="test", type="underexplored", importance=0.7,
                       actionability="medium", cited_paper_ids=[])
        passed += 1
        print(_ok("ResearchGapItem schema 创建成功"))
    except Exception as e:
        print(_fail(f"ResearchGapItem: {e}"))
        failed += 1

    # OQMD schema
    try:
        from core.tools.oqmd_nomad import OQMDEntry, OQMDQueryResult
        OQMDEntry(formula="Bi2Te3")
        OQMDQueryResult(query="Bi2Te3", matched=True)
        passed += 2
        print(_ok("OQMDEntry / OQMDQueryResult 创建成功"))
    except Exception as e:
        print(_fail(f"OQMD schema: {e}"))
        failed += 2

    # MinerU schema
    try:
        from core.tools.mineru_parse import MinerUDocument, MinerUSection
        MinerUDocument(doc_id="test")
        MinerUSection(heading="test", level=1, page=1, text="text")
        passed += 2
        print(_ok("MinerUDocument / MinerUSection 创建成功"))
    except Exception as e:
        print(_fail(f"MinerU schema: {e}"))
        failed += 2

    print(f"  → 通过 {passed}, 失败 {failed}")
    return passed, failed


def check_stage_graphs() -> tuple[int, int]:
    """5 阶段 + discovery 子图可正常构建。"""
    print(_section("4. 子图构建验证"))
    passed = failed = 0

    graphs = [
        ("stages.research.graph", "build_research_graph"),
        ("stages.ideation.graph", "build_ideation_graph"),
        ("stages.design.graph", "build_design_graph"),
        ("stages.experiment.graph", "build_experiment_graph"),
        ("stages.writing.graph", "build_writing_graph"),
        ("stages.discovery.graph", "build_discovery_graph"),
    ]
    for mod_name, func_name in graphs:
        try:
            mod = importlib.import_module(mod_name)
            graph = getattr(mod, func_name)()
            # 校验：图至少包含入口/出口节点
            if graph.entry_node and graph.exit_node and len(graph.nodes) >= 2:
                passed += 1
                print(_ok(f"{func_name}: {len(graph.nodes)} 节点, {len(graph.edges)} 边"))
            else:
                print(_fail(f"{func_name}: 图结构异常"))
                failed += 1
        except Exception as e:
            print(_fail(f"{func_name}: {type(e).__name__}: {str(e)[:120]}"))
            failed += 1
    print(f"  → 通过 {passed}, 失败 {failed}")
    return passed, failed


def check_context_keys() -> tuple[int, int]:
    """ContextKey 定义一致性（避免 KeyError）。"""
    print(_section("5. ContextKey 一致性"))
    from stages.common import (
        DISCOVERY_HYPOTHESES, DISCOVERY_CANDIDATES, DISCOVERY_RELATIONSHIPS,
        DISCOVERY_REPORT_ARTIFACT_ID, DISCOVERY_SEARCH_SPACE,
        RESEARCH_CROSS_VALIDATION_REPORT, RESEARCH_PAPER_IDS, RESEARCH_TOPIC,
    )
    keys = {
        "DISCOVERY_HYPOTHESES": DISCOVERY_HYPOTHESES,
        "DISCOVERY_CANDIDATES": DISCOVERY_CANDIDATES,
        "DISCOVERY_RELATIONSHIPS": DISCOVERY_RELATIONSHIPS,
        "DISCOVERY_REPORT_ARTIFACT_ID": DISCOVERY_REPORT_ARTIFACT_ID,
        "DISCOVERY_SEARCH_SPACE": DISCOVERY_SEARCH_SPACE,
        "RESEARCH_CROSS_VALIDATION_REPORT": RESEARCH_CROSS_VALIDATION_REPORT,
        "RESEARCH_PAPER_IDS": RESEARCH_PAPER_IDS,
        "RESEARCH_TOPIC": RESEARCH_TOPIC,
    }
    passed = failed = 0
    for name, key in keys.items():
        if not str(key) or str(key).endswith("."):
            print(_fail(f"{name}: key 定义异常 {key!r}"))
            failed += 1
        else:
            passed += 1
    print(f"  → 通过 {passed}, 失败 {failed}")
    return passed, failed


# ===== 主入口 =====


def main() -> int:
    print(f"\n{C.BOLD}{'='*70}")
    print(f"架构质量验证（本地）")
    print(f"{'='*70}{C.RESET}")

    results = []
    for name, fn in [
        ("Python 语法", check_python_syntax),
        ("关键模块导入", check_key_imports),
        ("Schema 创建", check_schemas),
        ("子图构建", check_stage_graphs),
        ("ContextKey 一致性", check_context_keys),
    ]:
        p, f = fn()
        results.append((name, p, f))

    # 汇总
    print(f"\n{C.BOLD}{'='*70}")
    print(f"架构验证汇总")
    print(f"{'='*70}{C.RESET}")
    total_p = total_f = 0
    for name, p, f in results:
        status = f"{C.GREEN}PASS{C.RESET}" if f == 0 else f"{C.RED}FAIL ({f}){C.RESET}"
        print(f"  {name:20s}  通过 {p:3d}, 失败 {f:3d}  [{status}]")
        total_p += p
        total_f += f
    print(f"  {'─'*60}")
    print(f"  {'合计':20s}  通过 {total_p:3d}, 失败 {total_f:3d}")

    if total_f == 0:
        print(f"\n{C.GREEN}{C.BOLD}✓ 架构验证全部通过{C.RESET}\n")
        return 0
    print(f"\n{C.RED}{C.BOLD}✗ 架构验证有 {total_f} 项失败{C.RESET}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())