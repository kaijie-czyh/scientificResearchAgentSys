"""Task 2 验证：真实 LLM 抽取 material_extraction 节点。

复用旧项目 proj_20260806_152947_31cdd6 已入库的论文（跳过抓取阶段），
直接驱动 MaterialKnowledgeExtractionAgent 真实抽取（DRY_RUN=False），
验证：tasks.yaml 注册生效 + LLM 抽取 + 落库 + 三元组统计。

用法：python verify_material_extract.py [limit]
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# 加载 .env 到进程环境（服务由 shell 注入，脚本进程需手动加载）
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from core.config import get_config
from core.knowledge import KnowledgeStore
from core.orchestration.node import NodeStatus
from runtime.pipeline import Pipeline
from stages.common import (
    DRY_RUN,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    RESEARCH_PAPER_IDS,
    RESEARCH_TOPIC,
)
from stages.research.agents import MaterialKnowledgeExtractionAgent


def main() -> int:
    args = sys.argv[1:]
    reset = "--reset" in args
    limit = None
    for a in args:
        if a.isdigit():
            limit = int(a)
    project_id = "proj_20260806_152947_31cdd6"
    topic = "thermoelectric material defect engineering"

    config = get_config()
    pipe = Pipeline(config)
    session, ctx = pipe.resume_project(project_id, topic=topic)

    store = ctx.get(KNOWLEDGE_STORE)
    if reset:
        print("[0] 清空材料三表（重置验证）...")
        store._reset_material_tables()
    papers = store.list_papers()
    if limit:
        papers = papers[:limit]
    paper_ids = [p.paper_id for p in papers]
    print(f"[1] 论文加载: {len(papers)} 篇 (limit={limit or '全量'}, 库内共 "
          f"{len(store.list_papers())} 篇)")
    if not paper_ids:
        print("!!! 项目库无论文，无法验证")
        return 1

    # 强制真实模式
    ctx.set(DRY_RUN, False)
    ctx.set(RESEARCH_PAPER_IDS, paper_ids)

    agent = MaterialKnowledgeExtractionAgent(node_id="material_extraction")
    print(f"[2] 开始真实 LLM 抽取 {len(papers)} 篇论文 ...")
    result = agent.run(ctx)
    print(f"[3] status={result.status}")
    print(f"    summary={result.summary}")
    if result.status != NodeStatus.SUCCESS:
        print(f"    error={result.error}")
        return 1

    mstats = store.material_stats()
    print(f"[4] 库内统计: {mstats}")

    mats = store.list_materials(limit=5)
    for m in mats:
        props = store.list_material_properties(m.material_id, limit=3)
        syns = store.list_material_synthesis(m.material_id, limit=3)
        print(f"    - {m.name} | 结构={m.crystal_structure or m.space_group or '-'} "
              f"| 性能 {len(props)} 条 | 合成 {len(syns)} 条")
        for p in props[:2]:
            print(f"        prop: {p.property_name} = {p.value} @ {p.condition}")
        for s in syns[:2]:
            print(f"        syn: {s.method} | {s.temperature} | {s.precursors}")

    print(f"[5] 完成: 材料 {mstats['materials']} / 性能 {mstats['properties']} "
          f"/ 合成 {mstats['synthesis']} / 完整三元组 {mstats['complete_triples']}")
    return 0 if mstats["materials"] > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
