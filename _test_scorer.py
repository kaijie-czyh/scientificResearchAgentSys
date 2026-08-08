import sys
sys.path.insert(0, '.')
from core.config import get_config
from core.knowledge import KnowledgeStore
from core.tools.discovery_metrics import DiscoveryReliabilityScorer, ExpertAssistanceBuilder, score_discoveries

cfg = get_config()
projects = [(p.name, p.stat().st_mtime) for p in cfg.paths.projects.iterdir() if p.is_dir()]
projects.sort(key=lambda x: x[1], reverse=True)
for name, _ in projects[:3]:
    project_id = name.replace('projects\\', '').replace('projects/', '')
    db = cfg.paths.project_db(project_id)
    if not db.exists():
        continue
    store = KnowledgeStore(db)
    search_space = store.get_kv("discovery_search_space") or {}
    lit_points = store.get_kv("discovery_literature_points") or []
    rels = store.get_kv("discovery_relationships") or []
    print(f'=== Project: {project_id} ===')
    print(f'  relationships count: {len(rels)}')
    if rels:
        print(f'  sample relationship keys: {list(rels[0].keys())[:10]}')
        print(f'  sample material: {rels[0].get("config", {}).get("material", "?")}')
        try:
            scorer = DiscoveryReliabilityScorer(store)
            s = scorer.score(rels[0], search_space, lit_points)
            print(f'  reliability_score: {s["reliability_score"]}')
            print(f'  dimensions: {s["dimensions"]}')
            print(f'  risk_label: {s["risk_label"]}')
            try:
                expert = ExpertAssistanceBuilder(store)
                a = expert.build_for_discovery(rels[0], search_space)
                print(f'  expert assistance: NN={len(a.get("nearest_neighbor_synthesis", []))}, similar={len(a.get("similar_materials_table", []))}')
            except Exception as e:
                print(f'  Expert error: {e}')
        except Exception as e:
            print(f'  Scorer error: {type(e).__name__}: {e}')
    print()