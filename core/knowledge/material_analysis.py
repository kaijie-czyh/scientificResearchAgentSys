"""材料深度分析规则引擎（确定性底座，不调用 LLM，不编造数据）。

把「材料信息罗列」升级为「材料选择 + 性质判断 + 合成方案设计 + 实验决策」的
分析能力。核心原则：

1. **绝不编造科研数据**：所有数值必须来自输入的性质/合成实体，缺失即标记
   ``missing=True`` / ``暂无可靠文献数据``，机制解释只来自领域规则库（物理规律），
   不生成"看起来像实验数据"的数字。
2. **证据可溯源**：每个评分/结论尽量附带 ``evidence``（paper_title / snippet），
   证据等级 A/B/C/D/E 由输入数据推导，LLM 推断值必须显式标注 E。
3. **纯函数**：输入 store 中的 Material / MaterialProperty / MaterialSynthesis，
   输出 dict（供 API 序列化为 MaterialProfile 等结构）。

LLM 分析节点（MaterialAnalysisAgent）在本引擎的确定性结果之上，仅做机制语义
补全与推荐理由润色；LLM 失败或 dry_run 时，本引擎的规则结果即为最终兜底。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from core.knowledge.schema import (
    EVIDENCE_LEVEL_LABELS,
    Material,
    MaterialProperty,
    MaterialSynthesis,
)
from core.knowledge.normalize import (
    DIMENSION_LABELS,
    classify_method,
    normalize_property,
)

# ========================================================================
# 1. 性质 → 机制 → 目标性能 关系库（领域物理规律，非实验数据）
# ========================================================================
# 每条：property key → {mechanism 物理机制, impact 对目标性能影响, targets 关联目标}
# 这是「性质关系解释」的确定性底座。物理规律可靠，不是编造的实验数值。

MECHANISM_LIBRARY: dict[str, dict[str, Any]] = {
    "carrier_concentration": {
        "mechanism": "载流子浓度通过费米能级位置同时调控电导率与 Seebeck 系数（σ=n·e·μ，S∝1/n）",
        "impact": "载流子浓度↑ → 电导率↑、Seebeck↓；存在最优掺杂浓度使功率因子 S²σ 最大",
        "targets": ["ZT", "power_factor"],
    },
    "carrier_mobility": {
        "mechanism": "迁移率反映载流子散射强弱，受晶界/缺陷/声子/杂质散射影响",
        "impact": "迁移率↑ → 电导率↑，利于功率因子提升",
        "targets": ["ZT", "power_factor", "electrical_conductivity"],
    },
    "seebeck_coefficient": {
        "mechanism": "Seebeck 系数由态密度有效质量与费米能级决定，与载流子浓度成反比",
        "impact": "S↑ → 功率因子 S²σ↑，但通常伴随载流子浓度↓导致 σ↓，需权衡",
        "targets": ["ZT", "power_factor"],
    },
    "electrical_conductivity": {
        "mechanism": "电导率 σ=n·e·μ，由载流子浓度与迁移率共同决定",
        "impact": "σ↑ → 功率因子↑，但电子热导率 κ_e 同步↑（Wiedemann-Franz 定律）",
        "targets": ["ZT", "power_factor"],
    },
    "electrical_resistivity": {
        "mechanism": "电阻率 ρ=1/σ，是电导率的倒数",
        "impact": "ρ↓ → 电导率↑，利于功率因子，但不利于绝缘/介电应用",
        "targets": ["electrical_conductivity"],
    },
    "thermal_conductivity": {
        "mechanism": "总热导率 κ=κ_L+κ_e（晶格声子 + 电子载流子贡献）",
        "impact": "κ↓ → ZT↑；降低总热导率是提升热电优值的直接路径",
        "targets": ["ZT"],
    },
    "lattice_thermal_conductivity": {
        "mechanism": "晶格热导率由声子输运决定，缺陷/晶界/纳米析出/固溶可增强声子散射",
        "impact": "κ_L↓ → ZT↑，是热电材料优化的核心路径",
        "targets": ["ZT"],
    },
    "electronic_thermal_conductivity": {
        "mechanism": "电子热导率通过 Wiedemann-Franz 定律与电导率耦合（κ_e=LσT）",
        "impact": "κ_e 随 σ 同步变化，电导率提升会部分抵消热导优势",
        "targets": ["ZT"],
    },
    "power_factor": {
        "mechanism": "功率因子 PF=S²σ，综合衡量电输运能力",
        "impact": "PF↑ → ZT 分子项↑，直接提升 ZT",
        "targets": ["ZT"],
    },
    "band_gap": {
        "mechanism": "带隙决定本征激发温度与双极扩散（双极热导）起始温度",
        "impact": "带隙过小 → 高温双极扩散 → Seebeck↓、双极热导↑，限制 ZT 上限",
        "targets": ["ZT", "photoresponse_range"],
    },
    "direct_band_gap": {
        "mechanism": "直接带隙材料光吸收效率高（动量守恒无需声子参与）",
        "impact": "直接带隙利于光电/光吸收应用，间接带隙利于载流子寿命",
        "targets": ["photoresponse_range"],
    },
    "effective_mass": {
        "mechanism": "有效质量 m* 通过态密度影响 Seebeck 系数（m*↑→S↑）",
        "impact": "m*↑ → S↑ 但迁移率↓，需在二者间平衡",
        "targets": ["ZT", "seebeck_coefficient"],
    },
    "debye_temperature": {
        "mechanism": "德拜温度反映晶格刚度与声速，低德拜温度倾向低晶格热导率",
        "impact": "低德拜温度材料倾向低 κ_L，利于 ZT",
        "targets": ["ZT", "lattice_thermal_conductivity"],
    },
    "gruneisen_parameter": {
        "mechanism": "格林艾森参数衡量晶格非谐性，非谐性强 → 声子-声子散射强",
        "impact": "γ↑ → κ_L↓，利于 ZT",
        "targets": ["ZT", "lattice_thermal_conductivity"],
    },
    "anharmonicity": {
        "mechanism": "晶格非谐性增强声子散射，抑制声子热输运",
        "impact": "非谐性↑ → κ_L↓，利于 ZT",
        "targets": ["ZT", "lattice_thermal_conductivity"],
    },
    "phonon_mean_free_path": {
        "mechanism": "声子平均自由程决定声子散射长度，纳米结构可选择性散射长程声子",
        "impact": "声子自由程↓ → κ_L↓，利于 ZT",
        "targets": ["ZT", "lattice_thermal_conductivity"],
    },
    "formation_energy": {
        "mechanism": "形成能衡量相的热力学稳定性（负值越大越稳定）",
        "impact": "形成能越负 → 相越稳定，利于长期服役",
        "targets": ["stability"],
    },
    "carrier_lifetime": {
        "mechanism": "载流子寿命由复合机制（辐射/缺陷复合）决定",
        "impact": "寿命↑ → 利于光电转换/太阳能电池效率",
        "targets": ["energy_conversion_efficiency"],
    },
    "absorption_coefficient": {
        "mechanism": "吸收系数决定光在材料中的衰减长度，与带隙类型和跃迁概率相关",
        "impact": "吸收系数↑ → 更薄器件即可充分吸光，利于光伏/光催化",
        "targets": ["photoresponse_range", "energy_conversion_efficiency"],
    },
    "young_modulus": {
        "mechanism": "杨氏模量反映材料弹性刚度，与键强和晶体结构相关",
        "impact": "杨氏模量↑ → 结构刚度↑，但脆性可能↑",
        "targets": ["mechanical_stability"],
    },
    "hardness": {
        "mechanism": "硬度反映材料抵抗局部塑性变形的能力",
        "impact": "硬度↑ → 耐磨性↑，利于结构/涂层应用",
        "targets": ["mechanical_stability"],
    },
    "fracture_toughness": {
        "mechanism": "断裂韧性衡量材料抵抗裂纹扩展的能力",
        "impact": "韧性↑ → 抗断裂能力↑，利于结构可靠性",
        "targets": ["mechanical_stability"],
    },
    "decomposition_temperature": {
        "mechanism": "分解温度是材料热力学稳定上限，超过则发生相分解/元素挥发",
        "impact": "分解温度↑ → 高温稳定性↑，拓宽工作温度范围",
        "targets": ["stability"],
    },
}

# 证据等级 → 强度权重（用于证据强度评分，A 最强）
_EVIDENCE_WEIGHT = {"A": 1.0, "B": 0.8, "C": 0.6, "D": 0.5, "E": 0.2, "": 0.2}


def _evidence_level(props: Iterable[MaterialProperty], syns: Iterable[MaterialSynthesis]) -> tuple[str, int]:
    """由一组性质/合成推导整体证据等级（取最强等级 + 独立文献数）。

    A(多篇实验) > B(单篇实验) > C(多篇间接) > D(理论/数据库) > E(LLM 推断)
    """
    levels = [p.evidence_level for p in props if p.evidence_level] + \
             [s.evidence_level for s in syns if s.evidence_level]
    # 独立来源数：去重 paper_id
    pids = {p.paper_id for p in props if p.paper_id} | {s.paper_id for s in syns if s.paper_id}
    if not levels:
        return "E", len(pids)
    # 取最强等级（A 优先）
    best = "E"
    for lv in ("A", "B", "C", "D", "E"):
        if lv in levels:
            best = lv
            break
    # 若多条证据但等级为 B，升级为 A（多篇实验）
    if best == "B" and len(pids) >= 2:
        best = "A"
    return best, len(pids)


def _derive_evidence_level(n_papers: int, data_type: str, source_type: str) -> str:
    """单条性质/合成的证据等级推导（规则）。"""
    if source_type in ("materials_project", "sciverse", "database") or data_type == "database":
        return "D"
    if data_type == "theoretical":
        return "D"
    if data_type == "inferred" or source_type == "llm_inference":
        return "E"
    # 实验值：按文献数
    if n_papers >= 2:
        return "A"
    if n_papers == 1:
        return "B"
    return "C"


# ========================================================================
# 2. 目标性能因果拆解库（如 ZT = S²σT/κ）
# ========================================================================

TARGET_LIBRARY: dict[str, dict[str, Any]] = {
    "ZT": {
        "formula": "ZT = S²σT / κ = S²σT / (κ_L + κ_e)",
        "factors": [
            {"factor": "seebeck_coefficient", "factor_cn": "Seebeck 系数", "role": "分子平方项，对 ZT 贡献最大"},
            {"factor": "electrical_conductivity", "factor_cn": "电导率", "role": "与 S² 乘积构成功率因子"},
            {"factor": "power_factor", "factor_cn": "功率因子", "role": "S²σ，综合衡量电输运能力"},
            {"factor": "thermal_conductivity", "factor_cn": "热导率", "role": "分母项，需最小化"},
            {"factor": "temperature", "factor_cn": "温度", "role": "线性放大项，但 S/σ/κ 均随温度变化"},
        ],
    },
    "power_factor": {
        "formula": "PF = S²σ",
        "factors": [
            {"factor": "seebeck_coefficient", "factor_cn": "Seebeck 系数", "role": "平方项，主导贡献"},
            {"factor": "electrical_conductivity", "factor_cn": "电导率", "role": "线性项，与 S 存在 trade-off"},
        ],
    },
    "energy_conversion_efficiency": {
        "formula": "η = (T_h - T_c)/T_h · (√(1+ZT)-1)/(√(1+ZT)+T_c/T_h)",
        "factors": [
            {"factor": "ZT", "factor_cn": "热电优值", "role": "决定材料效率上限"},
            {"factor": "temperature", "factor_cn": "冷热端温差", "role": "卡诺效率因子"},
        ],
    },
}

# 目标性能 → 目标性质列表（识别"当前研究目标"用）
_TARGET_PROPERTY_HINTS = {
    "ZT": ["ZT", "power_factor", "seebeck_coefficient", "thermal_conductivity"],
    "power_factor": ["power_factor", "seebeck_coefficient", "electrical_conductivity"],
    "energy_conversion_efficiency": ["energy_conversion_efficiency", "ZT"],
}


# ========================================================================
# 3. 对比矩阵：单位统一 + 范围显示 + 证据标注（不伪造缺失值）
# ========================================================================

# 常见热电性质的单位换算（换算到标准单位）
_UNIT_SCALE = {
    "electrical_conductivity": {"S/cm": 1.0, "S/m": 0.01, "S·cm⁻¹": 1.0, "S·m⁻¹": 0.01},
    "thermal_conductivity": {"W/m·K": 1.0, "W/mK": 1.0, "W/cm·K": 100.0, "W/cmK": 100.0},
    "seebeck_coefficient": {"μV/K": 1.0, "µV/K": 1.0, "V/K": 1e6},
    "carrier_concentration": {"cm⁻³": 1.0, "cm-3": 1.0, "m⁻³": 1e-6, "m-3": 1e-6},
}


def _scale_value(value_num: Optional[float], unit: str, norm_key: str) -> Optional[float]:
    """把 value_num 换算到标准单位（用于跨文献比较）。无法换算返回原值。"""
    if value_num is None:
        return None
    scale_map = _UNIT_SCALE.get(norm_key)
    if not scale_map:
        return value_num
    factor = scale_map.get((unit or "").strip())
    if factor is None:
        return value_num
    return value_num * factor


def build_comparison(
    materials: list[Material],
    props_by_mat: dict[str, list[MaterialProperty]],
) -> dict[str, Any]:
    """构建材料横向对比矩阵。

    同一性质存在多个文献值时显示范围（min–max），并标注文献数/证据等级/测试温度/
    实验或理论类型。数据缺失的材料标记 missing=True，绝不伪造。

    Returns:
        {
            "properties": [{"norm_key", "norm_cn", "symbol", "unit", "dimension"}, ...],
            "matrix": {material_id: {"name", "cells": {norm_key: cell}}},
        }
    """
    # 收集所有性质 key（去重，按维度排序）
    all_keys: list[tuple[str, str]] = []  # (norm_key, norm_cn)
    seen: set[str] = set()
    for props in props_by_mat.values():
        for p in props:
            norm = normalize_property(p.property_name, p.property_name_cn)
            k = norm["key"]
            if k and k not in seen:
                seen.add(k)
                all_keys.append((k, norm["cn"] or p.property_name_cn or p.property_name))
    # 按维度分组排序
    _dim_order = {d: i for i, d in enumerate(DIMENSION_LABELS)}

    def _sort_key(item: tuple[str, str]) -> tuple[int, str]:
        norm = normalize_property(item[0], item[1])
        dim = norm.get("dimension", "other")
        return (_dim_order.get(dim, 99), item[0])

    all_keys.sort(key=_sort_key)

    props_meta = []
    for k, cn in all_keys:
        norm = normalize_property(k, cn)
        props_meta.append({
            "norm_key": k, "norm_cn": norm["cn"], "symbol": norm["symbol"],
            "unit": norm["unit"], "dimension": norm.get("dimension", "other"),
        })

    matrix: dict[str, dict[str, Any]] = {}
    for m in materials:
        cells: dict[str, Any] = {}
        props = props_by_mat.get(m.material_id, [])
        # 按 norm_key 聚合
        by_key: dict[str, list[MaterialProperty]] = {}
        for p in props:
            norm = normalize_property(p.property_name, p.property_name_cn)
            by_key.setdefault(norm["key"], []).append(p)
        for k, cn in all_keys:
            vals = by_key.get(k, [])
            if not vals:
                cells[k] = {"material": m.name, "missing": True, "value": "暂无数据",
                            "unit": "", "evidence_level": "", "paper_count": 0}
                continue
            # 单位统一 + 数值范围
            nums = [_scale_value(p.value_num, p.unit, k) for p in vals]
            nums = [x for x in nums if x is not None]
            units = {p.unit for p in vals if p.unit}
            unit = next(iter(units), "") if units else ""
            if nums:
                lo, hi = min(nums), max(nums)
                value = f"{lo:.3g}" if abs(lo - hi) < 1e-9 else f"{lo:.3g}–{hi:.3g}"
            else:
                value = vals[0].value or "暂无数据"
            # 温度范围
            temps = [p.test_temperature or p.condition for p in vals if (p.test_temperature or p.condition)]
            temp = "–".join(sorted({t for t in temps if t})) if temps else ""
            # 数据类型（实验/理论，取最可信）
            dtypes = {p.data_type for p in vals if p.data_type}
            data_type = "experimental" if "experimental" in dtypes else (next(iter(dtypes), "") if dtypes else "")
            # 证据等级
            lvl, npapers = _evidence_level(vals, [])
            cells[k] = {
                "material": m.name, "missing": False, "value": value, "unit": unit,
                "source": "; ".join(sorted({p.paper_title for p in vals if p.paper_title})),
                "data_type": data_type, "test_temperature": temp,
                "confidence": round(max((p.confidence or 0) for p in vals), 2),
                "evidence_level": lvl, "evidence_label": EVIDENCE_LEVEL_LABELS.get(lvl, lvl),
                "paper_count": npapers,
            }
        matrix[m.material_id] = {"name": m.name, "formula": m.formula, "cells": cells}

    return {"properties": props_meta, "matrix": matrix}


# ========================================================================
# 4. 材料候选排序（材料选择决策）
# ========================================================================

RANKING_WEIGHTS = {
    "target_potential": 0.30,      # 目标性能潜力
    "evidence_strength": 0.20,     # 文献证据强度
    "structure_match": 0.15,       # 结构/性质匹配度
    "synthesis_feasibility": 0.15, # 合成可行性
    "stability": 0.10,             # 稳定性
    "novelty": 0.10,               # 创新性
}

# 目标性能相关性质（用于目标潜力评分）
_TARGET_KEYS = {
    "ZT": {"ZT", "power_factor"},
    "power_factor": {"power_factor"},
    "energy_conversion_efficiency": {"energy_conversion_efficiency", "ZT"},
    "default": {"ZT", "power_factor", "energy_conversion_efficiency"},
}


def rank_candidates(
    materials: list[Material],
    props_by_mat: dict[str, list[MaterialProperty]],
    syns_by_mat: dict[str, list[MaterialSynthesis]],
    target: str = "ZT",
) -> list[dict[str, Any]]:
    """材料候选排序：按六维加权评分，评分可追溯到证据。

    Returns:
        [{material, formula, composite_score, dimensions, strengths, risks, reason, evidence}, ...]
    """
    target_keys = _TARGET_KEYS.get(target, _TARGET_KEYS["default"])
    scored: list[dict[str, Any]] = []
    for m in materials:
        props = props_by_mat.get(m.material_id, [])
        syns = syns_by_mat.get(m.material_id, [])
        # 目标性能潜力：找目标性质的最大值（归一化到 0-100）
        target_vals = []
        for p in props:
            norm = normalize_property(p.property_name, p.property_name_cn)
            if norm["key"] in target_keys and p.value_num is not None:
                target_vals.append(p.value_num)
        target_potential = min(100.0, max(target_vals) * 40 if target_vals else 0.0)

        # 证据强度：证据等级 + 文献数
        lvl, npapers = _evidence_level(props, syns)
        ev_strength = _EVIDENCE_WEIGHT.get(lvl, 0.2) * 60 + min(40.0, npapers * 10)

        # 结构/性质匹配度：有晶体结构信息 + 性质覆盖维度数
        structure_match = 0.0
        if m.crystal_structure or m.space_group:
            structure_match += 40
        dims = {normalize_property(p.property_name, p.property_name_cn).get("dimension", "other") for p in props}
        structure_match += min(40.0, len(dims) * 8)
        structure_match += 20.0 if m.formula else 0.0

        # 合成可行性：有合成方法 + 可复现性
        syn_feas = 0.0
        if syns:
            syn_feas += 50
            best_rep = max(
                (s.reproducibility_score or 0 for s in syns if s.reproducibility_score is not None),
                default=0,
            )
            if best_rep:
                syn_feas += best_rep * 0.5
        else:
            syn_feas = 20.0  # 无合成信息，可行性未知

        # 稳定性：有稳定性类性质（形成能/分解温度/空气/水稳定性）
        stab = 0.0
        stab_keys = {"formation_energy", "decomposition_temperature", "air_stability",
                     "water_stability", "oxidation_resistance", "thermal_stability"}
        stab_props = [p for p in props if normalize_property(p.property_name, p.property_name_cn)["key"] in stab_keys]
        if stab_props:
            stab = 60.0
            stab_lvl, _ = _evidence_level(stab_props, [])
            stab += _EVIDENCE_WEIGHT.get(stab_lvl, 0.2) * 40

        # 创新性：有明确化学式 + 跨文献来源数（规则近似，不依赖年份数据）
        novelty = 40.0
        if m.formula:
            novelty += 20.0
        novelty += min(20.0, npapers * 10.0)
        novelty += 20.0 if m.source_snippet else 0.0

        dims_scored = {
            "target_potential": round(target_potential, 1),
            "evidence_strength": round(ev_strength, 1),
            "structure_match": round(structure_match, 1),
            "synthesis_feasibility": round(syn_feas, 1),
            "stability": round(stab, 1),
            "novelty": round(novelty, 1),
        }
        composite = sum(dims_scored[k] * RANKING_WEIGHTS[k] for k in RANKING_WEIGHTS)

        # 优势/风险（由规则推导，可溯源）
        strengths: list[str] = []
        risks: list[str] = []
        if target_vals:
            strengths.append(f"目标性能最高达 {max(target_vals):.3g}（来自 {npapers} 篇文献）")
        if m.crystal_structure:
            strengths.append(f"具备明确晶体结构信息（{m.crystal_structure}）")
        if stab_props:
            strengths.append("有稳定性相关性质记录")
        if syns:
            strengths.append(f"有 {len(syns)} 条合成方法记录")
        if not target_vals:
            risks.append("目标性能数值缺失，性能潜力评估不足")
        if not syns:
            risks.append("无合成方法记录，合成可行性未知")
        if lvl in ("E", ""):
            risks.append("证据等级偏低（多为推断/单一来源）")

        evidence = sorted({p.paper_title for p in props if p.paper_title} |
                          {s.paper_title for s in syns if s.paper_title})

        scored.append({
            "material": m.name, "formula": m.formula,
            "composite_score": round(composite, 1),
            "dimensions": dims_scored,
            "strengths": strengths, "risks": risks,
            "reason": _ranking_reason(m.name, dims_scored, composite),
            "evidence": evidence[:5],
        })

    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    return scored


def _ranking_reason(name: str, dims: dict[str, float], composite: float) -> str:
    """生成候选推荐理由（规则模板，非编造数据）。"""
    top = max(dims, key=dims.get)
    top_cn = {
        "target_potential": "目标性能潜力", "evidence_strength": "文献证据强度",
        "structure_match": "结构/性质匹配度", "synthesis_feasibility": "合成可行性",
        "stability": "稳定性", "novelty": "创新性",
    }.get(top, top)
    return f"{name} 综合评分 {composite:.0f}/100，主要优势维度为「{top_cn}」（{dims[top]:.0f} 分）"


# ========================================================================
# 5. 合成路线对比 / 风险 / 可复现性（P4 复用）
# ========================================================================

# 工艺类别 → 路线画像（确定性领域知识，用于路线对比与推荐）
ROUTE_PROFILES: dict[str, dict[str, Any]] = {
    "固相法": {"temperature": "高温(>800℃)", "cost": "低", "phase_purity": "高",
               "particle_control": "中", "scale": "易放大", "equipment": "管式炉/箱式炉"},
    "烧结法": {"temperature": "中高温", "cost": "中", "phase_purity": "高",
               "particle_control": "中", "scale": "易放大", "equipment": "SPS/热压机"},
    "熔融法": {"temperature": "高温(>熔点)", "cost": "中", "phase_purity": "高",
               "particle_control": "低", "scale": "中", "equipment": "电弧炉/感应炉"},
    "球磨法": {"temperature": "室温", "cost": "低", "phase_purity": "中",
               "particle_control": "高", "scale": "易放大", "equipment": "行星球磨机"},
    "溶液法": {"temperature": "低温(<200℃)", "cost": "中", "phase_purity": "中",
               "particle_control": "高", "scale": "中", "equipment": "反应釜/烧瓶"},
    "薄膜法": {"temperature": "中温", "cost": "高", "phase_purity": "高",
               "particle_control": "高", "scale": "难放大", "equipment": "CVD/PVD 设备"},
    "电化学法": {"temperature": "室温~中温", "cost": "中", "phase_purity": "中",
                 "particle_control": "高", "scale": "中", "equipment": "电化学工作站"},
    "纳米合成": {"temperature": "低温", "cost": "中", "phase_purity": "中",
                 "particle_control": "高", "scale": "难放大", "equipment": "模板/胶体合成装置"},
    "计算模拟": {"temperature": "—", "cost": "低", "phase_purity": "—",
                 "particle_control": "—", "scale": "—", "equipment": "计算集群"},
    "未指定": {"temperature": "—", "cost": "—", "phase_purity": "—",
               "particle_control": "—", "scale": "—", "equipment": "—"},
}

# 工艺类别 → 常见风险（确定性领域知识；来源标为"领域规则"，非文献）
METHOD_RISK_LIBRARY: dict[str, list[dict[str, str]]] = {
    "固相法": [
        {"risk": "第二相/杂质相生成", "level": "Medium", "reason": "高温长时间烧结易产生未反应物或杂相"},
        {"risk": "元素挥发", "level": "High", "reason": "含易挥发元素（如 Se/Te/Sb）在高温下易损失，导致化学计量偏离"},
        {"risk": "晶粒异常长大", "level": "Medium", "reason": "高温长时间保温导致晶粒粗化"},
    ],
    "烧结法": [
        {"risk": "第二相生成", "level": "Medium", "reason": "热压/SPS 过程中局部成分不均匀"},
        {"risk": "晶粒长大", "level": "Medium", "reason": "长时间高温烧结导致晶粒粗化"},
    ],
    "熔融法": [
        {"risk": "元素挥发/偏析", "level": "High", "reason": "熔炼温度高，低熔点元素易挥发或发生偏析"},
        {"risk": "冷却速率敏感", "level": "High", "reason": "冷却速率影响相组成与缺陷，工艺窗口窄"},
    ],
    "球磨法": [
        {"risk": "杂质引入", "level": "Medium", "reason": "球磨介质磨损可能引入 Fe/W 等杂质"},
        {"risk": "非晶化/团聚", "level": "Low", "reason": "长时间高能球磨可能导致团聚"},
    ],
    "溶液法": [
        {"risk": "前驱体残留", "level": "Medium", "reason": "洗涤不充分导致有机/离子残留"},
        {"risk": "形貌/粒径不均", "level": "Medium", "reason": "成核与生长速率受 pH/温度敏感影响"},
    ],
    "薄膜法": [
        {"risk": "成分偏离", "level": "High", "reason": "薄膜沉积时各元素蒸气压差异导致成分偏离"},
        {"risk": "设备成本高", "level": "High", "reason": "CVD/PVD 设备与维护成本高"},
    ],
    "电化学法": [
        {"risk": "副反应", "level": "Medium", "reason": "电沉积过程伴随析氢等副反应"},
    ],
    "纳米合成": [
        {"risk": "放大困难", "level": "High", "reason": "纳米颗粒合成放大后粒径/形貌控制退化"},
    ],
}


def compare_routes(syns: list[MaterialSynthesis]) -> dict[str, Any]:
    """合成路线对比矩阵：按工艺类别聚合，输出对比表 + 推荐排序。

    Returns:
        {"routes": [{method, method_category, ...}], "recommended": [method_category...]}
    """
    # 按工艺类别聚合
    by_cat: dict[str, list[MaterialSynthesis]] = {}
    for s in syns:
        cat = classify_method(s.method)["category"]
        by_cat.setdefault(cat, []).append(s)

    routes: list[dict[str, Any]] = []
    for cat, items in by_cat.items():
        profile = ROUTE_PROFILES.get(cat, ROUTE_PROFILES["未指定"])
        risks = METHOD_RISK_LIBRARY.get(cat, [])
        lvl, npapers = _evidence_level([], items)
        # 可复现性取最大值
        rep_scores = [s.reproducibility_score for s in items if s.reproducibility_score is not None]
        rep = max(rep_scores) if rep_scores else None
        # 推荐度：证据强度 + 可复现性 + 相纯度 + 粒径控制
        rec = _EVIDENCE_WEIGHT.get(lvl, 0.2) * 4.0
        if profile["phase_purity"] == "高":
            rec += 2.0
        if profile["particle_control"] == "高":
            rec += 2.0
        if profile["scale"] == "易放大":
            rec += 1.5
        if rep is not None:
            rec += (rep / 100) * 1.5
        rec = min(10.0, rec)
        routes.append({
            "method": cat,
            "method_category": cat,
            "temperature": profile["temperature"],
            "time": "; ".join(sorted({s.duration for s in items if s.duration})) or "—",
            "equipment": profile["equipment"],
            "cost": profile["cost"],
            "phase_purity": profile["phase_purity"],
            "particle_control": profile["particle_control"],
            "scale_difficulty": profile["scale"],
            "recommendation_score": round(rec, 1),
            "advantages": _route_advantages(cat),
            "risks": risks,
            "reproducibility_score": rep,
            "evidence_level": lvl,
            "evidence": sorted({s.paper_title for s in items if s.paper_title})[:5],
        })

    routes.sort(key=lambda r: r["recommendation_score"], reverse=True)
    recommended = [r["method"] for r in routes if r["recommendation_score"] >= 6.0]
    return {"routes": routes, "recommended": recommended}


def _route_advantages(cat: str) -> list[str]:
    """路线优势（规则模板）。"""
    profile = ROUTE_PROFILES.get(cat, {})
    adv = []
    if profile.get("phase_purity") == "高":
        adv.append("相纯度高")
    if profile.get("particle_control") == "高":
        adv.append("粒径/形貌可控")
    if profile.get("cost") == "低":
        adv.append("成本低")
    if profile.get("scale") == "易放大":
        adv.append("易放大生产")
    if profile.get("temperature", "").startswith("低温"):
        adv.append("低温节能")
    return adv or ["—"]


# ========================================================================
# 5.5 分步实验流程生成（从「方法名称」升级为「实验流程」）
# ========================================================================

# 工艺类别 → 标准流程骨架（每步含 operation + 参数来源字段 + is_literal 标记）
# 只有「文献明确给出」的参数才填入 parameter；其余步骤标注 is_literal=False（AI 归纳通用步骤）。
_WORKFLOW_SKELETON: dict[str, list[dict[str, Any]]] = {
    "固相法": [
        {"operation": "前驱体称量", "param_field": "precursors", "note": "按化学计量比称取前驱体粉末"},
        {"operation": "混合研磨", "param_field": "stirring", "note": "研钵/球磨混合均匀"},
        {"operation": "压制成型", "param_field": "", "note": "压片（若文献提到）"},
        {"operation": "高温烧结", "param_field": "temperature", "note": "管式炉/箱式炉中烧结"},
        {"operation": "保温", "param_field": "duration", "note": "保温使反应充分"},
        {"operation": "冷却", "param_field": "cooling_method", "note": "随炉/淬火冷却"},
        {"operation": "后处理", "param_field": "post_treatment", "note": "研磨/再次烧结/退火"},
    ],
    "溶液法": [
        {"operation": "前驱体称量", "param_field": "precursors", "note": "称取前驱体"},
        {"operation": "溶液配置", "param_field": "solvent", "note": "溶于溶剂"},
        {"operation": "搅拌", "param_field": "stirring", "note": "搅拌混合均匀"},
        {"operation": "调节 pH", "param_field": "ph", "note": "调节 pH（若文献提到）"},
        {"operation": "转移至反应釜", "param_field": "", "note": "转入水热/溶剂热反应釜"},
        {"operation": "水热/溶剂热处理", "param_field": "temperature", "note": "高温高压反应"},
        {"operation": "保温", "param_field": "duration", "note": "保温"},
        {"operation": "冷却", "param_field": "cooling_method", "note": "自然冷却/淬火"},
        {"operation": "离心/洗涤", "param_field": "", "note": "分离并洗涤产物"},
        {"operation": "干燥", "param_field": "drying_temperature", "note": "干燥"},
        {"operation": "退火", "param_field": "calcination_temperature", "note": "退火（若文献提到）"},
    ],
    "烧结法": [
        {"operation": "前驱体称量", "param_field": "precursors", "note": "称取前驱体粉末"},
        {"operation": "球磨混合", "param_field": "stirring", "note": "机械球磨混合"},
        {"operation": "装模", "param_field": "", "note": "装入石墨模具"},
        {"operation": "SPS/热压烧结", "param_field": "temperature", "note": "放电等离子烧结/热压"},
        {"operation": "保压保温", "param_field": "duration", "note": "保压保温"},
        {"operation": "冷却脱模", "param_field": "cooling_method", "note": "冷却后脱模"},
    ],
    "熔融法": [
        {"operation": "前驱体称量", "param_field": "precursors", "note": "按计量比称取"},
        {"operation": "熔炼", "param_field": "temperature", "note": "电弧/感应熔炼"},
        {"operation": "保温", "param_field": "duration", "note": "保温均化"},
        {"operation": "急冷/淬火", "param_field": "cooling_method", "note": "急冷或浇铸"},
        {"operation": "退火", "param_field": "calcination_temperature", "note": "退火消除应力/均化"},
    ],
    "球磨法": [
        {"operation": "前驱体称量", "param_field": "precursors", "note": "称取前驱体"},
        {"operation": "高能球磨", "param_field": "duration", "note": "高能球磨（机械合金化）"},
        {"operation": "出料", "param_field": "", "note": "取出粉末"},
        {"operation": "退火", "param_field": "calcination_temperature", "note": "退火（若文献提到）"},
    ],
}


def build_workflow_steps(syn: MaterialSynthesis) -> list[dict[str, Any]]:
    """由合成参数生成分步实验流程。

    每一步含：step 序号 / operation 操作 / parameter 参数 / unit 单位 /
    source 来源（文献原文或"AI 归纳"）/ is_literal 是否为文献明确数据。

    硬约束：仅当对应参数字段非空时才标注 is_literal=True 并填 parameter；
    否则 operation 为通用步骤（is_literal=False，来源"AI 归纳"），绝不编造参数。
    """
    cat = classify_method(syn.method)["category"]
    skeleton = _WORKFLOW_SKELETON.get(cat)
    # 已提供 workflow_steps（抽取时结构化好的）则直接返回
    if syn.workflow_steps:
        return syn.workflow_steps
    if not skeleton:
        return []

    steps: list[dict[str, Any]] = []
    for i, sk in enumerate(skeleton, 1):
        field = sk["param_field"]
        param_value = getattr(syn, field, "") if field else ""
        if field and param_value:
            # 文献明确给出的参数
            steps.append({
                "step": i, "operation": sk["operation"],
                "parameter": param_value, "unit": "",
                "source": syn.paper_title or "文献",
                "is_literal": True,
                "note": sk["note"],
            })
        else:
            # 通用步骤（AI 归纳，非文献原文数据）
            steps.append({
                "step": i, "operation": sk["operation"],
                "parameter": "", "unit": "",
                "source": "AI 归纳",
                "is_literal": False,
                "note": sk["note"],
            })
    return steps


# 工艺类别 → 满足的目标画像（路线选择器打分依据）
_ROUTE_GOAL_FIT: dict[str, dict[str, float]] = {
    # 目标关键词 → 各工艺类别的适配分（0-10，领域经验规则）
    "高纯度": {"固相法": 9, "烧结法": 9, "熔融法": 9, "球磨法": 6, "溶液法": 7,
               "薄膜法": 8, "电化学法": 6, "纳米合成": 6},
    "小粒径": {"固相法": 4, "烧结法": 5, "熔融法": 3, "球磨法": 8, "溶液法": 9,
               "薄膜法": 8, "电化学法": 7, "纳米合成": 9},
    "缺陷丰富": {"固相法": 5, "烧结法": 6, "熔融法": 8, "球磨法": 8, "溶液法": 7,
                 "薄膜法": 7, "电化学法": 7, "纳米合成": 8},
    "形貌可控": {"固相法": 4, "烧结法": 4, "熔融法": 3, "球磨法": 6, "溶液法": 9,
                 "薄膜法": 9, "电化学法": 8, "纳米合成": 9},
    "低成本": {"固相法": 9, "烧结法": 6, "熔融法": 7, "球磨法": 9, "溶液法": 7,
               "薄膜法": 3, "电化学法": 6, "纳米合成": 4},
    "易放大": {"固相法": 9, "烧结法": 8, "熔融法": 7, "球磨法": 9, "溶液法": 6,
               "薄膜法": 3, "电化学法": 6, "纳米合成": 4},
}


def recommend_routes(routes: list[dict[str, Any]], goals: list[str]) -> dict[str, Any]:
    """合成路线选择器：根据用户目标（高纯度/小粒径/缺陷丰富/…）推荐方法。

    对每条路线按目标适配度加权打分，输出排序 + 优势/风险说明。
    目标缺失时回退到 compare_routes 的推荐度。
    """
    goals = [g for g in goals if g in _ROUTE_GOAL_FIT]
    if not goals or not routes:
        return {"ranking": [], "note": "未提供明确目标或无可比路线"}

    scored = []
    for r in routes:
        cat = r.get("method", "")
        score = 0.0
        reasons = []
        for g in goals:
            fit = _ROUTE_GOAL_FIT[g].get(cat, 5.0)
            score += fit
            reasons.append(f"「{g}」适配度 {fit:.0f}/10")
        avg = score / len(goals)
        scored.append({
            "method": cat, "score": round(avg, 1),
            "advantages": r.get("advantages", []),
            "risks": r.get("risks", []),
            "goal_reasons": reasons,
            "recommendation_score": r.get("recommendation_score", 0),
        })
    scored.sort(key=lambda x: (x["score"], x["recommendation_score"]), reverse=True)
    return {"ranking": scored, "goals": goals}


# ========================================================================
# 5.6 工艺 → 结构 → 性质 → 性能 链路（P5：性质-合成联合分析）
# ========================================================================

# 工艺参数 → 结构变化 → 性质变化 → 目标性能 的因果链库（领域物理规律，非实验数据）
# 每条含 method_categories：该链路适用于哪些工艺类别（避免"球磨时间"误用到固相法）
PROCESS_PROPERTY_LINKS: list[dict[str, Any]] = [
    {
        "process": "退火温度", "process_key": "calcination_temperature",
        "method_categories": ["固相法", "烧结法", "熔融法", "球磨法", "溶液法", "薄膜法"],
        "direction": "↑",
        "structure_effect": "晶粒长大、晶界减少",
        "property_effect": "载流子迁移率↑ → 电导率↑",
        "target_effect": "利于功率因子，但晶格热导率可能↑",
        "target": "ZT",
    },
    {
        "process": "快速冷却/淬火", "process_key": "cooling_method",
        "method_categories": ["固相法", "烧结法", "熔融法", "溶液法"],
        "direction": "快冷",
        "structure_effect": "缺陷浓度↑、晶格应变↑",
        "property_effect": "声子散射↑ → 晶格热导率↓",
        "target_effect": "利于 ZT 提升（但可能牺牲电导率）",
        "target": "ZT",
    },
    {
        "process": "掺杂浓度", "process_key": "doping",
        "method_categories": ["固相法", "烧结法", "熔融法", "球磨法", "溶液法", "薄膜法"],
        "direction": "↑",
        "structure_effect": "载流子浓度改变、引入点缺陷",
        "property_effect": "电导率↑、Seebeck↓（存在最优值）",
        "target_effect": "调控功率因子峰值位置",
        "target": "ZT",
    },
    {
        "process": "烧结温度", "process_key": "temperature",
        "method_categories": ["固相法", "烧结法"],
        "direction": "↑",
        "structure_effect": "致密度↑、晶粒长大",
        "property_effect": "电导率↑，但热导率可能↑",
        "target_effect": "权衡致密度与晶粒尺寸",
        "target": "ZT",
    },
    {
        "process": "球磨时间", "process_key": "duration",
        "method_categories": ["球磨法"],
        "direction": "↑",
        "structure_effect": "粒径↓、晶界/缺陷↑",
        "property_effect": "声子散射↑ → 晶格热导率↓",
        "target_effect": "利于降低热导率，但可能引入杂质",
        "target": "ZT",
    },
]


def build_process_property_links(syns: list[MaterialSynthesis], target: str = "ZT") -> list[dict[str, Any]]:
    """工艺 → 结构 → 性质 → 性能 链路（基于文献中出现的合成参数）。

    仅对「文献实际给出的参数」且「工艺类别匹配」的链路生成；不编造。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in syns:
        cat = classify_method(s.method)["category"]
        for link in PROCESS_PROPERTY_LINKS:
            if cat not in link["method_categories"]:
                continue
            key = link["process_key"]
            has_param = False
            if key == "doping":
                md = s.metadata or {}
                has_param = bool(md.get("doping") or md.get("doping_concentration"))
            else:
                has_param = bool(getattr(s, key, ""))
            if not has_param:
                continue
            if link["target"] != target:
                continue
            if link["process"] in seen:
                continue
            seen.add(link["process"])
            out.append({
                "process": link["process"], "direction": link["direction"],
                "structure_effect": link["structure_effect"],
                "property_effect": link["property_effect"],
                "target_effect": link["target_effect"],
                "evidence": [s.paper_title] if s.paper_title else [],
                "is_literal": True,  # 参数来自文献，链路是领域规则
            })
    return out


def build_joint_analysis(
    material: Material,
    props: list[MaterialProperty],
    syns: list[MaterialSynthesis],
    target: str = "ZT",
) -> dict[str, Any]:
    """性质-合成联合分析：回答「想获得目标性质，该选什么材料 + 什么合成条件」。

    返回：
    - goal：研究目标
    - target_property：目标性质
    - recommended_materials：候选材料（复用 rank_candidates）
    - process_property_links：工艺→结构→性质→性能链路
    - synthesis_recommendation：合成路线选择器结果
    - risks：主要风险（路线风险汇总）
    - evidence：文献证据
    """
    links = build_process_property_links(syns, target)
    routes = compare_routes(syns)
    # 默认目标画像（可由材料体系推导，当前用通用目标）
    goals = ["高纯度", "小粒径"] if material.material_type in ("热电", "thermoelectric") else ["高纯度"]
    route_reco = recommend_routes(routes["routes"], goals)
    # 汇总风险
    all_risks: list[dict[str, str]] = []
    for s in syns:
        for r in analyze_risks(s):
            if not any(x["risk"] == r["risk"] for x in all_risks):
                all_risks.append(r)

    evidence = sorted({s.paper_title for s in syns if s.paper_title} |
                      {p.paper_title for p in props if p.paper_title})

    return {
        "goal": f"优化目标性能 {target}",
        "target_property": target,
        "process_property_links": links,
        "route_recommendation": route_reco,
        "risks": all_risks,
        "evidence": evidence,
    }


def score_reproducibility(syn: MaterialSynthesis) -> dict[str, Any]:
    """合成路线可复现性评分（0~100）。

    评分因素：参数完整度 / 前驱体信息 / 设备信息 / 关键参数明确度 /
    独立文献支持 / 结果一致性。所有因素由已有字段推导，不编造。
    """
    # 参数完整度：关键字段填写比例
    key_fields = ["temperature", "pressure", "atmosphere", "duration"]
    filled = sum(1 for f in key_fields if getattr(syn, f, ""))
    param_completeness = int(filled / len(key_fields) * 100)

    precursor_completeness = 100 if syn.precursors else 0
    equipment_completeness = 100 if syn.equipment else 0

    # 关键参数是否明确（温度/时间/气氛至少一项）
    key_param_clarity = 100 if (syn.temperature or syn.duration or syn.atmosphere) else 0

    # 独立文献支持（由 evidence_count 推导）
    independent_sources = min(100, (syn.evidence_count or 0) * 20)

    # 结果一致性（无对比数据时取中性 50，不编造）
    result_consistency = 50

    # 加权（参数完整度权重最高）
    score = int(
        param_completeness * 0.30 +
        precursor_completeness * 0.20 +
        equipment_completeness * 0.10 +
        key_param_clarity * 0.20 +
        independent_sources * 0.10 +
        result_consistency * 0.10
    )
    factors = {
        "param_completeness": param_completeness,
        "precursor_completeness": precursor_completeness,
        "equipment_completeness": equipment_completeness,
        "key_param_clarity": key_param_clarity,
        "independent_sources": independent_sources,
        "result_consistency": result_consistency,
    }
    return {"score": score, "factors": factors, "paper_count": syn.evidence_count or 0}


def analyze_risks(syn: MaterialSynthesis) -> list[dict[str, str]]:
    """合成路线风险分析：方法类别风险库 + 参数推断风险。

    风险来源明确标注：方法风险为「领域规则」，参数风险为「参数推断」。
    """
    cat = classify_method(syn.method)["category"]
    risks = [dict(r) for r in METHOD_RISK_LIBRARY.get(cat, [])]
    for r in risks:
        r["source"] = "领域规则"
        r["evidence"] = ""
    # 参数推断风险
    temp = (syn.temperature or "").lower()
    if any(ch in temp for ch in (">800", "900", "1000", "1200")) or "高温" in temp:
        risks.append({"risk": "高温工艺元素挥发风险", "level": "Medium",
                      "source": "参数推断", "reason": "温度条件较高，含易挥发元素时需关注成分偏离",
                      "evidence": syn.source_snippet[:100]})
    return risks


def sensitivity_analysis(syns: list[MaterialSynthesis]) -> dict[str, Any]:
    """合成参数敏感性分析（规则：按字段被多篇文献反复强调的参数视为高影响）。

    数据不足时明确标注，不编造敏感性结论。
    """
    if not syns:
        return {"high_impact": [], "low_impact": [], "evidence": [], "note": "暂无足够合成数据"}
    # 统计各参数在多篇文献中的出现频率（出现频率高 → 工艺关键）
    param_fields = ["temperature", "duration", "pressure", "atmosphere", "doping",
                    "calcination_temperature", "cooling_method", "precursor_ratio", "ph"]
    freq: dict[str, int] = {}
    for s in syns:
        for f in ("temperature", "duration", "pressure", "atmosphere", "calcination_temperature", "cooling_method"):
            if getattr(s, f, ""):
                freq[f] = freq.get(f, 0) + 1
        md = s.metadata or {}
        if md.get("doping") or md.get("doping_concentration"):
            freq["doping"] = freq.get("doping", 0) + 1
        if s.precursor_ratio:
            freq["precursor_ratio"] = freq.get("precursor_ratio", 0) + 1
        if s.ph:
            freq["ph"] = freq.get("ph", 0) + 1

    n = len(syns)
    high = []
    low = []
    cn = {"temperature": "温度", "duration": "时间", "pressure": "压力", "atmosphere": "气氛",
          "calcination_temperature": "退火/煅烧温度", "cooling_method": "冷却方式",
          "doping": "掺杂浓度", "precursor_ratio": "前驱体比例", "ph": "pH"}
    for f, c in freq.items():
        ratio = c / n
        entry = {"parameter": cn.get(f, f), "field": f,
                 "reason": f"{c}/{n} 篇文献均明确给出该参数",
                 "evidence": sorted({s.paper_title for s in syns if getattr(s, f, "") or (f in ('doping','precursor_ratio','ph'))} )[:3]}
        if ratio >= 0.5:
            high.append(entry)
        else:
            low.append(entry)
    high.sort(key=lambda e: -freq.get(e.get("field", ""), 0))
    return {"high_impact": high, "low_impact": low,
            "evidence": sorted({s.paper_title for s in syns if s.paper_title}),
            "note": "" if (high or low) else "参数出现频率不足以判断敏感性"}


# ========================================================================
# 6. 性质 → 机制 → 目标性能（build_mechanisms）
# ========================================================================

def build_mechanisms(props: list[MaterialProperty], target: str = "ZT") -> list[dict[str, Any]]:
    """为每个关键性质生成「性质 → 机制 → 目标性能」关系解释。

    机制来自 MECHANISM_LIBRARY（物理规律），数值来自输入，证据来自 paper_title。
    无机制库条目时给出保守说明，不编造。
    """
    out: list[dict[str, Any]] = []
    for p in props:
        norm = normalize_property(p.property_name, p.property_name_cn)
        key = norm["key"]
        rule = MECHANISM_LIBRARY.get(key)
        if not rule:
            continue
        out.append({
            "property": key,
            "property_cn": norm["cn"] or p.property_name,
            "value": p.value or (f"{p.value_num}" if p.value_num is not None else ""),
            "unit": p.unit,
            "mechanism": rule["mechanism"],
            "impact_on_target": rule["impact"],
            "evidence_level": p.evidence_level or "E",
            "evidence": [p.paper_title] if p.paper_title else [],
        })
    return out


def decompose_target(props: list[MaterialProperty], target: str = "ZT") -> dict[str, Any]:
    """目标性能因果拆解：ZT = S²σT/κ → 因素分解 + 优势/瓶颈/优化变量。"""
    tinfo = TARGET_LIBRARY.get(target)
    if not tinfo:
        return {"target": target, "formula": "", "factors": [], "strengths": [],
                "bottlenecks": [], "optimization_priority": [], "evidence": []}
    # 找各因素的实际值
    prop_by_key: dict[str, MaterialProperty] = {}
    for p in props:
        key = normalize_property(p.property_name, p.property_name_cn)["key"]
        if key not in prop_by_key:
            prop_by_key[key] = p
    factors = []
    for f in tinfo["factors"]:
        p = prop_by_key.get(f["factor"])
        factors.append({
            "factor": f["factor"], "factor_cn": f["factor_cn"], "role": f["role"],
            "value": p.value if p else "", "unit": p.unit if p else "",
            "has_data": p is not None,
        })
    # 优势 / 瓶颈（由规则 + 数据推导）
    strengths, bottlenecks = [], []
    if target == "ZT":
        if prop_by_key.get("seebeck_coefficient"):
            strengths.append("已测得 Seebeck 系数，可评估电输运能力")
        if prop_by_key.get("lattice_thermal_conductivity") or prop_by_key.get("thermal_conductivity"):
            strengths.append("有热导率数据，可评估声子输运")
        if not prop_by_key.get("electrical_conductivity"):
            bottlenecks.append("电导率数据缺失，无法完整评估功率因子")
        if not prop_by_key.get("thermal_conductivity") and not prop_by_key.get("lattice_thermal_conductivity"):
            bottlenecks.append("热导率数据缺失，无法评估 ZT 分母项")
    elif target == "power_factor":
        if prop_by_key.get("seebeck_coefficient") and prop_by_key.get("electrical_conductivity"):
            strengths.append("S 与 σ 数据齐备，功率因子可完整评估")
        else:
            bottlenecks.append("S 或 σ 数据缺失，功率因子评估不完整")
    # 优化变量优先级（热电领域规则）
    priority = []
    if target in ("ZT", "power_factor"):
        priority = [
            {"priority": 1, "variable": "载流子浓度（掺杂）", "reason": "直接调控 S 与 σ 的平衡，决定功率因子峰值"},
            {"priority": 2, "variable": "掺杂元素/浓度", "reason": "优化费米能级位置，兼顾 Seebeck 与电导率"},
            {"priority": 3, "variable": "晶格缺陷/纳米结构", "reason": "增强声子散射降低晶格热导率 κ_L"},
        ]
    evidence = sorted({p.paper_title for p in props if p.paper_title})
    return {"target": target, "formula": tinfo["formula"], "factors": factors,
            "strengths": strengths, "bottlenecks": bottlenecks,
            "optimization_priority": priority, "evidence": evidence}


# ========================================================================
# 7. 材料画像聚合（build_profile）
# ========================================================================

def build_profile(
    material: Material,
    props: list[MaterialProperty],
    syns: list[MaterialSynthesis],
    all_materials: Optional[list[Material]] = None,
    props_by_mat: Optional[dict[str, list[MaterialProperty]]] = None,
    syns_by_mat: Optional[dict[str, list[MaterialSynthesis]]] = None,
    target: str = "ZT",
) -> dict[str, Any]:
    """聚合单个材料的完整深度分析画像。

    供 API /materials/{id}/profile 返回。结构对齐 MaterialProfile 模型。
    """
    # 基础结构
    structure = {
        "formula": material.formula, "material_type": material.material_type,
        "crystal_structure": material.crystal_structure, "crystal_system": material.crystal_system,
        "space_group": material.space_group, "lattice_parameters": material.lattice_parameters,
        "symmetry": material.symmetry, "morphology": material.morphology,
        "phase_composition": material.phase_composition, "is_multiphase": material.is_multiphase,
        "element_composition": material.element_composition, "element_ratio": material.element_ratio,
        "composition": material.composition,
    }
    # 性质分组（按六大维度）
    grouped: dict[str, list[dict[str, Any]]] = {}
    for p in props:
        norm = normalize_property(p.property_name, p.property_name_cn)
        dim = norm.get("dimension", "other")
        grouped.setdefault(dim, []).append({
            "property_name": p.property_name, "property_name_cn": norm["cn"] or p.property_name_cn,
            "norm_key": norm["key"], "symbol": norm["symbol"], "unit": p.unit or norm["unit"],
            "category": norm["category"], "value": p.value, "value_num": p.value_num,
            "condition": p.condition, "mechanism": p.mechanism, "impact_on_target": p.impact_on_target,
            "evidence_level": p.evidence_level or "E",
            "evidence_label": EVIDENCE_LEVEL_LABELS.get(p.evidence_level or "E", p.evidence_level or "E"),
            "evidence_count": p.evidence_count, "data_type": p.data_type,
            "test_temperature": p.test_temperature, "source_type": p.source_type,
            "paper_title": p.paper_title, "confidence": p.confidence,
        })

    # 机制关系
    mechanisms = build_mechanisms(props, target)
    # 目标性能因果拆解
    target_decomposition = decompose_target(props, target)
    # 对比（若提供全体材料）
    comparison: dict[str, Any] = {}
    if all_materials and props_by_mat is not None:
        comparison = build_comparison(all_materials, props_by_mat)
    # 候选排序
    ranking: list[dict[str, Any]] = []
    if all_materials and props_by_mat is not None and syns_by_mat is not None:
        ranking = rank_candidates(all_materials, props_by_mat, syns_by_mat, target)
    # 合成路线
    routes_result = compare_routes(syns)
    synthesis = {
        "routes": routes_result,
        "route_recommendation": recommend_routes(routes_result["routes"], ["高纯度", "小粒径", "缺陷丰富"]),
        "workflows": [
            {
                "method": s.method, "method_category": classify_method(s.method)["category"],
                "workflow_steps": build_workflow_steps(s) or s.workflow_steps,
                "steps": s.steps,
                "reproducibility": score_reproducibility(s),
                "risks": analyze_risks(s),
                "evidence_level": s.evidence_level or "E",
                "evidence_count": s.evidence_count,
                "paper_title": s.paper_title,
            }
            for s in syns
        ],
        "sensitivity": sensitivity_analysis(syns),
    }
    # 性质-合成联合分析（工艺→结构→性质→性能链路 + 路线推荐 + 风险）
    joint_analysis = build_joint_analysis(material, props, syns, target)

    return {
        "material_id": material.material_id, "name": material.name,
        "formula": material.formula, "category": "",
        "structure": structure, "properties": grouped, "mechanisms": mechanisms,
        "target_decomposition": target_decomposition, "comparison": comparison,
        "ranking": ranking, "synthesis": synthesis, "joint_analysis": joint_analysis,
    }


# ========================================================================
# 8. 证据等级回填（抽取后统一标注，不编造）
# ========================================================================

def annotate_evidence_levels(
    props: list[MaterialProperty], syns: list[MaterialSynthesis]
) -> tuple[list[MaterialProperty], list[MaterialSynthesis]]:
    """为性质/合成回填证据等级（若抽取时未填）。

    纯规则推导：按 data_type / source_type / 文献数。不改变已有值。
    """
    for p in props:
        if not p.evidence_level or p.evidence_level == "E":
            n = 1 if p.paper_id else 0
            p.evidence_level = _derive_evidence_level(n, p.data_type, p.source_type)
            p.evidence_count = p.evidence_count or n
    for s in syns:
        if not s.evidence_level or s.evidence_level == "E":
            n = 1 if s.paper_id else 0
            s.evidence_level = _derive_evidence_level(n, "", s.source_type if hasattr(s, "source_type") else "")
            s.evidence_count = s.evidence_count or n
    return props, syns
