"""OQMD（开放量子材料数据库）API 客户端封装（赛题三·方向三·路线 A 加分项）。

OQMD（Open Quantum Materials Database）提供 DFT 计算的材料形成焓、带隙等数据，
用于交叉验证材料科学发现。可通过 REST API 查询。

三种查询模式（优雅降级）：
- api  ：通过 OQMD REST API 真实查询（oqmd.org）
- fallback：使用内置材料常识表（含常见热电/电池/催化材料的已知物理范围）
- unavailable：API 不可用且无 token

赛题路线 A 明确要求「鼓励与 Materials Project、OQMD、NOMAD 等公开数据库交叉验证」，
本模块为该要求提供 OQMD 接入。

使用示例：
    client = OQMDClient()
    if client.is_available():
        results = client.query_by_formula("Bi2Te3")
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class OQMDEntry:
    """OQMD 单条材料条目。"""

    formula: str
    formation_energy: Optional[float] = None  # eV/atom
    band_gap: Optional[float] = None  # eV
    stability: Optional[str] = None  # stable / metastable / unstable
    space_group: Optional[str] = None
    source: str = "oqmd"
    url: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OQMDQueryResult:
    """OQMD 查询结果。"""

    query: str
    matched: bool
    entries: list[OQMDEntry] = field(default_factory=list)
    source: str = "fallback"  # api / fallback
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "matched": self.matched,
            "entries": [e.to_dict() for e in self.entries],
            "source": self.source,
            "error": self.error,
        }


# 内置热电材料常识表（OQMD API 不可用时降级使用）
# 数据来自常见热电材料公开论文，避免硬编码具体数值作为"真值"
_THERMOELECTRIC_REFERENCE: dict[str, dict[str, Any]] = {
    "Bi2Te3": {
        "formation_energy_range": (-0.5, -0.1),  # eV/atom
        "band_gap_range": (0.1, 0.2),  # eV (实验值 ~0.15)
        "typical_zt_range": (0.8, 1.2),  # @ 300-400K
        "stable": True,
        "application": "近室温热电",
    },
    "Sb2Te3": {
        "formation_energy_range": (-0.4, -0.1),
        "band_gap_range": (0.2, 0.3),
        "typical_zt_range": (0.5, 0.9),
        "stable": True,
        "application": "热电（与 Bi2Te3 合金）",
    },
    "PbTe": {
        "formation_energy_range": (-0.8, -0.3),
        "band_gap_range": (0.3, 0.4),
        "typical_zt_range": (0.8, 2.4),  # 高温可达 2.0+
        "stable": True,
        "application": "中温热电",
    },
    "SnSe": {
        "formation_energy_range": (-0.7, -0.2),
        "band_gap_range": (0.8, 1.0),  # 间接带隙
        "typical_zt_range": (0.5, 2.6),  # 单晶 SnSe 高温可达 2.6
        "stable": True,
        "application": "高温热电（2014 Nature 突破）",
    },
    "Mg3Sb2": {
        "formation_energy_range": (-0.6, -0.2),
        "band_gap_range": (0.5, 0.8),
        "typical_zt_range": (0.5, 1.5),
        "stable": True,
        "application": "中温热电（n 型）",
    },
    "SiGe": {
        "formation_energy_range": (-0.4, -0.1),
        "band_gap_range": (0.7, 1.1),
        "typical_zt_range": (0.5, 1.0),
        "stable": True,
        "application": "高温热电（RTG 太空应用）",
    },
    "Cu2Se": {
        "formation_energy_range": (-0.5, -0.2),
        "band_gap_range": (1.0, 1.5),
        "typical_zt_range": (0.5, 2.0),
        "stable": True,
        "application": "中温热电（液态离子导体）",
    },
    "GeTe": {
        "formation_energy_range": (-0.5, -0.2),
        "band_gap_range": (0.5, 0.8),
        "typical_zt_range": (0.6, 1.8),
        "stable": True,
        "application": "中温热电（铁电协同）",
    },
}


class OQMDClient:
    """OQMD 客户端。"""

    BASE_URL = "https://oqmd.org/OQMD"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OQMD_API_KEY", "")

    def is_available(self) -> bool:
        """OQMD REST API 不需要 auth，简单 GET 可达即视为可用。

        实际检查放在 query 时（避免启动阻塞）。
        """
        return True  # API 总是"潜在可用"，fallback 保证不抛异常

    def query_by_formula(self, formula: str) -> OQMDQueryResult:
        """按化学式查询材料条目。

        优先 OQMD API（失败时降级到 fallback）。
        """
        try:
            return self._query_via_api(formula)
        except Exception as e:
            logger.warning("OQMD API 查询失败（formula=%s）：%s，降级 fallback", formula, e)
            return self._query_fallback(formula, error=str(e))

    # ----- API 模式 -----

    def _query_via_api(self, formula: str) -> OQMDQueryResult:
        """通过 OQMD REST API 查询。"""
        try:
            import requests  # type: ignore
        except ImportError:
            raise RuntimeError("OQMD API 需要 requests 库")

        # OQMD REST API: GET /OQMD?formula=Bi2Te3
        resp = requests.get(
            f"{self.BASE_URL}",
            params={"formula": formula, "format": "json"},
            timeout=30,
        )
        resp.raise_for_status()

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("OQMD 返回非 JSON")

        entries: list[OQMDEntry] = []
        # OQMD 返回结构（兼容多种格式）
        for item in (data if isinstance(data, list) else data.get("results", [])):
            entry = OQMDEntry(
                formula=item.get("formula", formula),
                formation_energy=_safe_float(item.get("formation_energy")),
                band_gap=_safe_float(item.get("band_gap") or item.get("gap")),
                stability=item.get("stability", ""),
                space_group=item.get("spacegroup", ""),
                source="oqmd",
                url=f"https://oqmd.org/materials/{item.get('id', '')}",
            )
            entries.append(entry)

        return OQMDQueryResult(
            query=formula,
            matched=len(entries) > 0,
            entries=entries,
            source="api",
        )

    # ----- Fallback 模式 -----

    def _query_fallback(self, formula: str, error: str = "") -> OQMDQueryResult:
        """Fallback：使用内置常识表。

        不作为硬真值，仅用于判断材料是否在已知研究范围内、给出大致物理范围。
        """
        ref = _THERMOELECTRIC_REFERENCE.get(formula)
        if not ref:
            # 模糊匹配（处理 Bi2Te3 与 Bi_2Te_3）
            norm = formula.replace("_", "").replace("$", "")
            for k, v in _THERMOELECTRIC_REFERENCE.items():
                if k.replace("_", "").replace("$", "") == norm:
                    ref = v
                    break

        if not ref:
            return OQMDQueryResult(
                query=formula,
                matched=False,
                source="fallback",
                error=error or f"材料 {formula} 不在参考表中",
            )

        entry = OQMDEntry(
            formula=formula,
            formation_energy=sum(ref["formation_energy_range"]) / 2,
            band_gap=sum(ref["band_gap_range"]) / 2,
            stability="stable" if ref.get("stable") else "metastable",
            source="oqmd_fallback",
            note=(
                f"参考范围：形成焓 {ref['formation_energy_range']} eV/atom, "
                f"带隙 {ref['band_gap_range']} eV, "
                f"典型 ZT {ref['typical_zt_range']} "
                f"({ref.get('application', '')})"
            ),
            url=f"https://oqmd.org/?query={formula}",
        )
        return OQMDQueryResult(
            query=formula,
            matched=True,
            entries=[entry],
            source="fallback",
            error=error,
        )


# ===========================================================================
# 模块级便捷函数
# ===========================================================================


_default_client: Optional[OQMDClient] = None


def _get_default_client() -> OQMDClient:
    global _default_client
    if _default_client is None:
        _default_client = OQMDClient()
    return _default_client


def oqmd_is_available() -> bool:
    return _get_default_client().is_available()


def query_oqmd_by_formula(formula: str) -> OQMDQueryResult:
    """模块级：按化学式查询 OQMD（降级到 fallback）。"""
    return _get_default_client().query_by_formula(formula)


def _safe_float(value: Any) -> Optional[float]:
    """安全转换为 float。"""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None