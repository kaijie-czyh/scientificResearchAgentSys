"""沙盒代码运行工具。

供 ExperimentRunTool 执行 LLM 生成的实验代码。
设计要点：
- 代码写入项目内 experiments/ 目录（便于追溯）
- 用 subprocess 在子进程运行，避免主进程崩溃
- 捕获 stdout/stderr/returncode
- 设置 timeout，防止死循环
- 不做危险操作过滤（用户对自己生成的代码负责；生产环境应加沙箱）
"""
from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    """代码运行结果。"""

    success: bool
    returncode: int
    stdout: str
    stderr: str
    runtime_seconds: float
    code_path: str


def run_python_code(
    code: str,
    project_dir: Path,
    code_path: str = "experiments/run_exp.py",
    timeout: int = 600,
    extra_args: Optional[list[str]] = None,
) -> RunResult:
    """运行 Python 代码。

    Args:
        code: 完整的 Python 代码字符串
        project_dir: 项目根目录（代码写入此目录下）
        code_path: 代码文件相对路径（相对 project_dir）
        timeout: 运行超时秒（默认 10 分钟）
        extra_args: 传给脚本的额外命令行参数

    Returns:
        RunResult
    """
    project_dir = Path(project_dir).resolve()
    full_path = project_dir / code_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(code, encoding="utf-8")

    cmd = [sys.executable, str(full_path)]
    if extra_args:
        cmd.extend(extra_args)

    logger.info("运行实验代码: %s", " ".join(cmd))

    import time
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        runtime = time.time() - start
        return RunResult(
            success=(proc.returncode == 0),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            runtime_seconds=runtime,
            code_path=str(full_path),
        )
    except subprocess.TimeoutExpired as e:
        runtime = time.time() - start
        return RunResult(
            success=False,
            returncode=-1,
            stdout=e.stdout or "" if isinstance(e.stdout, str) else "",
            stderr=f"运行超时（{timeout}秒）",
            runtime_seconds=runtime,
            code_path=str(full_path),
        )
    except Exception as e:
        runtime = time.time() - start
        return RunResult(
            success=False,
            returncode=-2,
            stdout="",
            stderr=f"运行异常: {type(e).__name__}: {e}",
            runtime_seconds=runtime,
            code_path=str(full_path),
        )


def check_syntax(code: str) -> tuple[bool, str]:
    """检查代码语法。返回 (是否通过, 错误信息)。"""
    try:
        compile(code, "<experiment>", "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"语法错误 (L{e.lineno}): {e.msg}"
