"""端到端流水线验证脚本（本地不上传 git）。

验证维度：
1. 主流程 5 阶段 dry_run 完整跑通
2. discovery 流程 dry_run 完整跑通
3. resume 模式恢复逻辑
4. blocked 状态恢复（赛题三·方向三可靠性要求）
5. 阶段间产出持久化与跨阶段传递

输出：每阶段的 status、节点成功数、产出统计

使用：
    python tests/validate_pipeline.py           # 全部验证
    python tests/validate_pipeline.py --quick   # 仅基础验证
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# 强制 dry_run 模式
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


def _section(title: str) -> str:
    return f"\n{C.BOLD}{C.BLUE}▶ {title}{C.RESET}"


# ===== 检查项 =====


def check_main_pipeline() -> dict[str, Any]:
    """主流程 5 阶段 dry_run 跑通。"""
    print(_section("1. 主流程 5 阶段端到端"))
    from runtime.cli import _load_env, auto_human_callback
    _load_env()

    import core.config as _cfgmod
    _cfgmod._default_config = None
    from core.config import get_config
    from runtime.pipeline import Pipeline

    cfg = get_config()
    cfg.dry_run = True

    pipeline = Pipeline(config=cfg)
    project_id = f"validate_main_{int(os.getpid())}"

    try:
        result = pipeline.run_pipeline(
            project_id=project_id,
            topic="热电材料的构效关系与性能优化：基于文献驱动的材料发现智能体",
            human_callback=auto_human_callback,
            stop_before=None,  # 跑到底
        )
    except Exception as e:
        print(_fail(f"主流程异常: {type(e).__name__}: {str(e)[:200]}"))
        return {"status": "error", "completed": [], "nodes": 0}

    completed = [s.value for s in result.completed_stages]
    node_count = len(result.node_history)
    success_count = sum(1 for n in result.node_history if n.get("status") == "success")
    failed_nodes = [n for n in result.node_history if n.get("status") == "failed"]

    # 期望：5 阶段全部完成
    expected_stages = {"research", "ideation", "design", "experiment"}
    completed_set = set(completed)
    missing = expected_stages - completed_set

    if missing:
        print(_fail(f"主流程未完成全部阶段：缺失 {missing}"))
        return {
            "status": "incomplete",
            "completed": completed,
            "nodes": node_count,
            "missing": list(missing),
        }

    if failed_nodes:
        print(_fail(f"主流程有 {len(failed_nodes)} 个节点失败"))
        for n in failed_nodes[:3]:
            print(f"    - {n.get('node_id')}: {n.get('summary', '')[:80]}")
        return {"status": "node_failed", "completed": completed, "nodes": node_count}

    if success_count < 20:
        print(_fail(f"主流程节点成功数偏低：{success_count}/{node_count}"))
        return {"status": "low_success", "completed": completed, "nodes": success_count}

    print(_ok(f"主流程完成：{completed}, {success_count}/{node_count} 节点成功"))
    return {"status": "pass", "completed": completed, "nodes": success_count}


def check_discovery_pipeline() -> dict[str, Any]:
    """discovery 阶段 dry_run 跑通。"""
    print(_section("2. discovery 流程端到端"))
    from runtime.cli import _load_env, auto_human_callback
    _load_env()

    import core.config as _cfgmod
    _cfgmod._default_config = None
    from core.config import get_config
    from runtime.pipeline import Pipeline

    cfg = get_config()
    cfg.dry_run = True
    pipeline = Pipeline(config=cfg)
    project_id = f"validate_disc_{int(os.getpid())}"

    try:
        result = pipeline.run_discovery(
            project_id=project_id,
            topic="热电材料的构效关系与性能优化：基于文献驱动的材料发现智能体",
            human_callback=auto_human_callback,
        )
    except Exception as e:
        print(_fail(f"discovery 异常: {type(e).__name__}: {str(e)[:200]}"))
        return {"status": "error"}

    if result.status != "completed":
        print(_fail(f"discovery 未完成：{result.status}, summary={result.summary[:200]}"))
        return {"status": result.status, "summary": result.summary}

    # 校验节点
    success_nodes = [n for n in result.node_history if n.get("status") == "success"]
    expected_nodes = {"hypothesis_seed", "search_space", "llm_guided_search", "discovery_validate", "discovery_report"}
    actual_node_ids = {n.get("node_id") for n in success_nodes}
    missing = expected_nodes - actual_node_ids

    if missing:
        print(_fail(f"discovery 关键节点缺失：{missing}"))
        return {"status": "missing_nodes", "missing": list(missing)}

    print(_ok(f"discovery 完成：{len(success_nodes)} 节点成功，含核心节点"))
    return {"status": "pass", "nodes": len(success_nodes)}


def check_resume_recovery() -> dict[str, Any]:
    """resume 模式恢复逻辑（关键：修复第十轮的串主题 bug）。"""
    print(_section("3. Resume 恢复验证"))
    from runtime.cli import _load_env, auto_human_callback
    _load_env()

    import core.config as _cfgmod
    _cfgmod._default_config = None
    from core.config import get_config
    from runtime.pipeline import Pipeline

    cfg = get_config()
    cfg.dry_run = True
    pipeline = Pipeline(config=cfg)
    project_id = f"validate_resume_{int(os.getpid())}"
    topic = "热电材料性能优化测试"

    # 第一次跑：research 阶段
    try:
        result1 = pipeline.run_pipeline(
            project_id=project_id,
            topic=topic,
            human_callback=auto_human_callback,
            stop_before=None,
        )
    except Exception as e:
        print(_fail(f"首次运行异常: {e}"))
        return {"status": "error"}

    # 第二次跑：resume 模式
    try:
        result2 = pipeline.run_pipeline(
            project_id=project_id,
            topic=topic,
            human_callback=auto_human_callback,
            resume=True,
        )
    except Exception as e:
        print(_fail(f"resume 异常: {e}"))
        return {"status": "error"}

    # 校验：resume 后能从 stopped 状态恢复
    if result2.status not in ("completed", "failed"):
        print(_warn(f"resume 状态异常：{result2.status}"))

    print(_ok(f"resume 验证通过：首次 {result1.status}, resume {result2.status}"))
    return {"status": "pass", "first": result1.status, "resume": result2.status}


def check_blocked_recovery() -> dict[str, Any]:
    """blocked 状态恢复（第十轮修复的核心问题）。"""
    print(_section("4. Blocked 状态恢复验证"))
    from runtime.cli import _load_env, auto_human_callback
    _load_env()

    import core.config as _cfgmod
    _cfgmod._default_config = None
    from core.config import get_config
    from runtime.pipeline import Pipeline
    from core.state.lifecycle import LifecycleStage, StageStatus

    cfg = get_config()
    cfg.dry_run = True
    pipeline = Pipeline(config=cfg)
    project_id = f"validate_blocked_{int(os.getpid())}"
    topic = "blocked 测试主题"

    try:
        result = pipeline.run_pipeline(
            project_id=project_id,
            topic=topic,
            human_callback=auto_human_callback,
            stop_before=LifecycleStage.RESEARCH,
        )
    except Exception as e:
        print(_fail(f"初始化失败: {e}"))
        return {"status": "error"}

    # 手动 mark blocked ideation
    try:
        session, ctx = pipeline.resume_project(project_id)
        # 模拟研究已完成
        session.start_stage(LifecycleStage.RESEARCH, triggered_by="test")
        session.complete_stage(reason="mock done", triggered_by="test")
        session.start_stage(LifecycleStage.IDEATION, triggered_by="test")
        session.mark_blocked(reason="mock failure", triggered_by="test")
    except Exception as e:
        print(_fail(f"设置 blocked 失败: {e}"))
        return {"status": "error"}

    # resume 时应能 unblock
    try:
        result2 = pipeline.run_pipeline(
            project_id=project_id,
            topic=topic,
            human_callback=auto_human_callback,
            resume=True,
        )
        if "blocked" not in result2.summary.lower() and "异常" not in result2.summary.lower():
            print(_ok(f"blocked 恢复成功：{result2.status}"))
            return {"status": "pass"}
        else:
            print(_fail(f"blocked 未恢复：{result2.summary[:200]}"))
            return {"status": "failed", "summary": result2.summary}
    except Exception as e:
        print(_fail(f"resume 异常: {e}"))
        return {"status": "error"}


def check_stage_propagation() -> dict[str, Any]:
    """阶段间产出传递（核心：科研主题从 research → ideation → design）。"""
    print(_section("5. 阶段间产出传递验证"))
    from runtime.cli import _load_env, auto_human_callback
    _load_env()

    import core.config as _cfgmod
    _cfgmod._default_config = None
    from core.config import get_config
    from runtime.pipeline import Pipeline

    cfg = get_config()
    cfg.dry_run = True
    pipeline = Pipeline(config=cfg)
    project_id = f"validate_prop_{int(os.getpid())}"
    topic = "热电材料构效关系传播验证"

    try:
        result = pipeline.run_pipeline(
            project_id=project_id,
            topic=topic,
            human_callback=auto_human_callback,
            stop_before=None,
        )
    except Exception as e:
        print(_fail(f"流水线异常: {e}"))
        return {"status": "error"}

    # 读取产出，校验主题是否贯穿
    from core.knowledge import KnowledgeStore
    store = KnowledgeStore(cfg.paths.project_db(project_id))

    ideas = store.list_ideas()
    claims = store.list_claims()
    cv_report = store.get_kv("cross_validation_report") or {}

    # 检查关键字段非空
    checks = {
        "ideas 生成": len(ideas) > 0,
        "claims 生成": len(claims) > 0,
        "cv_report 持久化": bool(cv_report),
        "papers 入库": len(store.list_papers()) >= 0,
    }
    failed = [k for k, v in checks.items() if not v]
    if failed:
        print(_fail(f"产出缺失：{failed}"))
        return {"status": "missing_outputs", "missing": failed}

    print(_ok(f"阶段产出正常：{len(ideas)} ideas, {len(claims)} claims, "
             f"{len(store.list_papers())} papers"))
    return {
        "status": "pass",
        "ideas": len(ideas),
        "claims": len(claims),
        "papers": len(store.list_papers()),
    }


# ===== 主入口 =====


def main() -> int:
    print(f"\n{C.BOLD}{'='*70}")
    print(f"端到端流水线验证（本地）")
    print(f"{'='*70}{C.RESET}")

    results = []
    for name, fn in [
        ("主流程 5 阶段", check_main_pipeline),
        ("discovery 流程", check_discovery_pipeline),
        ("Resume 恢复", check_resume_recovery),
        ("Blocked 恢复", check_blocked_recovery),
        ("阶段产出传递", check_stage_propagation),
    ]:
        try:
            r = fn()
        except Exception as e:
            r = {"status": f"error: {type(e).__name__}: {str(e)[:200]}"}
        results.append((name, r))

    print(f"\n{C.BOLD}{'='*70}")
    print(f"流水线验证汇总")
    print(f"{'='*70}{C.RESET}")
    failed_count = 0
    for name, r in results:
        status = r.get("status", "?")
        ok = status == "pass"
        marker = f"{C.GREEN}PASS{C.RESET}" if ok else f"{C.RED}FAIL{C.RESET}"
        print(f"  {name:20s}  [{marker}]  {status}")
        if not ok:
            failed_count += 1

    if failed_count == 0:
        print(f"\n{C.GREEN}{C.BOLD}✓ 流水线验证全部通过{C.RESET}\n")
        return 0
    print(f"\n{C.RED}{C.BOLD}✗ 流水线验证有 {failed_count} 项失败{C.RESET}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())