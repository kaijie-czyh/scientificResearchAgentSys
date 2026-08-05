"""材料构效关系发现运行脚本（路线 A：构效关系发现）。

流程：research 阶段（文献调研）→ discovery 子图（构效关系发现）
- research：从材料科学文献中识别 Research Gap + 共识/冲突 + 入库论文
- discovery：LLM 引导搜索（MCTS + 代理模型）发现构效关系，附证据链与物理机制

使用方法：
  # dry_run（不调 API，验证架构）
  python discovery_run.py

  # 真实运行（需配置 .env 中的 MINIMAX_API_KEY）
  python discovery_run.py --real

  # 从已有项目恢复（复用 research 阶段产出）
  python discovery_run.py --resume --project proj_materials_thermoelectric
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 默认 dry_run，--real 切换真实调用
if "--real" in sys.argv:
    os.environ["SRA_DRY_RUN"] = "false"
else:
    os.environ.setdefault("SRA_DRY_RUN", "true")

from runtime.cli import _load_env, auto_human_callback
_load_env()

import core.config as _cfgmod
_cfgmod._default_config = None
from core.config import get_config
from runtime.pipeline import Pipeline

resume = "--resume" in sys.argv
project_id = "proj_materials_thermoelectric"
# 解析 --project 参数
for i, arg in enumerate(sys.argv):
    if arg == "--project" and i + 1 < len(sys.argv):
        project_id = sys.argv[i + 1]

cfg = get_config()
print(f"[discovery] dry_run={cfg.dry_run}, resume={resume}, project={project_id}")

pipeline = Pipeline(config=cfg)
result = pipeline.run_discovery(
    project_id=project_id,
    topic="热电材料的构效关系与性能优化：基于文献驱动的材料发现智能体",
    human_callback=auto_human_callback,
    resume=resume,
)

print("\n" + "=" * 70)
print("[DISCOVERY RUN RESULT]")
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
