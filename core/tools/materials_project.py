"""Materials Project API 交叉验证工具（赛题路线 A 硬要求）。

赛题路线 A「构效关系发现」明确要求「与公开数据库交叉验证」。
本工具提供：
1. Materials Project REST API 接入（可选，需要 API key）
   - https://api.materialsproject.org
   - 申请：https://next-gen.materialsproject.org/api
2. 无 API key 时的规则交叉验证（基于文献数据点物理合理性范围检查）

设计要点：
- 无 API key 时优雅降级到规则验证（不阻塞流程）
- 验证逻辑：发现配置的物理参数是否在已知材料体系合理范围内
- 输出 cross_validation_report 含每条发现的验证结果与可信度
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

_MP_BASE = "https://api.materialsproject.org"
_TIMEOUT = 20


def _get_api_key() -> Optional[str]:
    """从环境变量读取 Materials Project API key。"""
    return os.environ.get("MATERIALS_PROJECT_API_KEY") or os.environ.get("MP_API_KEY")


def is_available() -> bool:
    """Materials Project API 是否可用（有 key）。"""
    return _get_api_key() is not None


def query_material_by_formula(formula: str) -> list[dict]:
    """按化学式查询 Materials Project 材料。

    Args:
        formula: 化学式（如 "Bi2Te3"）

    Returns:
        材料条目列表（含 band_gap、density、formula_pretty 等字段）
    """
    api_key = _get_api_key()
    if not api_key:
        return []
    try:
        resp = requests.get(
            f"{_MP_BASE}/materials/summary/{formula}",
            headers={"X-API-KEY": api_key},
            timeout=_TIMEOUT,
            params={"_limit": 5, "_fields": "formula_pretty,band_gap,density,structure,symmetry"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", []) if isinstance(data, dict) else []
    except Exception as e:
        logger.warning("Materials Project 查询失败（formula=%s）: %s", formula, e)
        return []


# ===== 规则交叉验证（无 API key 时使用）=====

# 已知热电材料体系的物理合理范围（基于公开文献综述）
_THERMOELECTRIC_KNOWN_RANGES = {
    # 化学式: (典型 ZT 范围, 典型温度范围 K, 备注)
    "Bi2Te3": ((0.5, 1.5), (300, 500), "室温区经典热电材料"),
    "Sb2Te3": ((0.3, 1.2), (300, 500), "p型室温区"),
    "PbTe": ((0.8, 2.0), (600, 900), "中高温区"),
    "SnSe": ((1.0, 2.8), (600, 950), "单晶 SnSe 高 ZT 记录"),
    "GeTe": ((0.8, 2.3), (600, 800), "中高温区，富 Ge 相变材料"),
    "Skutterudite": ((0.5, 1.8), (600, 900), "填充式 Skutterudite"),
    "half-Heusler": ((0.6, 1.5), (700, 1000), "高温区 half-Heusler"),
    "Cu2Se": ((0.5, 2.1), (700, 1000), "液态离子导体"),
    "Mg3Sb2": ((0.4, 1.5), (500, 800), "无毒性 n 型"),
    "SiGe": ((0.5, 1.3), (900, 1200), "高温区合金"),
    "AgSbTe2": ((0.5, 1.5), (400, 700), "p 型窄带隙"),
    "Zn4Sb3": ((0.6, 1.3), (400, 700), "β-Zn4Sb3"),
}


@dataclass
class CrossValidationResult:
    """单条发现的交叉验证结果。"""

    claim_id: str = ""
    material: str = ""
    novelty: str = "unknown"  # novel / partially_known / known
    mp_match: bool = False  # 是否在 Materials Project 中查到对应材料
    mp_band_gap: Optional[float] = None
    rule_check_passed: bool = False
    rule_check_notes: str = ""
    literature_consistent: bool = False  # 与文献数据点是否一致
    confidence: float = 0.5  # 综合置信度（交叉验证后调整）
    cross_validation_source: str = "rule"  # mp / rule / hybrid


@dataclass
class CrossValidationReport:
    """整个 discovery 阶段的交叉验证报告。"""

    total_discoveries: int = 0
    mp_validated: int = 0
    rule_validated: int = 0
    results: list[CrossValidationResult] = field(default_factory=list)
    overall_confidence: float = 0.5
    source: str = "rule"  # mp / rule / hybrid
    notes: str = ""


def _extract_material(config: dict, relationship: str = "") -> str:
    """从配置中提取材料体系名（用于交叉验证查询）。

    提取策略（多级回退）：
    1. config 直接包含 material/compound/system/matrix 键
    2. config 字符串里出现已知热电材料体系（Bi2Te3、SnSe、PbTe 等）
    3. relationship 文本里出现已知材料体系
    """
    # 优先：从 config 字典常见 key 中提取
    if config:
        for key in ("material", "compound", "system", "matrix", "base_material"):
            if key in config and isinstance(config[key], str):
                val = config[key].strip()
                if val:
                    # 归一化后查表
                    normalized = _normalize_formula(val)
                    if normalized in _THERMOELECTRIC_KNOWN_RANGES:
                        return normalized
                    return val
    # 其次：从 config 与 relationship 字符串中扫描已知材料
    config_str = (str(config) if config else "") + " " + relationship
    config_str_normalized = _normalize_formula(config_str)
    for material in _THERMOELECTRIC_KNOWN_RANGES:
        if material in config_str_normalized:
            return material
    return ""


def _normalize_formula(text: str) -> str:
    """归一化化学式表达（去掉下标/LaTeX 包壳）。"""
    return (
        text
        .replace("$_2$", "2").replace("$_3$", "3").replace("$_4$", "4")
        .replace("$_{2}$", "2").replace("$_{3}$", "3").replace("$_{4}$", "4")
        .replace("\\mathrm{Bi2Te3}", "Bi2Te3").replace("\\mathrm{SnSe}", "SnSe")
        .replace("Bi$_2$Te$_3$", "Bi2Te3").replace("Bi_2Te_3", "Bi2Te3")
        .replace("Sb$_2$Te$_3$", "Sb2Te3").replace("Sb_2Te_3", "Sb2Te3")
        .replace("SnSe", "SnSe").replace("Bi2Te3", "Bi2Te3")
    )


def _rule_check(
    material: str,
    predicted_target: float,
    config: dict,
    literature_points: list[dict],
) -> tuple[bool, str, bool]:
    """规则交叉验证：检查预测值与配置是否在物理合理范围。

    Returns:
        (rule_passed, notes, literature_consistent)
    """
    if not material:
        # 未识别到材料体系时，仅做文献一致性检查
        lit_consistent = _check_literature_consistency(predicted_target, config, literature_points)
        return lit_consistent, "未识别到材料体系，仅做文献一致性检查", lit_consistent

    if material not in _THERMOELECTRIC_KNOWN_RANGES:
        return False, f"材料 {material} 不在已知热电体系范围", False

    (zt_low, zt_high), (t_low, t_high), note = _THERMOELECTRIC_KNOWN_RANGES[material]
    notes_parts = [f"材料 {material} 已知范围：ZT {zt_low}-{zt_high} @ {t_low}-{t_high}K ({note})"]

    # 检查预测 ZT 是否在合理范围（允许略超出，因为是搜索外推）
    zt_in_range = zt_low * 0.5 <= predicted_target <= zt_high * 1.5
    if zt_in_range:
        notes_parts.append(f"预测 ZT={predicted_target:.3g} 在合理范围（允许 50% 外推）")
    else:
        notes_parts.append(
            f"预测 ZT={predicted_target:.3g} 明显超出合理范围，疑似代理模型外推过度"
        )

    # 检查温度配置
    temp = config.get("temperature") or config.get("T") or config.get("temp_K")
    temp_in_range = True
    if isinstance(temp, (int, float)):
        temp_in_range = t_low * 0.7 <= temp <= t_high * 1.2
        if temp_in_range:
            notes_parts.append(f"温度 {temp}K 在合理范围")
        else:
            notes_parts.append(
                f"温度 {temp}K 明显超出材料 {material} 典型温区，建议复核"
            )

    lit_consistent = _check_literature_consistency(predicted_target, config, literature_points)
    if lit_consistent:
        notes_parts.append("与文献数据点趋势一致")
    else:
        notes_parts.append("与文献数据点趋势存在差异（可能是合理外推）")

    passed = zt_in_range and temp_in_range
    return passed, "；".join(notes_parts), lit_consistent


def _check_literature_consistency(
    predicted_target: float, config: dict, literature_points: list[dict]
) -> bool:
    """检查预测值与文献数据点的趋势一致性。

    简单规则：若存在温度相近（±100K）的文献数据点，预测值偏差不超过 50% 即认为一致。
    """
    if not literature_points:
        return False
    temp = config.get("temperature") or config.get("T") or config.get("temp_K")
    if not isinstance(temp, (int, float)):
        # 无温度信息时，只看 ZT 数量级
        for lp in literature_points:
            lp_target = lp.get("target") or lp.get("predicted_target") or 0
            if isinstance(lp_target, (int, float)) and lp_target > 0:
                ratio = predicted_target / lp_target if lp_target else 0
                if 0.3 <= ratio <= 3.0:
                    return True
        return False
    # 有温度信息时，找最近的文献点
    for lp in literature_points:
        lp_config = lp.get("config", {})
        lp_temp = lp_config.get("temperature") or lp_config.get("T") or lp_config.get("temp_K")
        lp_target = lp.get("target") or lp.get("predicted_target") or 0
        if isinstance(lp_temp, (int, float)) and isinstance(lp_target, (int, float)):
            if abs(lp_temp - temp) <= 100 and lp_target > 0:
                ratio = predicted_target / lp_target
                if 0.5 <= ratio <= 2.0:
                    return True
    return False


def cross_validate_discovery(
    relationships: list[dict],
    literature_points: Optional[list[dict]] = None,
) -> CrossValidationReport:
    """对 discovery 发现做 Materials Project + 规则交叉验证。

    Args:
        relationships: 验证后的构效关系列表（含 config / predicted_target / novelty 等）
        literature_points: 文献抽取的 (结构, 性能) 数据点

    Returns:
        CrossValidationReport
    """
    lit_points = literature_points or []
    report = CrossValidationReport(total_discoveries=len(relationships))
    mp_available = is_available()

    for rel in relationships:
        config = rel.get("config", {}) or {}
        # 兼容不同 schema：predicted_target / predicted_ZT / target_value
        predicted_target = (
            rel.get("predicted_target")
            or rel.get("predicted_ZT")
            or rel.get("target_value")
            or 0.0
        )
        material = _extract_material(config, rel.get("relationship", ""))
        novelty = rel.get("novelty", "unknown")
        claim_id = rel.get("claim_id", "")

        # 1. Materials Project API 验证（可选）
        mp_match = False
        mp_band_gap = None
        if mp_available and material:
            try:
                mp_results = query_material_by_formula(material)
                if mp_results:
                    mp_match = True
                    mp_band_gap = mp_results[0].get("band_gap")
                    report.mp_validated += 1
            except Exception as e:
                logger.warning("MP 验证失败（material=%s）: %s", material, e)

        # 2. 规则交叉验证
        rule_passed, notes, lit_consistent = _rule_check(
            material, predicted_target, config, lit_points
        )
        if rule_passed:
            report.rule_validated += 1

        # 3. 综合置信度调整
        confidence = rel.get("confidence", 0.5)
        if mp_match:
            confidence = min(1.0, confidence + 0.15)
        if rule_passed:
            confidence = min(1.0, confidence + 0.10)
        if lit_consistent:
            confidence = min(1.0, confidence + 0.05)
        if not rule_passed and not mp_match:
            confidence = max(0.1, confidence - 0.15)

        source = "hybrid" if (mp_match and rule_passed) else ("mp" if mp_match else "rule")
        report.results.append(CrossValidationResult(
            claim_id=claim_id,
            material=material,
            novelty=novelty,
            mp_match=mp_match,
            mp_band_gap=mp_band_gap,
            rule_check_passed=rule_passed,
            rule_check_notes=notes,
            literature_consistent=lit_consistent,
            confidence=confidence,
            cross_validation_source=source,
        ))

    # 总体置信度
    if report.results:
        report.overall_confidence = sum(r.confidence for r in report.results) / len(report.results)
    report.source = "mp" if mp_available else "rule"
    report.notes = (
        f"使用 Materials Project API + 规则双路交叉验证（{len(report.results)} 条发现）"
        if mp_available
        else f"未配置 MATERIALS_PROJECT_API_KEY，仅使用规则交叉验证（{len(report.results)} 条发现）。"
        f" 配置 API key 后可启用 Materials Project 数据库交叉验证以满足赛题路线 A 完整要求。"
    )
    return report


def report_to_dict(report: CrossValidationReport) -> dict:
    """将报告转为可序列化 dict（持久化到 KV / 返回前端）。"""
    return {
        "total_discoveries": report.total_discoveries,
        "mp_validated": report.mp_validated,
        "rule_validated": report.rule_validated,
        "overall_confidence": report.overall_confidence,
        "source": report.source,
        "notes": report.notes,
        "results": [
            {
                "claim_id": r.claim_id,
                "material": r.material,
                "novelty": r.novelty,
                "mp_match": r.mp_match,
                "mp_band_gap": r.mp_band_gap,
                "rule_check_passed": r.rule_check_passed,
                "rule_check_notes": r.rule_check_notes,
                "literature_consistent": r.literature_consistent,
                "confidence": r.confidence,
                "cross_validation_source": r.cross_validation_source,
            }
            for r in report.results
        ],
    }
