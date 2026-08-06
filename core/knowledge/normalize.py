"""材料知识数据标准化（Task 2 增强）。

将 LLM 抽取的杂乱字段归一为材料科学标准体系，供前端统一展示与检索：
1. 性能指标归一化：ZT / zT / thermoelectric_figure_of_merit → 标准「热电优值 (ZT)」
2. 合成方法分类：ball milling / DFT computation / solid-state reaction → 工艺类别
3. 材料体系分类：按名称/化学式识别 Bi2Te3 基、PbTe 基、钙钛矿等体系
"""
from __future__ import annotations

import re

# ===== 1. 性能指标归一化 =====
# 映射键：LLM 可能输出的各种拼写 → 标准性能
# 每个标准性能：key(标准名) / cn(中文名) / symbol(符号) / unit(常见单位) / category(类别)

PROPERTY_CANON: dict[str, dict] = {
    "ZT": {"key": "ZT", "cn": "热电优值", "symbol": "ZT", "unit": "", "category": "热电优值"},
    "power_factor": {"key": "power_factor", "cn": "功率因子", "symbol": "PF", "unit": "μW/cm·K²", "category": "电输运"},
    "thermal_conductivity": {"key": "thermal_conductivity", "cn": "热导率", "symbol": "κ", "unit": "W/m·K", "category": "热输运"},
    "lattice_thermal_conductivity": {"key": "lattice_thermal_conductivity", "cn": "晶格热导率", "symbol": "κ_L", "unit": "W/m·K", "category": "热输运"},
    "electronic_thermal_conductivity": {"key": "electronic_thermal_conductivity", "cn": "电子热导率", "symbol": "κ_e", "unit": "W/m·K", "category": "热输运"},
    "seebeck_coefficient": {"key": "seebeck_coefficient", "cn": "Seebeck 系数", "symbol": "S", "unit": "μV/K", "category": "电输运"},
    "electrical_conductivity": {"key": "electrical_conductivity", "cn": "电导率", "symbol": "σ", "unit": "S/cm", "category": "电输运"},
    "electrical_resistivity": {"key": "electrical_resistivity", "cn": "电阻率", "symbol": "ρ", "unit": "Ω·cm", "category": "电输运"},
    "carrier_concentration": {"key": "carrier_concentration", "cn": "载流子浓度", "symbol": "n", "unit": "cm⁻³", "category": "载流子"},
    "carrier_mobility": {"key": "carrier_mobility", "cn": "载流子迁移率", "symbol": "μ", "unit": "cm²/V·s", "category": "载流子"},
    "carrier_lifetime": {"key": "carrier_lifetime", "cn": "载流子寿命", "symbol": "τ", "unit": "s", "category": "载流子"},
    "band_gap": {"key": "band_gap", "cn": "带隙", "symbol": "E_g", "unit": "eV", "category": "能带结构"},
    "band_structure": {"key": "band_structure", "cn": "能带结构", "symbol": "", "unit": "", "category": "能带结构"},
    "formation_energy": {"key": "formation_energy", "cn": "形成能", "symbol": "E_f", "unit": "eV", "category": "稳定性"},
    "debye_temperature": {"key": "debye_temperature", "cn": "德拜温度", "symbol": "Θ_D", "unit": "K", "category": "热输运"},
    "energy_conversion_efficiency": {"key": "energy_conversion_efficiency", "cn": "能量转换效率", "symbol": "η", "unit": "%", "category": "器件性能"},
    "exciton_binding_energy": {"key": "exciton_binding_energy", "cn": "激子结合能", "symbol": "E_b", "unit": "meV", "category": "能带结构"},
    "thermoelectric_performance": {"key": "thermoelectric_performance", "cn": "热电性能", "symbol": "", "unit": "", "category": "热电优值"},
    "thermoelectric_figure_of_merit": {"key": "ZT", "cn": "热电优值", "symbol": "ZT", "unit": "", "category": "热电优值"},
    "thermoelectric_properties": {"key": "thermoelectric_performance", "cn": "热电性能", "symbol": "", "unit": "", "category": "热电优值"},
    "resistivity": {"key": "electrical_resistivity", "cn": "电阻率", "symbol": "ρ", "unit": "Ω·cm", "category": "电输运"},
    "power_factor_enhancement": {"key": "power_factor", "cn": "功率因子", "symbol": "PF", "unit": "μW/cm·K²", "category": "电输运"},
    "zT": {"key": "ZT", "cn": "热电优值", "symbol": "ZT", "unit": "", "category": "热电优值"},
    "figure_of_merit": {"key": "ZT", "cn": "热电优值", "symbol": "ZT", "unit": "", "category": "热电优值"},
    "phonon_thermal_conductivity": {"key": "lattice_thermal_conductivity", "cn": "晶格热导率", "symbol": "κ_L", "unit": "W/m·K", "category": "热输运"},
    "effective_mass": {"key": "effective_mass", "cn": "有效质量", "symbol": "m*", "unit": "m₀", "category": "载流子"},
    "density_of_states": {"key": "density_of_states", "cn": "态密度", "symbol": "DOS", "unit": "", "category": "能带结构"},
    "lattice_constant": {"key": "lattice_constant", "cn": "晶格常数", "symbol": "a", "unit": "Å", "category": "稳定性"},
    "gruneisen_parameter": {"key": "gruneisen_parameter", "cn": "格林艾森参数", "symbol": "γ", "unit": "", "category": "热输运"},
    "anharmonicity": {"key": "anharmonicity", "cn": "非谐性", "symbol": "", "unit": "", "category": "热输运"},
    "phonon_mean_free_path": {"key": "phonon_mean_free_path", "cn": "声子平均自由程", "symbol": "Λ", "unit": "nm", "category": "热输运"},
    "carrier_type": {"key": "carrier_type", "cn": "载流子类型", "symbol": "", "unit": "", "category": "载流子"},
    "hall_coefficient": {"key": "hall_coefficient", "cn": "霍尔系数", "symbol": "R_H", "unit": "cm³/C", "category": "载流子"},
}

# 归一化兜底：按名称子串匹配（大小写不敏感）
_PROPERTY_ALIAS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:device|module).?zt", re.I), "ZT"),
    (re.compile(r"\bzt\b|figure.?of.?merit", re.I), "ZT"),
    (re.compile(r"power.?factor", re.I), "power_factor"),
    (re.compile(r"lattice.?thermal|phonon.?thermal", re.I), "lattice_thermal_conductivity"),
    (re.compile(r"electronic.?thermal|electron.?thermal", re.I), "electronic_thermal_conductivity"),
    (re.compile(r"thermal.?conduct", re.I), "thermal_conductivity"),
    (re.compile(r"seebeck", re.I), "seebeck_coefficient"),
    (re.compile(r"electrical.?conduct|electronic.?conduct", re.I), "electrical_conductivity"),
    (re.compile(r"resistiv", re.I), "electrical_resistivity"),
    (re.compile(r"carrier.?concentr", re.I), "carrier_concentration"),
    (re.compile(r"carrier.?mobil", re.I), "carrier_mobility"),
    (re.compile(r"carrier.?lifetime", re.I), "carrier_lifetime"),
    (re.compile(r"carrier.?type|doping.?type|majority.?carrier", re.I), "carrier_type"),
    (re.compile(r"hall.?coeffic", re.I), "hall_coefficient"),
    (re.compile(r"band.?gap", re.I), "band_gap"),
    (re.compile(r"band.?structure", re.I), "band_structure"),
    (re.compile(r"density.?of.?states|DOS", re.I), "density_of_states"),
    (re.compile(r"effective.?mass", re.I), "effective_mass"),
    (re.compile(r"formation.?energy", re.I), "formation_energy"),
    (re.compile(r"lattice.?constant|cell.?parameter|unit.?cell", re.I), "lattice_constant"),
    (re.compile(r"debye", re.I), "debye_temperature"),
    (re.compile(r"gruneisen|grüneisen", re.I), "gruneisen_parameter"),
    (re.compile(r"anharmonic", re.I), "anharmonicity"),
    (re.compile(r"mean.?free.?path|phonon.?mean", re.I), "phonon_mean_free_path"),
    (re.compile(r"conversion.?efficien", re.I), "energy_conversion_efficiency"),
    (re.compile(r"exciton", re.I), "exciton_binding_energy"),
    (re.compile(r"thermoelectric", re.I), "thermoelectric_performance"),
]


def normalize_property(property_name: str, property_name_cn: str = "") -> dict:
    """把 LLM 抽取的性能名归一为标准性能。

    Returns:
        {"key", "cn", "symbol", "unit", "category", "original"}
    """
    original = (property_name or "").strip()
    if not original:
        return {"key": "", "cn": property_name_cn or "", "symbol": "",
                "unit": "", "category": "其他", "original": ""}
    # 1) 精确匹配
    canon = PROPERTY_CANON.get(original.lower())
    # 2) 中文名匹配（property_name 与 property_name_cn 任一命中即可，
    #    兼容 LLM 把中文名直接写在 property_name 的情况）
    if canon is None and (original or property_name_cn):
        cn_lower = f"{original} {property_name_cn}".lower().replace(" ", "")
        cn_map = {
            "热电优值": "ZT", "功率因子": "power_factor", "热导率": "thermal_conductivity",
            "晶格热导率": "lattice_thermal_conductivity", "电子热导率": "electronic_thermal_conductivity",
            "seebeck系数": "seebeck_coefficient",
            "电导率": "electrical_conductivity", "电阻率": "electrical_resistivity",
            "载流子浓度": "carrier_concentration", "载流子迁移率": "carrier_mobility",
            "载流子寿命": "carrier_lifetime", "载流子类型": "carrier_type",
            "有效质量": "effective_mass", "态密度": "density_of_states",
            "带隙": "band_gap", "能带结构": "band_structure",
            "形成能": "formation_energy", "晶格常数": "lattice_constant",
            "德拜温度": "debye_temperature", "格林艾森参数": "gruneisen_parameter",
            "能量转换效率": "energy_conversion_efficiency", "激子结合能": "exciton_binding_energy",
            "热电性能": "thermoelectric_performance",
        }
        matched = next((v for k, v in cn_map.items() if k in cn_lower), None)
        if matched:
            canon = PROPERTY_CANON.get(matched)
    # 3) 正则子串兜底
    if canon is None:
        for pat, key in _PROPERTY_ALIAS_PATTERNS:
            if pat.search(original):
                canon = PROPERTY_CANON.get(key)
                break
    if canon is None:
        return {"key": original, "cn": property_name_cn or original,
                "symbol": "", "unit": "", "category": "其他", "original": original}
    return {**canon, "original": original}


# ===== 2. 合成方法分类 =====
# method 原文 → (分类, 中文名)

_METHOD_CATEGORY_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"first.?principles|DFT|density functional|ab.?initio|DFTB", re.I),
     "计算模拟", "第一性原理计算"),
    (re.compile(r"machine.?learning|ML|Monte Carlo|numerical simulation|simulation|data.?driven", re.I),
     "计算模拟", "机器学习/数值模拟"),
    (re.compile(r"arc melting|melt spinning|melting|zone.?melt", re.I),
     "熔融法", "熔融/急冷法"),
    (re.compile(r"solid.?state|bulk synthesis|ceramic", re.I),
     "固相法", "固相反应"),
    (re.compile(r"ball milling|mechanical alloying|high.?energy milling", re.I),
     "球磨法", "机械球磨"),
    (re.compile(r"spark plasma|SPS|hot press|sinter", re.I),
     "烧结法", "放电等离子烧结/热压"),
    (re.compile(r"electrodepos|electrochem|electroplating", re.I),
     "电化学法", "电化学沉积"),
    (re.compile(r"solution|sol.?gel|hydrothermal|microwave|wet.?chem", re.I),
     "溶液法", "溶液/湿化学法"),
    (re.compile(r"CVD|vapor|sputter|deposition|epitax|film", re.I),
     "薄膜法", "气相/薄膜沉积"),
    (re.compile(r"nanoparticle|template|colloid", re.I),
     "纳米合成", "纳米颗粒/模板法"),
]


def classify_method(method: str) -> dict:
    """把合成方法原文归类为标准工艺类别。

    Returns:
        {"category": 大类, "label": 中文工艺名, "original": 原文}
    """
    original = (method or "").strip()
    if not original or original.lower() in ("not specified", "n/a", "-"):
        return {"category": "未指定", "label": "未指定", "original": original}
    for pat, cat, label in _METHOD_CATEGORY_PATTERNS:
        if pat.search(original):
            return {"category": cat, "label": label, "original": original}
    return {"category": "其他", "label": "其他工艺", "original": original}


# ===== 3. 材料体系分类 =====
# 顺序敏感：先匹配具体体系，再匹配通用体系

_MATERIAL_CATEGORY_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 注意：Half-Heusler 必须在方钴矿之前（TiCoSb 含 "CoSb" 子串，会先命中方钴矿）
    (re.compile(r"half.?heusler|TiCoSb|NiTiSn|ZrNiSn|HfNiSn", re.I), "Half-Heusler"),
    (re.compile(r"skutterudite|CoSb", re.I), "方钴矿"),
    (re.compile(r"perovskite|MAPbI|CH3NH3PbI|CsPb|FAPb", re.I), "钙钛矿"),
    (re.compile(r"Bi2Te3|bismuth telluride|BST", re.I), "Bi₂Te₃ 基"),
    (re.compile(r"PbTe|lead telluride|lead chalcogenide", re.I), "PbTe 基"),
    (re.compile(r"SnTe|tin telluride", re.I), "SnTe 基"),
    (re.compile(r"GeTe|germanium telluride", re.I), "GeTe 基"),
    (re.compile(r"SnSe|tin selenide|tin monosulfide|SnS", re.I), "SnSe/SnS 基"),
    (re.compile(r"chalcogenide|selenide|sulfide|telluride|Cu2|CuFe|Cu4Sn|BaCu|SrCu|CuIn|Ag2Te|Ag2Se|Cu2Se|Cu2Te|\bPbS\b|\bPbSe\b|\bSb2Te3\b|\bBi2S3\b|\bBi2Se3\b|(?:Se|S|Te)\d", re.I), "硫族化物"),
    (re.compile(r"clathrate|zintl", re.I), "笼合物/Zintl"),
    (re.compile(r"graphene|carbon nanotube|carbon|fullerene|graphite", re.I), "碳材料"),
    (re.compile(r"high.?entropy", re.I), "高熵合金"),
    (re.compile(r"alloy|intermetallic", re.I), "合金"),
    (re.compile(r"oxide|Ga2O3|NiO|BaCe|spinel|ferrite|Fe2O4|Fe3O4|SOFC", re.I), "氧化物"),
    (re.compile(r"diamondoid|GaAs|silicon|Si\b", re.I), "半导体"),
    (re.compile(r"photovoltaic|solar|perovskite solar", re.I), "光伏材料"),
    (re.compile(r"batter|electrode|cathode|anode|LiCo|LiFe|LiNi|Na-ion", re.I), "电池材料"),
    (re.compile(r"phosphor|light.?emit|LED", re.I), "发光材料"),
    (re.compile(r"magnetic", re.I), "磁性材料"),
]


def categorize_material(name: str, formula: str = "") -> str:
    """识别材料体系分类。

    Returns:
        体系名（如 "Bi₂Te₃ 基"）；无法识别返回 "其他"
    """
    text = f"{name or ''} {formula or ''}"
    for pat, cat in _MATERIAL_CATEGORY_PATTERNS:
        if pat.search(text):
            return cat
    return "其他"


# 性能指标类别 → 中文标签（前端分组用）
PROPERTY_CATEGORIES = [
    "热电优值", "电输运", "热输运", "载流子", "能带结构", "稳定性", "器件性能", "其他",
]
