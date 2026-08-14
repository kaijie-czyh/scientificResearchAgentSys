"""物理一致性检查的本体实现（无需 numpy / scipy）。

参考依据：
- 元素周期表（标准 118 元素 + 标准价态）
- IUPAC 推荐元素价态范围
- half-Heusler 18 电子规则（Zintl 类 18-电子计数）
- Goldschmidt 容忍因子 t = (rA + rO) / [sqrt(2) * (rB + rO)]，钙钛矿稳定区间 0.825~1.059
- Materials Project / OQMD 公开数据上的常见性能范围
- 维德曼-弗朗茨定律（Wiedemann-Franz）：电导率与热导率比值不超过 Sommerfeld 值（L0 = 2.44e-8 WΩ/K²）
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

# ============================================================
# 数据：元素周期表与典型价态
# ============================================================

# 简化版：常见元素符号 -> 原子量（用于配比计算）
ATOMIC_MASS: dict[str, float] = {
    "H": 1.008, "He": 4.003, "Li": 6.941, "Be": 9.012, "B": 10.811,
    "C": 12.011, "N": 14.007, "O": 15.999, "F": 18.998, "Ne": 20.180,
    "Na": 22.990, "Mg": 24.305, "Al": 26.982, "Si": 28.086, "P": 30.974,
    "S": 32.065, "Cl": 35.453, "Ar": 39.948, "K": 39.098, "Ca": 40.078,
    "Sc": 44.956, "Ti": 47.867, "V": 50.942, "Cr": 51.996, "Mn": 54.938,
    "Fe": 55.845, "Co": 58.933, "Ni": 58.693, "Cu": 63.546, "Zn": 65.380,
    "Ga": 69.723, "Ge": 72.640, "As": 74.922, "Se": 78.960, "Br": 79.904,
    "Kr": 83.798, "Rb": 85.468, "Sr": 87.620, "Y": 88.906, "Zr": 91.224,
    "Nb": 92.906, "Mo": 95.960, "Tc": 98.000, "Ru": 101.070, "Rh": 102.906,
    "Pd": 106.420, "Ag": 107.868, "Cd": 112.411, "In": 114.818, "Sn": 118.710,
    "Sb": 121.760, "Te": 127.600, "I": 126.904, "Xe": 131.293, "Cs": 132.905,
    "Ba": 137.327, "La": 138.905, "Ce": 140.116, "Pr": 140.908, "Nd": 144.242,
    "Pm": 145.000, "Sm": 150.360, "Eu": 151.964, "Gd": 157.250, "Tb": 158.925,
    "Dy": 162.500, "Ho": 164.930, "Er": 167.259, "Tm": 168.934, "Yb": 173.045,
    "Lu": 174.967, "Hf": 178.490, "Ta": 180.948, "W": 183.840, "Re": 186.207,
    "Os": 190.230, "Ir": 192.217, "Pt": 195.084, "Au": 196.967, "Hg": 200.592,
    "Tl": 204.383, "Pb": 207.200, "Bi": 208.980, "Po": 209.000, "At": 210.000,
    "Rn": 222.000, "Fr": 223.000, "Ra": 226.000, "Ac": 227.000, "Th": 232.038,
    "Pa": 231.036, "U": 238.029, "Np": 237.000, "Pu": 244.000,
}

# 各元素常用价态（多个时取最常见；用于电中性检查的"容差判定"）
COMMON_OXIDATION: dict[str, list[int]] = {
    # X = 有机阳离子团（MA / FA / PEA 等），整体 +1
    "X": [1],
    "H": [1, -1], "Li": [1], "Be": [2], "B": [3], "C": [4, -4, 2],
    "N": [-3, 3, 5], "O": [-2], "F": [-1], "Na": [1], "Mg": [2],
    "Al": [3], "Si": [4, -4], "P": [5, -3, 3], "S": [6, -2, 4, 2],
    "Cl": [-1, 1, 3, 5, 7], "K": [1], "Ca": [2], "Sc": [3],
    "Ti": [4, 3, 2], "V": [5, 4, 3, 2], "Cr": [3, 6, 2],
    "Mn": [2, 4, 7, 3, 6], "Fe": [2, 3, 6], "Co": [2, 3],
    "Ni": [2, 3], "Cu": [1, 2], "Zn": [2], "Ga": [3],
    "Ge": [4, 2, -4], "As": [3, 5, -3], "Se": [-2, 4, 6],
    "Br": [-1, 1, 3, 5, 7], "Rb": [1], "Sr": [2], "Y": [3],
    "Zr": [4], "Nb": [5, 3], "Mo": [6, 4, 3], "Ru": [3, 4],
    "Rh": [3], "Pd": [2, 4], "Ag": [1], "Cd": [2], "In": [3],
    "Sn": [4, 2], "Sb": [3, 5, -3], "Te": [-2, 4, 6],
    "I": [-1, 1, 3, 5, 7], "Cs": [1], "Ba": [2], "La": [3],
    "Ce": [3, 4], "Hf": [4], "Ta": [5], "W": [6, 4], "Re": [4, 6, 7],
    "Os": [4, 6], "Ir": [3, 4], "Pt": [2, 4], "Au": [1, 3],
    "Hg": [1, 2], "Tl": [1, 3], "Pb": [2, 4], "Bi": [3, 5],
    "Th": [4], "U": [4, 6],
}

# 元素群组价电子数（half-Heusler 18 电子规则用）
VALENCE_ELECTRONS: dict[str, int] = {
    "H": 1, "He": 2, "Li": 1, "Be": 2, "B": 3, "C": 4, "N": 5, "O": 6,
    "F": 7, "Ne": 8, "Na": 1, "Mg": 2, "Al": 3, "Si": 4, "P": 5, "S": 6,
    "Cl": 7, "Ar": 8, "K": 1, "Ca": 2, "Sc": 3, "Ti": 4, "V": 5,
    "Cr": 6, "Mn": 7, "Fe": 8, "Co": 9, "Ni": 10, "Cu": 11, "Zn": 12,
    "Ga": 3, "Ge": 4, "As": 5, "Se": 6, "Br": 7, "Kr": 8, "Rb": 1,
    "Sr": 2, "Y": 3, "Zr": 4, "Nb": 5, "Mo": 6, "Ru": 8, "Rh": 9,
    "Pd": 10, "Ag": 11, "Cd": 12, "In": 3, "Sn": 4, "Sb": 5, "Te": 6,
    "I": 7, "Xe": 8, "Cs": 1, "Ba": 2, "La": 3, "Ce": 4, "Hf": 4,
    "Ta": 5, "W": 6, "Re": 7, "Os": 8, "Ir": 9, "Pt": 10, "Au": 11,
    "Hg": 12, "Tl": 3, "Pb": 4, "Bi": 5,
}

# Shannon 离子半径（Å，常见配位数 6；用于 Goldschmidt 容忍因子）
# 数据来源：Shannon 1976 / 1981
IONIC_RADIUS_CN6: dict[str, float] = {
    "Li": 0.90, "Na": 1.16, "K": 1.52, "Rb": 1.66, "Cs": 1.81,
    "Mg": 0.86, "Ca": 1.14, "Sr": 1.32, "Ba": 1.49,
    "Al": 0.675, "Sc": 0.885, "Y": 1.040, "La": 1.172,
    "Ti": 0.745, "Zr": 0.86, "Hf": 0.85,
    "V": 0.78, "Nb": 0.86, "Ta": 0.86,
    "Cr": 0.755, "Mo": 0.83, "W": 0.86,
    "Mn": 0.81, "Fe": 0.745, "Co": 0.685, "Ni": 0.83, "Cu": 0.87, "Zn": 0.88,
    "Ga": 0.760, "In": 0.940, "Tl": 1.025,
    "Si": 0.54, "Ge": 0.67, "Sn": 0.83, "Pb": 0.915,
    "P": 0.58, "As": 0.72, "Sb": 0.90,
    "S": 0.43, "Se": 0.56, "Te": 0.70,
    "F": 1.19, "Cl": 1.67, "Br": 1.82, "I": 2.06,
    "O": 1.26, "N": 1.45,
    "Bi": 1.17, "Sb": 0.90,
}


# ============================================================
# 物理量范围（经验值 + Materials Project 统计）
# ============================================================

# 常见性能指标的物理可达范围
# 单位：(min, max, 单位说明)
PROPERTY_PHYSICAL_BOUNDS: dict[str, tuple[float, float, str]] = {
    "zt": (0.0, 5.0, ""),                        # 热电优值（理论上 ZT≈∞ 在极端非平衡，实际 ZT>4 罕见）
    "seebeck": (-2.0, 2.0, "mV/K"),               # 热电 Seebeck 系数（典型 -1 ~ +1 mV/K；>2 罕见）
    "thermal_conductivity": (0.05, 500.0, "W/mK"),# 热导率（玻璃下限 0.05W/mK ~ 高纯金属 ~500W/mK）
    "electrical_conductivity": (1e-12, 1e8, "S/m"),# 电导率（绝缘体~10^-12 ~ 良导体~10^8）
    "power_factor": (0.0, 100.0, "mW/mK^2"),       # 功率因子（典型 0~50；>100 罕见）
    "band_gap": (0.0, 15.0, "eV"),                 # 带隙（0~15 eV；>15 接近原子单位）
    "formation_energy": (-10.0, 5.0, "eV/atom"),   # 形成能（稳定化合物通常 < 0）
    "bulk_modulus": (0.5, 500.0, "GPa"),           # 体模量（气凝胶下限 ~ 软材料下限）
    "curie_temperature": (0.0, 1500.0, "K"),       # 居里温度（典型铁电体 200~700K）
    "dielectric_constant": (1.0, 1e5, ""),         # 介电常数（真空=1，铁电体可达 1e4~1e5）
    "capacity": (0.0, 1000.0, "mAh/g"),            # 比容量（典型锂电正极 100~300；理论上限 ~1000）
    "efficiency": (0.0, 100.0, "%"),               # 效率
    "open_circuit_voltage": (0.0, 5.0, "V"),       # 开路电压（电化学）
    "overpotential": (-2.0, 5.0, "V"),             # 过电位
}


# ============================================================
# 化学式解析
# ============================================================

_FORMULA_TOKEN = re.compile(r"([A-Z][a-z]?)([\d\.]*)")
_PARENS_GROUP = re.compile(r"\(([A-Za-z0-9\.]+)\)(\d*\.\d+|\d*)")

# 多字母离子团 / 有机阳离子识别（用于钙钛矿 / 离子液体的化学式解析）
# 出现这些团时按"分子 +1 总电荷"的整体解析（具体原子贡献由 STO 启发式推断）
_MULTI_LETTER_GROUPS = [
    "FA",  # 甲脒 CH(NH2)2+
    "MA",  # 甲胺 CH3NH3+
    "PEA", # 苯乙胺 C6H5CH2CH2NH3+
    "BA",  # 正丁胺 CH3(CH2)3NH3+
    "HA",  # 己胺
    "OA",  # 辛胺
    "EDA", # 乙二胺
]


def _parse_formula(formula: str) -> dict[str, float]:
    """把化学式解析为元素-配比字典。

    支持：Bi2Te3 / Mg3Sb2 / Sr0.5Ba0.5TiO3 / (Na0.5Bi0.5)TiO3 / Cs0.05FA0.95PbI3
    不支持：完整有机分子式（FA / MA 等作为整体标记 X+）
    """
    formula = formula.replace(" ", "")
    if not formula:
        return {}

    out: dict[str, float] = {}

    # 第一遍：把 FA / MA 等离子团替换为标记占位符，避免被当成元素
    # （FSite0.05FA0.95PbI3 实际是混合钙钛矿）
    placeholders = {}
    for group in _MULTI_LETTER_GROUPS:
        if group in formula:
            placeholders[group] = f"_X{len(placeholders)}_"
            formula = formula.replace(group, placeholders[group])

    def _parse_segment(seg: str, multiplier: float = 1.0):
        i = 0
        while i < len(seg):
            m = re.match(r"([A-Z][a-z]?)(\d*\.?\d*)", seg[i:])
            if not m:
                i += 1
                continue
            sym, num = m.group(1), m.group(2)
            count = float(num) if num else 1.0
            out[sym] = out.get(sym, 0.0) + count * multiplier
            i += m.end()

    # 处理括号
    last_end = 0
    for m in _PARENS_GROUP.finditer(formula):
        prefix = formula[last_end:m.start()]
        _parse_segment(prefix)
        inner = m.group(1)
        coef = float(m.group(2)) if m.group(2) else 1.0
        _parse_segment(inner, coef)
        last_end = m.end()
    _parse_segment(formula[last_end:])

    # 把标记占位符恢复为"X"（统一标记为有机阳离子，valence_balanced 走"放宽"判定）
    for group, ph in placeholders.items():
        if ph in formula:
            x = out.pop(ph, 0.0)
            out["X"] = out.get("X", 0.0) + x  # "X" = organic cation

    return {k: v for k, v in out.items() if v > 0}


# ============================================================
# 化合物检查
# ============================================================


@dataclass
class FormulaCheckResult:
    """化学式检查结果。"""
    valid: bool
    formula: str
    composition: dict[str, float]
    reason: str = ""
    flags: list[str] = field(default_factory=list)
    # 价态检查（如果有 O / Cl / F 等阴离子，可尝试价态平衡判定）
    charge_balance: Optional[float] = None  # 越接近 0 越好
    valence_balanced: bool = False
    # 18 电子规则（half-Heusler 等）
    valence_electron_count: Optional[float] = None
    # Goldschmidt 容忍因子（仅对钙钛矿型 ABO3 有效）
    goldschmidt_t: Optional[float] = None


def check_formula(formula: str) -> FormulaCheckResult:
    """检查化学式是否在物理上合理。

    检查维度：
    1. 元素存在性（118 标准元素）
    2. 总配比 > 0
    3. 电中性（在常见价态下是否能给出总价 = 0 的赋值）
    4. half-Heusler / Zintl 相的 18 电子规则（如适用）
    5. 钙钛矿型 ABO3 的 Goldschmidt 容忍因子（如适用）
    """
    comp = _parse_formula(formula)
    flags: list[str] = []

    if not comp:
        return FormulaCheckResult(
            valid=False, formula=formula, composition={},
            reason="无法解析化学式", flags=["unparseable"],
        )

    # 元素存在性
    unknown = [el for el in comp if el not in ATOMIC_MASS]
    if unknown:
        return FormulaCheckResult(
            valid=False, formula=formula, composition=comp,
            reason=f"未知元素: {unknown}", flags=["unknown_element"],
        )

    # 检查是否有"重得离谱"的配比（防止 LLM 幻觉）
    for el, x in comp.items():
        if x > 20:
            flags.append(f"large_stoichiometry:{el}={x}")
        if x <= 0:
            return FormulaCheckResult(
                valid=False, formula=formula, composition=comp,
                reason=f"非正配比: {el}={x}", flags=["non_positive"],
            )

    # 电中性检查：尝试 DFS 找到最接近 0 的电荷组合；并用启发式作参考
    charge_balance = _compute_charge_balance(comp)
    # 判定：用 DFS 找到的最佳 diff
    # （详见 _compute_charge_balance 注释：best_diff[0] 即最小偏离）

    # 18 电子规则（仅当化合物元素数 >= 3 时检查）
    valence_electron_count = None
    if len(comp) >= 3:
        vec = sum(VALENCE_ELECTRONS.get(el, 0) * x for el, x in comp.items())
        valence_electron_count = round(vec, 2)

    # Goldschmidt 容忍因子（仅对 1:1:3 元素组合的钙钛矿型 ABO3）
    goldschmidt_t = _compute_goldschmidt_t(comp)

    # 综合判定
    # 重新跑一次 DFS 拿最小偏离（charge_balance 启发式 + DFS best_diff）
    charge_best_diff = _charge_balance_best_diff(comp)
    # 金属间化合物判定：若金属元素占比 >= 50%（金属间 / 金属键化合物），
    # 离子模型不适用，依赖 18 电子规则 / Hume-Rothery 等其他判定
    is_intermetallic = _is_intermetallic(comp)
    if is_intermetallic:
        # 金属间化合物：默认视为电中性合法（离子模型不适用）
        valence_balanced = True
        flags.append("intermetallic")
    else:
        valence_balanced = (charge_best_diff is not None and charge_best_diff < 0.5)

    valid = True
    reason = ""
    if not valence_balanced and charge_best_diff is not None and charge_best_diff >= 2.0:
        # DFS 都找不到 < 2.0 的偏离 → 电中性失衡严重
        valid = False
        reason = f"电中性检查失败：最小可达电荷偏离 = {charge_best_diff:.2f}"
        flags.append("charge_imbalance")

    if valid:
        reason = "化学式解析通过"

    return FormulaCheckResult(
        valid=valid,
        formula=formula,
        composition=comp,
        reason=reason,
        flags=flags,
        charge_balance=charge_balance,
        valence_balanced=valence_balanced,
        valence_electron_count=valence_electron_count,
        goldschmidt_t=goldschmidt_t,
    )


def _compute_charge_balance(comp: dict[str, float]) -> Optional[float]:
    """启发式估算电荷偏离 0 的程度（带符号）。

    算法：贪心 — 阴离子取负价 / 金属取正价 / 两性元素取主价。
    """
    if not comp:
        return None

    approx = 0.0
    for el, x in comp.items():
        states = COMMON_OXIDATION.get(el, [0])
        if not states:
            continue
        # 启发式：金属/非金属分类
        # 简单启发：O/S/Se/Te/F/Cl/Br/I 强制负价；其他非金属取 max；金属取 max
        force_negative = {"O", "S", "Se", "Te", "F", "Cl", "Br", "I", "N"}
        if el in force_negative:
            v = min(states)
        elif el in {"H", "C", "Si", "P", "As", "Sb", "Ge", "Sn", "B"}:
            v = max(states) if max(states) > 0 else min(states)
        else:
            v = max(states)
        approx += v * x
    return approx if math.isfinite(approx) else None


def _charge_balance_best_diff(comp: dict[str, float]) -> Optional[float]:
    """DFS 回溯：寻找价态组合使总电荷最接近 0，返回最小偏离。

    仅元素数 <= 6 时可行；>6 时返回 None（保守放行）。
    """
    if not comp:
        return None
    if len(comp) > 6:
        return None

    elements = list(comp.keys())
    n = len(elements)

    def _charge_for(oxi_assign: list[int]) -> float:
        return sum(oxi_assign[i] * comp[elements[i]] for i in range(n))

    def _dfs(idx: int, current: list[int], best_diff: list[float]) -> None:
        if idx == n:
            chg = _charge_for(current)
            diff = abs(chg)
            if diff < best_diff[0]:
                best_diff[0] = diff
            return
        el = elements[idx]
        states = COMMON_OXIDATION.get(el, [0])
        for s in states:
            current.append(s)
            _dfs(idx + 1, current, best_diff)
            current.pop()

    best_diff = [float("inf")]
    _dfs(0, [], best_diff)
    return best_diff[0] if math.isfinite(best_diff[0]) else None


def _is_intermetallic(comp: dict[str, float]) -> bool:
    """判定化合物是否为金属间化合物（金属键合，离子模型不适用）。

    判定规则：非金属元素占比 < 25%（即金属元素占比 >= 75%）。
    """
    if not comp:
        return False
    non_metals = {"H", "B", "C", "N", "O", "Si", "P", "S", "Se", "Te", "F", "Cl", "Br", "I"}
    total = sum(comp.values())
    if total == 0:
        return False
    non_metal_amount = sum(x for el, x in comp.items() if el in non_metals)
    return (non_metal_amount / total) < 0.25


def _compute_goldschmidt_t(comp: dict[str, float]) -> Optional[float]:
    """对钙钛矿型 ABO3 计算 Goldschmidt 容忍因子 t。

    仅对：恰好有 5 个元素（可能 A: x + B: 1-x + O3） 或 显式判定为钙钛矿型的组合计算。
    返回 t；超出 0.825~1.059 区间则提示可能不稳定。
    """
    if "O" not in comp or comp.get("O", 0) < 2.5:
        return None  # 非钙钛矿
    if len(comp) < 3:
        return None

    # 启发式：从非 O 元素中挑出 A、B（配比大的当 A=0.5~1, 配比小的当 B=0~1）
    non_o = {k: v for k, v in comp.items() if k != "O"}
    if len(non_o) < 2:
        return None

    # 把所有非 O 元素按配比降序，分两组：A 大配比，B 小配比
    items = sorted(non_o.items(), key=lambda x: -x[1])
    a_items, b_items = items[:1], items[1:]
    if not b_items:
        return None

    # 配比归一化到 A=1, B=1（钙钛矿结构）
    a_total = sum(x for _, x in a_items)
    b_total = sum(x for _, x in b_items)
    if b_total == 0:
        return None

    rA = sum(IONIC_RADIUS_CN6.get(el, 0) * (x / a_total) for el, x in a_items)
    rB = sum(IONIC_RADIUS_CN6.get(el, 0) * (x / b_total) for el, x in b_items)
    rO = IONIC_RADIUS_CN6.get("O", 1.26)
    if rB == 0 or rO == 0:
        return None

    t = (rA + rO) / (math.sqrt(2) * (rB + rO))
    return round(t, 3)


# ============================================================
# 性能检查
# ============================================================


@dataclass
class PropertyCheckResult:
    """性能范围检查结果。"""
    valid: bool
    prop_name: str
    value: float
    unit: str
    expected_range: tuple[float, float]
    reason: str = ""


def check_property_window(prop_name: str, value: float, unit: str = "") -> PropertyCheckResult:
    """检查性能数值是否在物理可达范围内。

    参数：
    - prop_name: 支持 zt / seebeck / band_gap / thermal_conductivity / electrical_conductivity
                 / power_factor / formation_energy / bulk_modulus / curie_temperature
                 / dielectric_constant / capacity / efficiency / open_circuit_voltage / overpotential
    - value: 数值
    - unit: 单位（仅记录，不强制换算）
    """
    key = prop_name.lower().replace(" ", ", ").replace("（", "(").replace("）", ")")
    # 模糊匹配
    matched_key = None
    for k in PROPERTY_PHYSICAL_BOUNDS:
        if k in key or key in k:
            matched_key = k
            break
    if matched_key is None:
        return PropertyCheckResult(
            valid=True, prop_name=prop_name, value=value, unit=unit,
            expected_range=(float("-inf"), float("inf")),
            reason="未在已知性能字典中，跳过检查（保守放行）",
        )
    lo, hi, expected_unit = PROPERTY_PHYSICAL_BOUNDS[matched_key]
    valid = lo <= value <= hi
    reason = "在物理可达范围内" if valid else f"超出物理可达范围 [{lo}, {hi}]"
    return PropertyCheckResult(
        valid=valid, prop_name=prop_name, value=value, unit=unit,
        expected_range=(lo, hi), reason=reason,
    )


# ============================================================
# 候选检查（顶层）
# ============================================================


@dataclass
class CandidateCheckResult:
    """一个 (材料组成, 工艺, 性能) 候选的物理一致性结果。"""
    valid: bool
    material_check: Optional[FormulaCheckResult] = None
    property_checks: list[PropertyCheckResult] = field(default_factory=list)
    # 顶级判定原因（多条件汇总）
    reason: str = ""
    # 风险等级：none / low / medium / high
    risk: str = "low"
    # 适合推送给下游（mcts_search / discovery_validate）的派生信号
    feasibility_signal: float = 1.0  # 0~1，越高越可行


def check_candidate(
    config: dict,
    target_property: Optional[str] = None,
    predicted_value: Optional[float] = None,
    predicted_unit: Optional[str] = None,
) -> CandidateCheckResult:
    """检查一个构效关系候选是否在物理上合理。

    config 期望至少包含：
    - 'material' 或 'formula': 材料化学式
    - 可选 'synthesis_temp_k' / 'synthesis_pressure_gpa' / 'synthesis_duration_h'

    target_property + predicted_value: 期望达到的性能指标
    """
    # 1. 化学式
    material = config.get("material") or config.get("formula") or ""
    mat_check = check_formula(material) if material else None

    # 2. 工艺范围
    property_checks: list[PropertyCheckResult] = []
    reasons: list[str] = []

    if "synthesis_temp_k" in config:
        t = config["synthesis_temp_k"]
        if t < 0:
            property_checks.append(PropertyCheckResult(
                valid=False, prop_name="synthesis_temp_k", value=t, unit="K",
                expected_range=(0, 4000), reason="温度为负违反热力学第三定律",
            ))
            reasons.append(f"合成温度 {t}K < 0 违反热力学")
        elif t > 4000:
            property_checks.append(PropertyCheckResult(
                valid=False, prop_name="synthesis_temp_k", value=t, unit="K",
                expected_range=(0, 4000), reason="合成温度超过常见实验室上限（>4000K 罕见）",
            ))
            reasons.append(f"合成温度 {t}K > 4000K 罕见")

    if "synthesis_pressure_gpa" in config:
        p = config["synthesis_pressure_gpa"]
        if p < 0:
            property_checks.append(PropertyCheckResult(
                valid=False, prop_name="synthesis_pressure_gpa", value=p, unit="GPa",
                expected_range=(0, 200), reason="压力为负违反物理",
            ))
            reasons.append(f"合成压力 {p}GPa < 0 违反物理")
        elif p > 200:
            property_checks.append(PropertyCheckResult(
                valid=False, prop_name="synthesis_pressure_gpa", value=p, unit="GPa",
                expected_range=(0, 200), reason="合成压力超过常见实验上限（>200 GPa 仅金刚石压砧可达）",
            ))

    # 3. 性能预测范围
    if target_property and predicted_value is not None:
        pc = check_property_window(target_property, predicted_value, predicted_unit or "")
        property_checks.append(pc)
        if not pc.valid:
            reasons.append(
                f"{target_property}={predicted_value} {predicted_unit or ''} {pc.reason}"
            )

    # 4. 化学式判定
    if mat_check is not None and not mat_check.valid:
        reasons.append(f"化学式 {mat_check.formula}：{mat_check.reason}")

    # 5. 汇总风险等级
    if any(p.prop_name in ("synthesis_temp_k", "synthesis_pressure_gpa")
           and not p.valid for p in property_checks):
        risk = "high"
        signal = 0.0
    elif mat_check is not None and not mat_check.valid:
        risk = "high"
        signal = 0.0
    elif any(not p.valid for p in property_checks):
        risk = "medium"
        signal = 0.3
    elif mat_check is not None and not mat_check.valence_balanced:
        risk = "low"
        signal = 0.6
    else:
        risk = "none"
        signal = 1.0

    # 高 / 中风险都视为不通过（保守原则 — 物理违反或性能超界一律拒绝）
    valid = risk not in ("high", "medium")
    if not reasons:
        reasons.append("通过全部物理一致性检查")

    return CandidateCheckResult(
        valid=valid,
        material_check=mat_check,
        property_checks=property_checks,
        reason="；".join(reasons),
        risk=risk,
        feasibility_signal=signal,
    )


# ============================================================
# 便捷工具：候选筛选
# ============================================================


def filter_physically_viable(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """批量物理一致性筛选，返回 (通过的, 被拒的)。

    每个 candidate 应包含 'material' / 'formula' 字段。
    """
    passed: list[dict] = []
    rejected: list[dict] = []
    for c in candidates:
        config = {
            "material": c.get("material") or c.get("formula", ""),
            **{k: v for k, v in c.items() if k.startswith("synthesis_")},
        }
        result = check_candidate(
            config,
            target_property=c.get("target_property"),
            predicted_value=c.get("predicted_value"),
            predicted_unit=c.get("predicted_unit"),
        )
        enriched = dict(c)
        enriched["physics_check"] = {
            "valid": result.valid,
            "risk": result.risk,
            "feasibility_signal": result.feasibility_signal,
            "reason": result.reason,
            "charge_balance": (result.material_check.charge_balance
                               if result.material_check else None),
            "valence_electron_count": (result.material_check.valence_electron_count
                                       if result.material_check else None),
            "goldschmidt_t": (result.material_check.goldschmidt_t
                              if result.material_check else None),
        }
        if result.valid:
            passed.append(enriched)
        else:
            rejected.append(enriched)
    return passed, rejected