"""自动化验证脚本总入口（本地不上传 git）。

运行所有验证并生成综合质量报告：
1. 架构质量（validate_arch）
2. 端到端流水线（validate_pipeline）
3. 产出质量（validate_outputs）
4. 赛题三·方向三评分项覆盖（validate_competition）
5. Web API 接口完整性（validate_api）

输出：
- 终端彩色报告
- JSON 综合报告（tests/last_full_report.json）

使用：
    python tests/run_all.py            # 全部验证
    python tests/run_all.py --quick    # 仅架构 + API
    python tests/run_all.py --no-color # 无 ANSI 颜色
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ANSI 颜色（无 --no-color 时启用）
USE_COLOR = True


class C:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _set_color(enabled: bool) -> None:
    if not enabled:
        for attr in ("RED", "GREEN", "YELLOW", "BLUE", "BOLD", "DIM", "RESET"):
            setattr(C, attr, "")


def _ok(msg: str) -> str:
    return f"  {C.GREEN}✓{C.RESET} {msg}"


def _fail(msg: str) -> str:
    return f"  {C.RED}✗{C.RESET} {msg}"


def _warn(msg: str) -> str:
    return f"  {C.YELLOW}⚠{C.RESET} {msg}"


def _section(title: str) -> str:
    return f"\n{C.BOLD}{C.BLUE}{'─'*70}\n▶ {title}\n{'─'*70}{C.RESET}"


# ===== 验证脚本调度 =====


VALIDATORS = [
    ("架构质量", "validate_arch"),
    ("流水线", "validate_pipeline"),
    ("产出质量", "validate_outputs"),
    ("赛题对齐", "validate_competition"),
    ("API 接口", "validate_api"),
]


def _run_validator(name: str, module_name: str) -> dict:
    """运行单个验证脚本，返回其主进程结果摘要。"""
    import importlib
    try:
        mod = importlib.import_module(f"tests.{module_name}")
    except Exception as e:
        return {"name": name, "status": "import_error", "error": str(e)[:200]}

    if not hasattr(mod, "main"):
        return {"name": name, "status": "no_main"}

    start = time.time()
    try:
        exit_code = mod.main()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        return {
            "name": name,
            "status": "exception",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
            "elapsed": time.time() - start,
        }
    elapsed = time.time() - start

    return {
        "name": name,
        "status": "pass" if exit_code == 0 else "fail",
        "exit_code": exit_code,
        "elapsed": elapsed,
    }


# ===== 报告生成 =====


def _print_summary(results: list[dict], elapsed_total: float) -> None:
    """打印综合报告到终端。"""
    print(f"\n{C.BOLD}{'='*70}")
    print(f"  自动化验证综合报告")
    print(f"  生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  总耗时：{elapsed_total:.1f}s")
    print(f"{'='*70}{C.RESET}\n")

    print(f"  {'验证项':15s}  {'结果':6s}  {'耗时':8s}  {'详情'}")
    print(f"  {'─'*60}")

    pass_count = 0
    for r in results:
        name = r["name"]
        status = r["status"]
        elapsed = r.get("elapsed", 0)
        if status == "pass":
            mark = f"{C.GREEN}PASS{C.RESET}"
            pass_count += 1
        elif status == "fail":
            mark = f"{C.RED}FAIL{C.RESET}"
        elif status == "import_error":
            mark = f"{C.RED}ERR {C.RESET}"
        else:
            mark = f"{C.YELLOW}{status.upper()}{C.RESET}"

        detail = r.get("error", "")
        if r.get("exit_code") is not None and r["exit_code"] != 0:
            detail = f"exit_code={r['exit_code']}"

        print(f"  {name:15s}  [{mark}]  {elapsed:6.1f}s  {C.DIM}{detail}{C.RESET}")

    print(f"\n  {'─'*60}")
    pct = (pass_count / max(1, len(results))) * 100
    color = C.GREEN if pct >= 80 else C.YELLOW if pct >= 60 else C.RED
    print(f"  通过：{C.GREEN}{pass_count}{C.RESET} / {len(results)}  ({color}{pct:.0f}%{C.RESET})")

    # 评级
    if pct >= 90:
        grade = "S 级 — 冠军候选"
        color = C.GREEN
    elif pct >= 80:
        grade = "A 级 — 强竞争力"
        color = C.GREEN
    elif pct >= 70:
        grade = "B 级 — 良好"
        color = C.YELLOW
    elif pct >= 60:
        grade = "C 级 — 合格"
        color = C.YELLOW
    else:
        grade = "D 级 — 需改进"
        color = C.RED

    print(f"\n  {C.BOLD}总体评级：{color}{grade}{C.RESET}\n")


def _save_json_report(results: list[dict], elapsed_total: float) -> Path:
    """保存 JSON 综合报告。"""
    report = {
        "generated_at": datetime.now().isoformat(),
        "total_elapsed": elapsed_total,
        "validators": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["status"] == "pass"),
            "failed": sum(1 for r in results if r["status"] == "fail"),
            "errors": sum(1 for r in results if r["status"] not in ("pass", "fail")),
        },
    }
    report_path = ROOT / "tests" / "last_full_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path


# ===== 主入口 =====


def main() -> int:
    parser = argparse.ArgumentParser(description="自动化验证总入口")
    parser.add_argument("--quick", action="store_true", help="仅架构 + API 快速验证")
    parser.add_argument("--no-color", action="store_true", help="无 ANSI 颜色")
    parser.add_argument(
        "--only",
        type=str,
        choices=[v[1] for v in VALIDATORS],
        help="仅运行指定验证",
    )
    args = parser.parse_args()

    _set_color(not args.no_color)

    print(f"\n{C.BOLD}{'='*70}")
    print(f"  自动化验证总入口（GOAI 赛道三·方向三备战）")
    print(f"{'='*70}{C.RESET}\n")

    validators = VALIDATORS
    if args.quick:
        validators = [v for v in VALIDATORS if v[1] in ("validate_arch", "validate_api")]
    if args.only:
        validators = [v for v in VALIDATORS if v[1] == args.only]

    results = []
    overall_start = time.time()
    for name, module_name in validators:
        print(_section(f"{name} ({module_name})"))
        r = _run_validator(name, module_name)
        results.append(r)
        if r["status"] == "pass":
            print(_ok(f"{name} 通过"))
        else:
            print(_fail(f"{name} {r['status']}: {r.get('error', '')[:200]}"))
    elapsed_total = time.time() - overall_start

    _print_summary(results, elapsed_total)

    report_path = _save_json_report(results, elapsed_total)
    print(f"  报告已保存：{report_path}\n")

    failed = sum(1 for r in results if r["status"] != "pass")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())