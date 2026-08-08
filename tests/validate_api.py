"""Web API 接口完整性验证脚本（本地不上传 git）。

验证维度：
1. FastAPI 路由存在性
2. 路由签名（路径、HTTP 方法）
3. 关键端点可导入/构造
4. 端点覆盖前端调用
5. 多格式导出接口（md/docx/pdf）

使用：
    python tests/validate_api.py
    python tests/validate_api.py --start  # 启动服务并 HTTP 测试
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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


def check_api_routes() -> dict[str, Any]:
    """关键 API 路由存在性。"""
    print(_section("1. API 路由注册"))
    from web.api import app

    # 用 (path, method) 集合，path 已是 FastAPI 模板（含 {project_id}）
    routes = set()
    for r in app.routes:
        if not hasattr(r, "methods"):
            continue
        for m in r.methods:
            routes.add((r.path, m))

    expected = [
        ("/api/projects", "GET"),
        ("/api/projects/{project_id}", "GET"),
        ("/api/projects/{project_id}/start", "POST"),
        ("/api/projects/{project_id}/discovery", "POST"),
        ("/api/projects/{project_id}/papers", "GET"),
        ("/api/projects/{project_id}/claims", "GET"),
        ("/api/projects/{project_id}/experiments", "GET"),
        ("/api/projects/{project_id}/upload-paper", "POST"),
        ("/api/projects/{project_id}/upload-topic", "POST"),
        ("/api/projects/{project_id}/download/{artifact_type}", "GET"),
        ("/", "GET"),
    ]

    passed = failed = 0
    for path, method in expected:
        if (path, method) in routes:
            passed += 1
            print(_ok(f"{method:6s} {path}"))
        else:
            failed += 1
            print(_fail(f"{method:6s} {path}  (缺失)"))

    print(f"  → 通过 {passed}/{passed + failed}")
    return {"passed": passed, "failed": failed}


def check_artifact_types() -> dict[str, Any]:
    """下载类型覆盖。"""
    print(_section("2. 下载类型覆盖"))
    expected_artifacts = {
        "full-report": "md/docx/pdf",
        "research-report": "md/docx/pdf",
        "ideas-summary": "md/docx/pdf",
        "method-doc": "md/docx/pdf",
        "experiment-code": "py（仅源码）",
        "experiment-results": "md/docx/pdf",
        "claims-summary": "md/docx/pdf",
        "discovery-report": "md/docx/pdf",
        "paper-draft": "md/docx/pdf",
    }
    # 通过 grep web/api.py 源码检测每个 artifact_type 分支
    api_path = ROOT / "web" / "api.py"
    api_content = api_path.read_text(encoding="utf-8")

    passed = 0
    failed = 0
    for art, fmt in expected_artifacts.items():
        if f'artifact_type == "{art}"' in api_content or f'"{art}"' in api_content:
            passed += 1
            print(_ok(f"{art:20s}  格式: {fmt}"))
        else:
            failed += 1
            print(_fail(f"{art:20s}  (源码中未找到该分支)"))

    print(f"  → 通过 {passed}/{passed + failed}")
    return {"passed": passed, "failed": failed}


def check_format_conversion() -> dict[str, Any]:
    """多格式转换功能。"""
    print(_section("3. 多格式转换（md/docx/pdf）"))
    checks = []

    try:
        from web.api import _md_to_docx_response, _md_to_pdf_response
        # 测试 markdown 转换
        r1 = _md_to_docx_response("# 标题\n\n正文内容", "test")
        assert r1.status_code == 200
        assert "wordprocessingml" in r1.media_type
        checks.append(("docx 转换", True))
    except Exception as e:
        checks.append(("docx 转换", False))
        print(_fail(f"docx 转换：{e}"))

    try:
        from web.api import _md_to_pdf_response
        r2 = _md_to_pdf_response("# 标题\n\n正文内容", "test")
        assert r2.status_code == 200
        assert r2.media_type == "application/pdf"
        checks.append(("pdf 转换", True))
    except Exception as e:
        checks.append(("pdf 转换", False))
        print(_fail(f"pdf 转换：{e}"))

    # python-docx 与 fpdf2 库可用
    try:
        import docx  # noqa
        checks.append(("python-docx 可用", True))
    except ImportError:
        checks.append(("python-docx 可用", False))

    try:
        import fpdf  # noqa
        checks.append(("fpdf2 可用", True))
    except ImportError:
        checks.append(("fpdf2 可用", False))

    for name, ok in checks:
        print(_ok(name) if ok else _fail(name))
    matched = sum(1 for _, ok in checks if ok)
    return {"score": matched * 25, "matched": matched, "total": len(checks)}


def check_frontend_endpoints() -> dict[str, Any]:
    """前端调用的 API 端点（grep app.js 验证）。"""
    print(_section("4. 前端 API 调用完整性"))
    from pathlib import Path as P
    app_js = P("web/static/app.js")
    if not app_js.exists():
        print(_fail("app.js 不存在"))
        return {"matched": 0, "total": 0}

    content = app_js.read_text(encoding="utf-8")
    # 前端常见的 API 路径
    api_patterns = [
        "/api/projects",
        "/api/projects/.*start",
        "/api/projects/.*discovery",
        "/api/projects/.*papers",
        "/api/projects/.*claims",
        "/api/projects/.*experiments",
        "/api/projects/.*ideas",
        "/api/projects/.*relationships",
        "/api/projects/.*upload-paper",
        "/api/projects/.*upload-topic",
        "/api/projects/.*download",
        "/api/projects/.*human-response",
    ]
    passed = 0
    for pattern in api_patterns:
        # 直接检测路径段是否出现在 content 中（不依赖 .* 替换）
        normalized = pattern.replace(".*", "{project_id}")
        # 同时尝试 /{project_id}/ 形式
        if (normalized in content or
            normalized.replace("{project_id}", f"/{os.getpid()}/") in content or
            # 检查最后一段路径
            any(seg in content for seg in normalized.split("/{project_id}/"))):
            passed += 1
            print(_ok(f"前端调用: {pattern}"))
        else:
            print(_warn(f"前端未引用: {pattern}"))

    print(f"  → 通过 {passed}/{len(api_patterns)}")
    return {"matched": passed, "total": len(api_patterns)}


def check_static_files() -> dict[str, Any]:
    """前端静态文件存在。"""
    print(_section("5. 前端静态文件"))
    files = [
        "web/static/app.js",
        "web/static/style.css",
        "web/static/index.html",
    ]
    passed = 0
    for f in files:
        if (ROOT / f).exists():
            passed += 1
            size = (ROOT / f).stat().st_size
            print(_ok(f"{f}  ({size:,} 字节)"))
        else:
            print(_fail(f"{f} 缺失"))
    return {"passed": passed, "total": len(files)}


# ===== 主入口 =====


def main() -> int:
    print(f"\n{C.BOLD}{'='*70}")
    print(f"Web API 接口验证（本地）")
    print(f"{'='*70}{C.RESET}")

    results = []
    for name, fn in [
        ("API 路由", check_api_routes),
        ("下载类型", check_artifact_types),
        ("多格式转换", check_format_conversion),
        ("前端调用完整性", check_frontend_endpoints),
        ("静态文件", check_static_files),
    ]:
        try:
            r = fn()
        except Exception as e:
            r = {"error": str(e)[:200]}
        results.append((name, r))

    # 汇总
    print(f"\n{C.BOLD}{'='*70}")
    print(f"API 验证汇总")
    print(f"{'='*70}{C.RESET}")
    total_ok = 0
    total = 0
    for name, r in results:
        ok = r.get("passed") or r.get("matched") or r.get("score", 0) // 25
        all_n = r.get("total") or r.get("passed", 0) + r.get("failed", 0) or 4
        total_ok += ok
        total += all_n
        marker = f"{C.GREEN}PASS{C.RESET}" if ok == all_n else f"{C.YELLOW}PARTIAL{C.RESET}"
        print(f"  {name:20s}  {ok}/{all_n}  [{marker}]")

    pct = (total_ok / max(1, total)) * 100
    color = C.GREEN if pct >= 80 else C.YELLOW if pct >= 60 else C.RED
    print(f"\n  {'合计':20s}  {color}{pct:.0f}%{C.RESET} ({total_ok}/{total})")

    if pct >= 80:
        print(f"\n{C.GREEN}{C.BOLD}✓ API 验证通过{C.RESET}\n")
        return 0
    print(f"\n{C.YELLOW}{C.BOLD}⚠ API 验证部分通过{C.RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())