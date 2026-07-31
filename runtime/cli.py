"""命令行入口。

使用方法：
    # dry_run 模式（默认，不调用 API，用占位数据验证全流程架构）
    python -m runtime.cli run --topic "联邦学习中的公平激励机制"

    # 真实调用模式（需先配置 .env 中的 MINIMAX_API_KEY）
    # 设 SRA_DRY_RUN=false 或 --no-dry-run
    python -m runtime.cli run --topic "..." --no-dry-run

    # 跳过论文写作（只跑到实验阶段）
    python -m runtime.cli run --topic "..." --stop-before writing

    # 恢复中断的项目
    python -m runtime.cli resume --project-id proj_001

    # 查看项目状态
    python -m runtime.cli status --project-id proj_001
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# 确保项目根在 sys.path（python -m runtime.cli 已保证，但直接运行脚本时需补充）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import get_config
from core.orchestration.node import HumanRequest, HumanResponse
from core.state.lifecycle import LifecycleStage
from runtime.pipeline import Pipeline, PipelineResult


def _load_env() -> None:
    """从 .env 文件加载环境变量（若存在）。

    简易实现：不依赖 python-dotenv，手动解析 KEY=VALUE。
    """
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 不覆盖已有环境变量
        if key not in os.environ:
            os.environ[key] = value


def cli_human_callback(req: HumanRequest) -> HumanResponse:
    """CLI 人工回调：用 input() 呈现请求并获取用户输入。"""
    print("\n" + "=" * 60)
    print("【需要人工输入】")
    print(req.prompt)
    if req.options:
        print(f"选项：{', '.join(req.options)}")
    print("=" * 60)

    try:
        text = input("请输入（或 'abort' 中止、'rollback' 回滚）: ").strip()
    except (EOFError, KeyboardInterrupt):
        return HumanResponse(action="abort")

    if text.lower() == "abort":
        return HumanResponse(action="abort")
    if text.lower() == "rollback":
        return HumanResponse(action="rollback")

    return HumanResponse(text=text, action="continue")


def auto_human_callback(req: HumanRequest) -> HumanResponse:
    """dry_run 模式自动回调：打印请求并自动确认 'ok'，让全流程跑通。"""
    print("\n" + "-" * 60)
    print(f"[dry_run 自动确认] 人工节点请求:")
    # 只打印前 200 字避免刷屏
    prompt_preview = req.prompt[:200] + ("..." if len(req.prompt) > 200 else "")
    print(prompt_preview)
    print("→ 自动确认: ok")
    print("-" * 60)
    return HumanResponse(text="ok", action="continue")


def cmd_run(args: argparse.Namespace) -> int:
    """运行全流程。"""
    _load_env()
    config = get_config()

    # 命令行覆盖 dry_run
    if args.no_dry_run:
        config.dry_run = False
        os.environ["SRA_DRY_RUN"] = "false"

    # 生成 project_id
    project_id = args.project_id or f"proj_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # stop_before 解析
    stop_before: Optional[LifecycleStage] = None
    if args.stop_before:
        try:
            stop_before = LifecycleStage(args.stop_before)
        except ValueError:
            print(f"错误：未知阶段 '{args.stop_before}'，可选：{[s.value for s in LifecycleStage]}")
            return 1

    # dry_run 提示
    if config.dry_run:
        print("[dry_run 模式] 不执行真实 LLM 调用，全部用占位数据验证架构。")
        print("  配置好 .env 后用 --no-dry-run 启用真实调用。")
    else:
        print("[真实调用模式] 将调用 MiniMax API，请确认 .env 已配置 MINIMAX_API_KEY")
        if not os.environ.get("MINIMAX_API_KEY"):
            print("错误：SRA_DRY_RUN=false 但未配置 MINIMAX_API_KEY")
            return 1

    print(f"\n项目 ID: {project_id}")
    print(f"研究主题: {args.topic}")
    if stop_before:
        print(f"将在 {stop_before.value} 阶段前停止")
    print()

    # 运行 Pipeline
    # dry_run 用自动确认回调（让全流程跑通）；真实模式用交互式回调
    human_cb = auto_human_callback if config.dry_run else cli_human_callback

    pipeline = Pipeline(config=config)
    result: PipelineResult = pipeline.run_pipeline(
        project_id=project_id,
        topic=args.topic,
        human_callback=human_cb,
        stop_before=stop_before,
        force_writing=args.force_writing,
    )

    # 输出结果
    print("\n" + "=" * 60)
    print("【执行结果】")
    print(f"状态: {result.status}")
    print(f"已完成阶段: {[s.value for s in result.completed_stages]}")
    if result.current_stage:
        print(f"当前阶段: {result.current_stage.value}")
    print(f"摘要: {result.summary}")
    if result.experiment_outcome:
        outcome = result.experiment_outcome
        print(f"实验评估: success={outcome.get('success')}")
        print(f"  已验证 Claim: {len(outcome.get('verified_claim_ids', []))}")
        print(f"  被反驳 Claim: {len(outcome.get('refuted_claim_ids', []))}")
        print(f"  建议: {outcome.get('recommendation')}")
    if result.recommendation:
        print(f"建议下一步: {result.recommendation}")
    print("=" * 60)

    # 节点历史
    if args.verbose and result.node_history:
        print("\n【节点执行历史】")
        for h in result.node_history:
            print(f"  [{h.get('status', '?')}] {h.get('node_id', '?')}: {h.get('summary', '')}")

    return 0 if result.status in ("completed", "stopped", "experiment_failed") else 1


def cmd_resume(args: argparse.Namespace) -> int:
    """恢复中断的项目。"""
    _load_env()
    config = get_config()

    pipeline = Pipeline(config=config)
    result = pipeline.run_pipeline(
        project_id=args.project_id,
        topic="",  # resume 模式 topic 已在 context
        human_callback=cli_human_callback if not config.dry_run else None,
        resume=True,
    )

    print(f"恢复结果: {result.status} - {result.summary}")
    return 0 if result.status in ("completed", "stopped") else 1


def cmd_status(args: argparse.Namespace) -> int:
    """查看项目状态。"""
    _load_env()
    config = get_config()

    from core.state.session import ProjectSession

    session = ProjectSession.load(args.project_id, config.paths)
    print(f"项目 ID: {args.project_id}")
    print(f"当前阶段: {session.current_stage().value}")
    print("\n各阶段状态:")
    for stage in LifecycleStage.ordered():
        status = session.status_of(stage)
        marker = "✓" if status.value == "done" else ("→" if stage == session.current_stage() else "○")
        print(f"  {marker} {stage.value}: {status.value}")

    # 快照历史
    history = session.snapshot_history()
    print(f"\n快照数: {len(history)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="科研论文 Agent 系统 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = sub.add_parser("run", help="运行全流程")
    p_run.add_argument("--topic", required=True, help="研究主题")
    p_run.add_argument("--project-id", default=None, help="项目 ID（默认自动生成）")
    p_run.add_argument("--no-dry-run", action="store_true", help="禁用 dry_run，启用真实 API 调用")
    p_run.add_argument("--force-writing", action="store_true", help="强制进入 writing 阶段（绕过实验成败判断，dry_run 下验证写作架构用）")
    p_run.add_argument("--stop-before", default=None, help="在某阶段前停止（research/ideation/design/experiment/writing）")
    p_run.add_argument("--verbose", action="store_true", help="输出详细节点历史")
    p_run.set_defaults(func=cmd_run)

    # resume
    p_resume = sub.add_parser("resume", help="恢复中断的项目")
    p_resume.add_argument("--project-id", required=True, help="项目 ID")
    p_resume.set_defaults(func=cmd_resume)

    # status
    p_status = sub.add_parser("status", help="查看项目状态")
    p_status.add_argument("--project-id", required=True, help="项目 ID")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
