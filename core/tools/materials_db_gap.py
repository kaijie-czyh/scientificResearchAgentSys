"""材料数据库驱动的 Research Gap 证据增强工具（Materials Project + OQMD + NOMAD）。

赛题明确鼓励结合 Materials Project（materialsproject.org）、OQMD（oqmd.org）、
NOMAD（nomad-lab.eu）等公开计算材料数据库交叉验证。本模块把这三库从
「discovery 阶段的事后交叉验证」前移到「research 阶段的 Research Gap 识别」，
用数据库的定量事实（数据条目密度 / 带隙 / 形成焓 / 稳定性）来提升 Gap 的：

- 准确性：用数据库数据密度校验 Gap 是否真实（如「某材料数据稀缺」可被
  「三库命中数都很低」直接佐证，而非 LLM 泛泛推断）；
- 新颖性：数据库数据密度低 = 真正新颖的探索方向；数据库有而文献少的 =
  值得深挖的 underexplored；
- 可操作性：给出具体化学式 + 带隙 + 稳定性，让建议行动更具体；
- 文献溯源完整性：每条 Gap 附带 db_evidence（[{source, formula, entry_count,
  band_gap, ...}]），形成「文献 + 数据库」双证据链。

设计要点：
- 三库均优雅降级：API 不可用 / 超时 / 无 key 时不阻塞流程；
- NOMAD 用官方 REST API（https://nomad-lab.eu/prod/v1/api/v1/entries/query）
  的「元素查询」统计条目数（数据密度），不依赖 pymatgen 等重依赖；
- 化学式解析用正则提取元素符号，仅保留元素周期表内符号，过滤掺杂噪声。
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

_NOMAD_BASE = "https://nomad-lab.eu/prod/v1/api/v1"
_TIMEOUT = 25

# 常见元素符号集合（用于化学式解析，过滤 FA/MA 等有机官能团噪声）
_ELEMENTS = set(
    """H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni
    Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe
    Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au
    Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr""".split()
)


def _clean_formula(text: str) -> str:
    """归一化化学式：去 LaTeX / 下标 / 空格 / 常见包装。"""
    if not text:
        return ""
    s = str(text)
    # LaTeX 下标包装：$_2$ → 2，$_{2}$ → 2，\mathrm{X} → X
    s = re.sub(r"\$[_^{}]*([0-9]+)[_}]*\$", r"\1", s)
    s = re.sub(r"\$[_^{}]*([0-9]+\.[0-9]+)[_}]*\$", r"\1", s)
    s = s.replace("\\mathrm{", "").replace("\\text{", "")
    s = s.replace("{", "").replace("}", "")
    s = s.replace("$_", "").replace("$", "")
    # 下划线表示下标：Bi_2Te_3 → Bi2Te3
    s = re.sub(r"_(\d+)", r"\1", s)
    # 去空格 / 逗号 / 分号等分隔符
    s = re.sub(r"[\s,;]+", "", s)
    return s.strip()


def parse_elements(formula: str) -> list[str]:
    """从化学式提取元素符号（保留周期表内符号，过滤噪声，去重保序）。"""
    cleaned = _clean_formula(formula)
    if not cleaned:
        return []
    tokens = re.findall(r"[A-Z][a-z]?", cleaned)
    seen: list[str] = []
    for t in tokens:
        if t in _ELEMENTS and t not in seen:
            seen.append(t)
    return seen


# ===== NOMAD 客户端 =====


class NOMADClient:
    """NOMAD 客户端（官方 REST API v1，公开数据无需 auth）。"""

    BASE_URL = _NOMAD_BASE

    def query_elements(self, elements: list[str], page_size: int = 10) -> dict[str, Any]:
        """按元素集合查询 NOMAD 条目，返回数据密度（条目数）+ 示例化学式。

        Returns:
            {"matched": bool, "count": int, "formulas": [str], "source": str, "error": str}
        """
        if not elements:
            return {"matched": False, "count": 0, "formulas": [], "source": "nomad", "error": "no elements"}
        try:
            resp = requests.post(
                f"{self.BASE_URL}/entries/query",
                json={
                    "query": {"results.material.elements": {"all": elements}},
                    "pagination": {"page_size": page_size},
                    "required": {"include": ["entry_id", "results.material.chemical_formula_hill"]},
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            count = int((data.get("pagination") or {}).get("total", 0) or 0)
            formulas: list[str] = []
            for item in (data.get("data") or [])[:page_size]:
                f = (item.get("results", {}) or {}).get("material", {}) or {}
                hill = f.get("chemical_formula_hill", "")
                if hill:
                    formulas.append(hill)
            return {
                "matched": count > 0,
                "count": count,
                "formulas": formulas,
                "source": "nomad",
                "error": "",
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("NOMAD 查询失败（elements=%r）: %s", elements, e)
            return {"matched": False, "count": 0, "formulas": [], "source": "nomad_error", "error": str(e)}


_nomad_default: Optional[NOMADClient] = None


def _get_nomad_client() -> NOMADClient:
    global _nomad_default
    if _nomad_default is None:
        _nomad_default = NOMADClient()
    return _nomad_default


def query_nomad_by_formula(formula: str) -> dict[str, Any]:
    """模块级：按化学式查询 NOMAD（返回数据密度）。"""
    elements = parse_elements(formula)
    return _get_nomad_client().query_elements(elements)


# ===== 数据库聚合查询 =====


@dataclass
class MaterialDBEvidence:
    """单个材料在三个数据库的聚合证据。"""

    formula: str = ""
    name: str = ""
    # Materials Project
    mp_matched: bool = False
    mp_entry_count: int = 0
    mp_band_gap: Optional[float] = None
    mp_band_gap_range: str = ""
    # OQMD
    oqmd_matched: bool = False
    oqmd_entry_count: int = 0
    oqmd_band_gap: Optional[float] = None
    oqmd_formation_energy: Optional[float] = None
    oqmd_stability: str = ""
    # NOMAD
    nomad_matched: bool = False
    nomad_entry_count: int = 0
    nomad_formulas: list[str] = field(default_factory=list)
    # 汇总
    total_entry_count: int = 0
    source: str = ""  # 命中来源：mp / oqmd / nomad / hybrid / none

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "name": self.name,
            "mp": {
                "matched": self.mp_matched,
                "entry_count": self.mp_entry_count,
                "band_gap": self.mp_band_gap,
            },
            "oqmd": {
                "matched": self.oqmd_matched,
                "entry_count": self.oqmd_entry_count,
                "band_gap": self.oqmd_band_gap,
                "formation_energy": self.oqmd_formation_energy,
                "stability": self.oqmd_stability,
            },
            "nomad": {
                "matched": self.nomad_matched,
                "entry_count": self.nomad_entry_count,
                "formulas": self.nomad_formulas[:5],
            },
            "total_entry_count": self.total_entry_count,
            "source": self.source,
        }

    def db_evidence(self) -> dict[str, Any]:
        """以「数据库证据」形式返回（供 Gap 挂载，赛题文献溯源完整性）。"""
        return self.to_dict()


def _query_material_project(formula: str) -> dict[str, Any]:
    """查询 Materials Project（复用 materials_project 模块，避免循环导入）。"""
    try:
        from core.tools.materials_project import query_material_by_formula

        results = query_material_by_formula(formula)
        if results:
            band_gap = results[0].get("band_gap")
            return {"matched": True, "count": len(results), "band_gap": band_gap}
        return {"matched": False, "count": 0, "band_gap": None}
    except Exception as e:  # noqa: BLE001
        logger.warning("MP 查询失败（%s）: %s", formula, e)
        return {"matched": False, "count": 0, "band_gap": None, "error": str(e)}


def _query_oqmd(formula: str) -> dict[str, Any]:
    """查询 OQMD（复用 oqmd_nomad 模块）。"""
    try:
        from core.tools.oqmd_nomad import query_oqmd_by_formula

        res = query_oqmd_by_formula(formula)
        if res.matched and res.entries:
            e = res.entries[0]
            return {
                "matched": True,
                "count": len(res.entries),
                "band_gap": e.band_gap,
                "formation_energy": e.formation_energy,
                "stability": e.stability or "",
            }
        return {"matched": False, "count": 0, "band_gap": None, "formation_energy": None, "stability": ""}
    except Exception as e:  # noqa: BLE001
        logger.warning("OQMD 查询失败（%s）: %s", formula, e)
        return {"matched": False, "count": 0, "band_gap": None, "formation_energy": None, "stability": "", "error": str(e)}


def query_materials_databases(
    formulas: list[dict[str, str]],
    max_materials: int = 8,
) -> list[MaterialDBEvidence]:
    """对候选化学式批量查询 MP + OQMD + NOMAD，返回聚合证据列表。

    Args:
        formulas: [{"formula": "Bi2Te3", "name": "Bi2Te3"}, ...]（formula 优先，name 兜底）
        max_materials: 最多查询的材料数（控制网络开销）

    Returns:
        list[MaterialDBEvidence]
    """
    if not formulas:
        return []

    evidence_list: list[MaterialDBEvidence] = []

    def _query_one(f: dict[str, str]) -> MaterialDBEvidence:
        formula = _clean_formula(f.get("formula") or f.get("name") or "")
        name = f.get("name") or formula
        ev = MaterialDBEvidence(formula=formula, name=name)
        if not formula:
            return ev

        # 三库并行查询
        with ThreadPoolExecutor(max_workers=3) as pool:
            fut_mp = pool.submit(_query_material_project, formula)
            fut_oqmd = pool.submit(_query_oqmd, formula)
            fut_nomad = pool.submit(query_nomad_by_formula, formula)
            mp = fut_mp.result(timeout=_TIMEOUT + 5)
            oqmd = fut_oqmd.result(timeout=_TIMEOUT + 5)
            nomad = fut_nomad.result(timeout=_TIMEOUT + 5)

        ev.mp_matched = bool(mp.get("matched"))
        ev.mp_entry_count = int(mp.get("count") or 0)
        ev.mp_band_gap = mp.get("band_gap")
        ev.oqmd_matched = bool(oqmd.get("matched"))
        ev.oqmd_entry_count = int(oqmd.get("count") or 0)
        ev.oqmd_band_gap = oqmd.get("band_gap")
        ev.oqmd_formation_energy = oqmd.get("formation_energy")
        ev.oqmd_stability = oqmd.get("stability") or ""
        ev.nomad_matched = bool(nomad.get("matched"))
        ev.nomad_entry_count = int(nomad.get("count") or 0)
        ev.nomad_formulas = nomad.get("formulas") or []

        ev.total_entry_count = ev.mp_entry_count + ev.oqmd_entry_count + ev.nomad_entry_count
        hit_sources = []
        if ev.mp_matched:
            hit_sources.append("mp")
        if ev.oqmd_matched:
            hit_sources.append("oqmd")
        if ev.nomad_matched:
            hit_sources.append("nomad")
        ev.source = "hybrid" if len(hit_sources) >= 2 else (hit_sources[0] if hit_sources else "none")
        return ev

    # 顺序批量（避免一次开过多线程；单个材料内部已并行三库）
    for f in formulas[:max_materials]:
        try:
            evidence_list.append(_query_one(f))
        except Exception as e:  # noqa: BLE001
            logger.warning("材料数据库聚合查询失败（%r）: %s", f, e)
            formula = _clean_formula(f.get("formula") or f.get("name") or "")
            evidence_list.append(MaterialDBEvidence(formula=formula, name=f.get("name") or formula, source="error"))

    return evidence_list


# ===== 数据库证据块（注入 LLM prompt）=====


def build_db_evidence_block(evidence_list: list[MaterialDBEvidence]) -> str:
    """把数据库证据列表转成注入 LLM 的文本块。

    LLM 据此判断哪些方向是真正的数据缺口（数据稀疏），避免泛泛而谈。
    """
    if not evidence_list:
        return "（无材料数据库证据）"

    lines: list[str] = []
    for ev in evidence_list:
        if not ev.formula:
            continue
        seg = [f"- {ev.formula}"]
        parts = []
        if ev.mp_matched:
            parts.append(f"MP 命中 {ev.mp_entry_count} 条" + (f"（带隙 {ev.mp_band_gap:.2f} eV）" if ev.mp_band_gap is not None else ""))
        else:
            parts.append("MP 未命中")
        if ev.oqmd_matched:
            oq = f"OQMD 命中 {ev.oqmd_entry_count} 条"
            if ev.oqmd_band_gap is not None:
                oq += f"（带隙 {ev.oqmd_band_gap:.2f} eV）"
            if ev.oqmd_stability:
                oq += f"（{ev.oqmd_stability}）"
            parts.append(oq)
        else:
            parts.append("OQMD 未命中")
        if ev.nomad_matched:
            parts.append(f"NOMAD 命中 {ev.nomad_entry_count} 条")
        else:
            parts.append("NOMAD 未命中")
        parts.append(f"三库合计 {ev.total_entry_count} 条")
        seg.append("；".join(parts))
        lines.append(" ".join(seg))

    return "\n".join(lines)


# ===== 规则层数据库缺口检测 =====


def detect_db_gaps(
    evidence_list: list[MaterialDBEvidence],
    material_coverage: Optional[dict[str, dict[str, Any]]] = None,
    max_gaps: int = 6,
) -> list[dict]:
    """规则层识别「数据库驱动」的 Research Gap。

    基于三库数据密度与材料库文献覆盖度，识别三类缺口：

    - data_gap（数据稀缺）：三库命中都极低 → 缺乏可靠的 DFT 基准，是真实数据空白；
    - unexplored（数据库有、文献少）：数据库条目丰富但材料库内性能/合成记录少
      → 有可靠计算基准却缺实验验证，值得深挖；
    - contradiction（稳定性冲突）：数据库标记 unstable/带隙异常 但文献报道高性能
      → 计算与实验存在矛盾。

    Args:
        evidence_list: 数据库聚合证据列表
        material_coverage: {formula: {"name", "prop_count", "syn_count", "paper_id", "paper_title"}}
        max_gaps: 最多返回条数

    Returns:
        list[dict]：Gap dict（含 db_evidence，可直接并入 ResearchGapIdentifyAgent 输出）
    """
    coverage = material_coverage or {}
    gaps: list[dict] = []

    def _mk_gap(gap_type: str, statement: str, detail: str, ev: MaterialDBEvidence,
                actionability: str, priority: int, suggested: list[str]) -> dict:
        return {
            "gap_id": "",  # 由调用方回填
            "gap_type": gap_type,
            "statement": statement,
            "detail": detail,
            "evidence": [],
            "related_materials": [ev.name] if ev.name else [],
            "actionability": actionability,
            "priority": priority,
            "source": "db_driven",
            "suggested_actions": suggested,
            "subquery": "",
            "db_evidence": [ev.db_evidence()],
        }

    # 1) 数据稀缺（三库合计 0 或极低）
    for ev in evidence_list:
        if not ev.formula:
            continue
        if ev.total_entry_count == 0:
            gaps.append(_mk_gap(
                gap_type="data_gap",
                statement=f"{ev.name or ev.formula} 在 Materials Project / OQMD / NOMAD 中均无 DFT 记录",
                detail=(
                    f"材料 {ev.name or ev.formula} 在三大公开计算数据库中均未命中，"
                    "缺乏可靠的带隙 / 形成焓 / 稳定性基准。这既可能是真正未被计算的新体系"
                    "（新颖性强），也可能是化学式表述不规范导致检索失败，需人工复核。"
                ),
                ev=ev,
                actionability="high",
                priority=2,
                suggested=[
                    f"规范化 {ev.formula} 化学式后重查三库",
                    "若确为未计算体系，可作为新颖性强的候选方向优先 DFT 计算",
                ],
            ))

    # 2) 数据库有、文献少（underexplored）
    for ev in evidence_list:
        if not ev.formula:
            continue
        cov = coverage.get(ev.formula, {})
        prop_count = int(cov.get("prop_count", 0) or 0)
        syn_count = int(cov.get("syn_count", 0) or 0)
        # 数据库条目丰富（>=1 命中），但材料库内性能+合成都很少（实验验证稀缺）
        if ev.total_entry_count >= 1 and (prop_count == 0 and syn_count == 0):
            gaps.append(_mk_gap(
                gap_type="unexplored",
                statement=f"{ev.name or ev.formula} 有计算数据但缺少实验性能 / 合成验证",
                detail=(
                    f"材料 {ev.name or ev.formula} 在三库中共有 {ev.total_entry_count} 条计算记录"
                    f"（可作可靠基准），但库内文献未抽取到其性能 / 合成数据，"
                    "「计算有据、实验空白」是典型可深挖方向。"
                ),
                ev=ev,
                actionability="high",
                priority=3,
                suggested=[
                    f"以 {ev.formula} 的 DFT 带隙 / 形成焓为基准，检索其实验合成与性能报道",
                    "将其纳入 discovery 搜索空间作为候选材料体系",
                ],
            ))

    # 3) 稳定性冲突（OQMD 标记 unstable 但文献报道高性能）
    for ev in evidence_list:
        if not ev.formula:
            continue
        cov = coverage.get(ev.formula, {})
        prop_count = int(cov.get("prop_count", 0) or 0)
        if ev.oqmd_matched and ev.oqmd_stability and "unstable" in ev.oqmd_stability.lower() and prop_count > 0:
            gaps.append(_mk_gap(
                gap_type="contradiction",
                statement=f"{ev.name or ev.formula} 计算稳定性与实验性能报道存在矛盾",
                detail=(
                    f"OQMD 将 {ev.name or ev.formula} 标记为 {ev.oqmd_stability}，"
                    f"但库内已有 {prop_count} 条实验性能记录。计算与实验稳定性判据不一致，"
                    "需厘清是亚稳态可合成还是计算设置偏差。"
                ),
                ev=ev,
                actionability="medium",
                priority=4,
                suggested=[
                    "核查该材料的合成条件与相图（是否亚稳相）",
                    "用更高精度 DFT（含 vdW / 自旋轨道耦合）复算形成焓",
                ],
            ))

    return gaps[:max_gaps]


def collect_material_formulas(store: Any, max_materials: int = 8) -> list[dict[str, str]]:
    """从 KnowledgeStore 材料库提取候选化学式列表（formula 优先，name 兜底，去重）。"""
    if store is None:
        return []
    try:
        materials = store.list_materials(limit=2000) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("读取材料库失败: %s", e)
        return []

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for m in materials:
        name = getattr(m, "name", "") or ""
        formula = getattr(m, "formula", "") or ""
        key = _clean_formula(formula) or _clean_formula(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"formula": formula or name, "name": name or formula})
        if len(out) >= max_materials:
            break
    return out


def build_material_coverage(store: Any) -> dict[str, dict[str, Any]]:
    """构建 {formula: {name, prop_count, syn_count, paper_id, paper_title}} 覆盖度表。

    供 detect_db_gaps 判断「数据库有、文献少」缺口。
    """
    coverage: dict[str, dict[str, Any]] = {}
    if store is None:
        return coverage
    try:
        materials = store.list_materials(limit=2000) or []
        props = store.list_material_properties(limit=5000) or []
        syns = store.list_material_synthesis(limit=5000) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("构建材料覆盖度失败: %s", e)
        return coverage

    prop_by_mat: dict[str, int] = {}
    for p in props:
        prop_by_mat[p.material_id] = prop_by_mat.get(p.material_id, 0) + 1
    syn_by_mat: dict[str, int] = {}
    for s in syns:
        syn_by_mat[s.material_id] = syn_by_mat.get(s.material_id, 0) + 1

    for m in materials:
        key = _clean_formula(getattr(m, "formula", "") or "") or _clean_formula(getattr(m, "name", "") or "")
        if not key:
            continue
        coverage[key] = {
            "name": getattr(m, "name", "") or key,
            "prop_count": prop_by_mat.get(m.material_id, 0),
            "syn_count": syn_by_mat.get(m.material_id, 0),
            "paper_id": getattr(m, "paper_id", None) or "",
            "paper_title": getattr(m, "paper_title", "") or "",
        }
    return coverage
