"""端到端 smoke test。

默认 dry_run 跑通全 5 阶段（force_writing）。
设置环境变量 SRA_SMOKE_REAL=1 启用真实 MiniMax 调用，默认 stop_before=writing
  只验证 research→experiment 真实链路（避免一次烧太多 token）。
设置 SRA_SMOKE_REAL=1 SRA_SMOKE_INCLUDE_WRITING=1 跑含写作的完整真实流程。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保项目根在 sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 加载 .env
from runtime.cli import _load_env, auto_human_callback
_load_env()

from core.config import get_config
from core.state.lifecycle import LifecycleStage
from runtime.pipeline import Pipeline

REAL = os.environ.get("SRA_SMOKE_REAL", "0") == "1"
INCLUDE_WRITING = os.environ.get("SRA_SMOKE_INCLUDE_WRITING", "0") == "1"

if REAL:
    os.environ["SRA_DRY_RUN"] = "false"
    # 重新加载 config（强制重读环境变量）
    import core.config as _cfgmod
    _cfgmod._default_config = None

cfg = get_config()
print(f"[smoke] REAL={REAL} INCLUDE_WRITING={INCLUDE_WRITING} dry_run={cfg.dry_run}")

# 自动确认人工节点：自动化迭代模式下不打断流程
# （用户需要人工干预时直接用 `python -m runtime.cli run --no-dry-run`）
human_cb = auto_human_callback

stop_before: LifecycleStage | None = None
if REAL and not INCLUDE_WRITING:
    stop_before = LifecycleStage.WRITING

project_id = "proj_smoke_real" if REAL else "proj_smoke_dryrun"
topic = "联邦学习中的公平激励机制设计"

pipeline = Pipeline(config=cfg)
result = pipeline.run_pipeline(
    project_id=project_id,
    topic=topic,
    human_callback=human_cb,
    force_writing=(not REAL) or INCLUDE_WRITING,  # 真实模式让实验成败自然决策
    stop_before=stop_before,
)

print("\n" + "=" * 70)
print("[SMOKE RESULT]")
print(f"status: {result.status}")
print(f"completed_stages: {[s.value for s in result.completed_stages]}")
if result.current_stage:
    print(f"current_stage: {result.current_stage.value}")
print(f"summary: {result.summary}")
if result.experiment_outcome:
    o = result.experiment_outcome
    print(f"experiment_outcome.success: {o.get('success')}")
    print(f"  verified: {len(o.get('verified_claim_ids', []))}")
    print(f"  refuted:  {len(o.get('refuted_claim_ids', []))}")
    print(f"  recommend: {o.get('recommendation')}")
if result.recommendation:
    print(f"recommendation: {result.recommendation}")
print("\n[node history]")
for h in result.node_history:
    print(f"  [{h.get('status', '?')}] {h.get('node_id', '?')}: {h.get('summary', '')}")
print("=" * 70)
print(f"\n[smoke] exit_code = {0 if result.status in ('completed', 'stopped', 'experiment_failed') else 1}")
