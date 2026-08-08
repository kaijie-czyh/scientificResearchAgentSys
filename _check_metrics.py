import sys, json, os
sys.path.insert(0, '.')
from core.config import get_config
from core.knowledge import KnowledgeStore

cfg = get_config()
projects = [(p.name, p.stat().st_mtime) for p in cfg.paths.projects.iterdir() if p.is_dir()]
projects.sort(key=lambda x: x[1], reverse=True)
for name, _ in projects[:3]:
    project_id = name.replace('projects\\', '').replace('projects/', '')
    db = cfg.paths.project_db(project_id)
    if not db.exists():
        continue
    store = KnowledgeStore(db)
    gap_scores = store.get_kv("research_gap_scores") or {}
    rel_scores = store.get_kv("discovery_reliability_scores") or {}
    assistances = store.get_kv("discovery_expert_assistance") or []
    search_space = store.get_kv("discovery_search_space") or {}
    print(f'=== Project: {project_id} ===')
    print(f'  search_space has {len(search_space.get("variables", []))} vars, {len(search_space.get("literature_points", []))} lit_points')
    if gap_scores:
        print(f'  Gap scores summary: {gap_scores.get("summary")}')
    if rel_scores:
        print(f'  Rel scores summary: {rel_scores.get("summary")}')
        for s in (rel_scores.get("scores") or [])[:2]:
            print(f"    - reliability={s.get('reliability_score')}, risk={s.get('risk_label')}")
            print(f"      dims={s.get('dimensions')}")
    if assistances:
        a = assistances[0]
        print(f'\n  Expert assistance for {a.get("material")}:')
        nns = a.get("nearest_neighbor_synthesis", [])
        print(f'    Nearest neighbors ({len(nns)}):')
        for s in nns[:2]:
            print(f"      - {s.get('source_material')} (sim={s.get('similarity')}): {s.get('method')}")
        sim_table = a.get("similar_materials_table", [])
        print(f'    Similar materials ({len(sim_table)}):')
        for s in sim_table[:2]:
            print(f"      - {s.get('material')} (sim={s.get('similarity')}): {s.get('value')}{s.get('unit')}")
        dft = a.get("dft_verification_protocol", {})
        print(f'    DFT tasks: {dft.get("tasks", [])[:5]}')
    print()