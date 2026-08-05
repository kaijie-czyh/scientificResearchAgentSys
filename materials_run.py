"""材料科学文献调研真实运行（赛题方向三·基本任务验证）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["SRA_DRY_RUN"] = "false"
from runtime.cli import _load_env, auto_human_callback
_load_env()

import core.config as _cfgmod
_cfgmod._default_config = None
from core.config import get_config
from core.state.lifecycle import LifecycleStage
from runtime.pipeline import Pipeline

cfg = get_config()
print(f"[materials] dry_run={cfg.dry_run}")

pipeline = Pipeline(config=cfg)
result = pipeline.run_pipeline(
    project_id="proj_materials_thermoelectric",
    topic="热电材料的构效关系与性能优化：基于文献驱动的材料发现智能体",
    human_callback=auto_human_callback,
    stop_before=LifecycleStage.IDEATION,
)

print("\n" + "=" * 70)
print("[MATERIALS RUN RESULT]")
print(f"status: {result.status}")
print(f"completed: {[s.value for s in result.completed_stages]}")
print(f"summary: {result.summary}")
print("\n[node history]")
for h in result.node_history:
    status = h.get("status", "?")
    node_id = h.get("node_id", "?")
    summary = h.get("summary", "")
    print(f"  [{status}] {node_id}: {summary}")
print("=" * 70)
