"""期刊影响因子与中科院分区查询工具。

提供常见材料科学/物理/化学/能源等领域期刊的 IF + CAS 分区映射，
通过 venue 字符串模糊匹配返回质量指标。

数据来源：Journal Citation Reports (JCR, 2025 版 / 2026-06 发布) + 中科院文献情报中心分区表。
顶刊数值已按 2025 JCR 最新发布核实更新；仅内置科研常用期刊，未命中时返回默认值
（IF=0, CAS=""），不阻塞流程。

设计要点：
- 纯静态映射，无网络请求，O(1) 查询
- venue 模糊匹配：大小写不敏感 + 子串匹配 + 常见缩写映射
- 可扩展：外部可传入额外映射表合并
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class JournalQuality:
    """期刊质量指标。"""

    impact_factor: float = 0.0      # JCR 影响因子（最新年）
    cas_zone: str = ""              # 中科院分区：1/2/3/4 或 ""（未收录）
    cas_subcategory: str = ""       # 中科院大类：如 "材料科学1区Top"
    is_top: bool = False            # 是否 Top 期刊


# ===== 期刊映射表 =====
# key: 期刊名（小写），value: JournalQuality
# 覆盖材料科学/物理/化学/能源/纳米/工程等科研主流期刊
_JOURNAL_DB: dict[str, JournalQuality] = {
    # ===== 顶级综合期刊（2025 JCR）=====
    "nature": JournalQuality(56.1, "1", "综合性期刊1区Top", True),
    "science": JournalQuality(47.3, "1", "综合性期刊1区Top", True),
    "nature communications": JournalQuality(18.1, "1", "综合性期刊1区Top", True),
    "science advances": JournalQuality(13.9, "1", "综合性期刊1区Top", True),
    "proceedings of the national academy of sciences": JournalQuality(9.5, "1", "综合性期刊1区", False),
    "pnas": JournalQuality(9.5, "1", "综合性期刊1区", False),

    # ===== 材料科学顶刊（2025 JCR）=====
    "advanced materials": JournalQuality(28.4, "1", "材料科学1区Top", True),
    "advanced functional materials": JournalQuality(19.9, "1", "材料科学1区Top", True),
    "advanced energy materials": JournalQuality(25.7, "1", "材料科学1区Top", True),
    "nano letters": JournalQuality(9.1, "1", "材料科学1区Top", True),
    "acs nano": JournalQuality(16.3, "1", "材料科学1区Top", True),
    "nano energy": JournalQuality(16.8, "1", "材料科学1区Top", True),
    "energy & environmental science": JournalQuality(30.8, "1", "材料科学1区Top", True),
    "energy and environmental science": JournalQuality(30.8, "1", "材料科学1区Top", True),
    "materials today": JournalQuality(22.0, "1", "材料科学1区Top", True),
    "materials today advances": JournalQuality(8.5, "2", "材料科学2区", False),
    "advanced science": JournalQuality(14.1, "1", "材料科学1区Top", True),
    "small": JournalQuality(13.0, "1", "材料科学1区Top", True),
    "small methods": JournalQuality(12.4, "1", "材料科学1区", False),
    "acs materials letters": JournalQuality(11.4, "1", "材料科学1区", False),
    "advanced optical materials": JournalQuality(8.4, "2", "材料科学2区", False),
    "advanced electronic materials": JournalQuality(6.8, "2", "材料科学2区", False),
    "npj computational materials": JournalQuality(8.4, "1", "材料科学1区", False),
    "npj 2d materials and applications": JournalQuality(10.4, "1", "材料科学1区", False),
    "2d materials": JournalQuality(3.5, "2", "材料科学2区", False),

    # ===== 热电/能源材料（2025 JCR）=====
    "journal of materials chemistry a": JournalQuality(10.7, "1", "材料科学1区Top", True),
    "journal of materials chemistry b": JournalQuality(6.1, "2", "材料科学2区", False),
    "journal of materials chemistry c": JournalQuality(5.7, "2", "材料科学2区", False),
    "journal of power sources": JournalQuality(8.3, "1", "材料科学1区", False),
    "journal of energy chemistry": JournalQuality(15.6, "1", "材料科学1区Top", True),
    "acs energy letters": JournalQuality(17.5, "1", "材料科学1区Top", True),
    "energy storage materials": JournalQuality(19.2, "1", "材料科学1区Top", True),
    "sustainable energy & fuels": JournalQuality(4.6, "2", "材料科学2区", False),
    "sustainable energy and fuels": JournalQuality(4.6, "2", "材料科学2区", False),

    # ===== 化学（2025 JCR）=====
    "journal of the american chemical society": JournalQuality(16.6, "1", "化学1区Top", True),
    "jacs": JournalQuality(16.6, "1", "化学1区Top", True),
    "angewandte chemie international edition": JournalQuality(17.6, "1", "化学1区Top", True),
    "angewandte chemie": JournalQuality(17.6, "1", "化学1区Top", True),
    "chemical society reviews": JournalQuality(48.3, "1", "化学1区Top", True),
    "chemical reviews": JournalQuality(64.2, "1", "化学1区Top", True),
    "acs applied materials & interfaces": JournalQuality(9.5, "1", "材料科学1区", False),
    "acs applied materials and interfaces": JournalQuality(9.5, "1", "材料科学1区", False),
    "langmuir": JournalQuality(3.7, "2", "化学2区", False),
    "chemistry of materials": JournalQuality(8.6, "1", "材料科学1区", False),
    "inorganic chemistry": JournalQuality(4.6, "2", "化学2区", False),
    "journal of physical chemistry c": JournalQuality(3.3, "3", "化学3区", False),
    "journal of physical chemistry letters": JournalQuality(5.7, "2", "化学2区", False),
    "journal of physical chemistry b": JournalQuality(3.3, "3", "化学3区", False),
    "journal of physical chemistry a": JournalQuality(2.8, "3", "化学3区", False),

    # ===== 物理（2025 JCR）=====
    "physical review letters": JournalQuality(9.4, "1", "物理1区Top", True),
    "physical review b": JournalQuality(3.7, "2", "物理2区", False),
    "physical review materials": JournalQuality(3.5, "2", "物理2区", False),
    "physical review applied": JournalQuality(4.7, "2", "物理2区", False),
    "physical review x": JournalQuality(16.8, "1", "物理1区Top", True),
    "reviews of modern physics": JournalQuality(54.5, "1", "物理1区Top", True),
    "review of modern physics": JournalQuality(54.5, "1", "物理1区Top", True),
    "applied physics letters": JournalQuality(3.7, "2", "物理2区", False),
    "journal of applied physics": JournalQuality(2.4, "3", "物理3区", False),
    "nanoscale": JournalQuality(6.7, "2", "材料科学2区", False),
    "nanoscale advances": JournalQuality(4.4, "3", "材料科学3区", False),
    "nanotechnology": JournalQuality(3.5, "3", "材料科学3区", False),
    "carbon": JournalQuality(10.5, "1", "材料科学1区", False),
    "graphene": JournalQuality(3.5, "3", "材料科学3区", False),

    # ===== 工程与综合（2025 JCR）=====
    "nature materials": JournalQuality(38.5, "1", "材料科学1区Top", True),
    "nature energy": JournalQuality(60.1, "1", "材料科学1区Top", True),
    "nature nanotechnology": JournalQuality(34.9, "1", "材料科学1区Top", True),
    "nature photonics": JournalQuality(38.1, "1", "材料科学1区Top", True),
    "nature physics": JournalQuality(18.0, "1", "物理1区Top", True),
    "nature chemistry": JournalQuality(24.5, "1", "化学1区Top", True),
    "scientific reports": JournalQuality(3.8, "3", "综合性期刊3区", False),
    "npj quantum materials": JournalQuality(9.2, "1", "物理1区", False),

    # ===== 专门期刊（2025 JCR）=====
    "acta materialia": JournalQuality(8.3, "1", "材料科学1区", False),
    "scripta materialia": JournalQuality(4.1, "2", "材料科学2区", False),
    "materials science and engineering a": JournalQuality(4.4, "2", "材料科学2区", False),
    "materials science and engineering r": JournalQuality(26.8, "1", "材料科学1区Top", True),
    "materials & design": JournalQuality(7.6, "1", "材料科学1区", False),
    "materials and design": JournalQuality(7.6, "1", "材料科学1区", False),
    "corrosion science": JournalQuality(7.4, "1", "材料科学1区", False),
    "journal of alloys and compounds": JournalQuality(6.2, "2", "材料科学2区", False),
    "intermetallics": JournalQuality(4.4, "3", "材料科学3区", False),
    "computational materials science": JournalQuality(3.3, "3", "材料科学3区", False),
    "modelling and simulation in materials science and engineering": JournalQuality(1.9, "4", "材料科学4区", False),
    "npj computational materials": JournalQuality(8.4, "1", "材料科学1区", False),
    "iscience": JournalQuality(4.4, "3", "综合性期刊3区", False),
    "device": JournalQuality(6.0, "2", "材料科学2区", False),
    "matter": JournalQuality(18.9, "1", "材料科学1区Top", True),
    "joule": JournalQuality(35.4, "1", "材料科学1区Top", True),

    # ===== 高分子/软物质 =====
    "macromolecules": JournalQuality(5.8, "2", "化学2区", False),
    "polymer": JournalQuality(4.6, "2", "材料科学2区", False),
    "soft matter": JournalQuality(2.9, "3", "材料科学3区", False),
    "macromolecular rapid communications": JournalQuality(4.6, "3", "材料科学3区", False),

    # ===== 晶体/无机 =====
    "crystal growth & design": JournalQuality(3.6, "3", "材料科学3区", False),
    "crystengcomm": JournalQuality(2.6, "4", "材料科学4区", False),
    "dalton transactions": JournalQuality(3.7, "3", "化学3区", False),

    # ===== 测量/仪器 =====
    "review of scientific instruments": JournalQuality(1.6, "4", "物理4区", False),
    "measurement science and technology": JournalQuality(2.4, "4", "物理4区", False),

    # ===== 其他常见 =====
    "rsc advances": JournalQuality(3.9, "3", "化学3区", False),
    "new journal of chemistry": JournalQuality(3.3, "3", "化学3区", False),
    "scientific reports": JournalQuality(3.8, "3", "综合性期刊3区", False),
    "plos one": JournalQuality(3.7, "3", "综合性期刊3区", False),
    "ieee transactions on nanotechnology": JournalQuality(2.5, "4", "材料科学4区", False),
    "sensors and actuators a": JournalQuality(4.3, "3", "材料科学3区", False),
    "sensors and actuators b": JournalQuality(7.4, "1", "材料科学1区", False),
    "applied surface science": JournalQuality(6.1, "2", "材料科学2区", False),
    "surface and coatings technology": JournalQuality(4.9, "2", "材料科学2区", False),
    "thin solid films": JournalQuality(2.0, "4", "材料科学4区", False),
    "vacuum": JournalQuality(3.8, "3", "材料科学3区", False),
    "ceramics international": JournalQuality(5.1, "2", "材料科学2区", False),
    "journal of the european ceramic society": JournalQuality(5.6, "2", "材料科学2区", False),
    "electrochimica acta": JournalQuality(6.0, "2", "材料科学2区", False),
    "solid state ionics": JournalQuality(3.3, "3", "材料科学3区", False),
    "journal of solid state chemistry": JournalQuality(3.5, "3", "材料科学3区", False),
    "inorganic chemistry frontiers": JournalQuality(7.0, "1", "材料科学1区", False),
    "chemical engineering journal": JournalQuality(13.3, "1", "材料科学1区Top", True),
    "journal of hazardous materials": JournalQuality(12.2, "1", "材料科学1区", False),
    "desalination": JournalQuality(9.9, "1", "材料科学1区", False),
    "separation and purification technology": JournalQuality(8.6, "1", "材料科学1区", False),
    "acs omega": JournalQuality(4.1, "3", "化学3区", False),
    "molecules": JournalQuality(4.9, "3", "化学3区", False),
}

# 常见缩写 -> 全称映射（用于模糊匹配）
_ABBR_MAP: dict[str, str] = {
    "jacs": "journal of the american chemical society",
    "jacs au": "journal of the american chemical society",
    "acsami": "acs applied materials and interfaces",
    "afm": "advanced functional materials",
    "aem": "advanced energy materials",
    "eel": "energy & environmental science",
    "ees": "energy & environmental science",
    "am": "advanced materials",
    "adv mater": "advanced materials",
    "adv funct mater": "advanced functional materials",
    "adv energy mater": "advanced energy materials",
    "j mater chem a": "journal of materials chemistry a",
    "j mater chem b": "journal of materials chemistry b",
    "j mater chem c": "journal of materials chemistry c",
    "j power sources": "journal of power sources",
    "appl phys lett": "applied physics letters",
    "j appl phys": "journal of applied physics",
    "phys rev b": "physical review b",
    "prb": "physical review b",
    "prl": "physical review letters",
    "nat commun": "nature communications",
    "nat mater": "nature materials",
    "nat energy": "nature energy",
    "nat nano": "nature nanotechnology",
    "nat photon": "nature photonics",
    "sci rep": "scientific reports",
    "chem mater": "chemistry of materials",
    "j am chem soc": "journal of the american chemical society",
    "angew chem int ed": "angewandte chemie international edition",
    "chem rev": "chemical reviews",
    "chem soc rev": "chemical society reviews",
    "j phys chem c": "journal of physical chemistry c",
    "j phys chem lett": "journal of physical chemistry letters",
    "acs appl mater interfaces": "acs applied materials and interfaces",
    "adv sci": "advanced science",
    "nano lett": "nano letters",
    "acs appl energy mater": "acs applied energy materials",
    "j alloys compd": "journal of alloys and compounds",
    "acta mater": "acta materialia",
    "scr mater": "scripta materialia",
    "mater sci eng a": "materials science and engineering a",
    "mater sci eng r": "materials science and engineering r",
    "comput mater sci": "computational materials science",
    "eng struct": "engineering structures",
    "mech mater": "mechanics of materials",
}


def _normalize_venue(venue: str) -> str:
    """标准化 venue 字符串用于匹配：小写 + 去多余空白 + 去标点。"""
    import re
    v = (venue or "").strip().lower()
    # 去除常见标点：. , : ; ( ) [ ] & -
    v = re.sub(r"[.,:;()\[\]&\-]", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v


# 预构建标准化 DB（key 已 _normalize_venue），避免每次查询重复处理
_NORMALIZED_DB: dict[str, JournalQuality] = {}
for _k, _v in _JOURNAL_DB.items():
    _nk = _normalize_venue(_k)
    if _nk:
        _NORMALIZED_DB[_nk] = _v


def lookup_journal_quality(venue: str) -> JournalQuality:
    """查询期刊的影响因子与中科院分区。

    匹配策略（按优先级）：
    1. 精确匹配（标准化后）
    2. 缩写映射（如 "Adv. Mater." → "advanced materials"）
    3. 最长子串匹配（venue 包含某期刊名，优先匹配最长的避免短名截获）

    Args:
        venue: 期刊/会议名称（如 "Advanced Materials", "ACS Nano", "arxiv"）

    Returns:
        JournalQuality；未命中返回 IF=0, CAS="" 的默认值。
    """
    if not venue or not venue.strip():
        return JournalQuality()

    v = _normalize_venue(venue)

    # 1. 精确匹配
    if v in _NORMALIZED_DB:
        return _NORMALIZED_DB[v]

    # 2. 缩写映射
    if v in _ABBR_MAP:
        full = _normalize_venue(_ABBR_MAP[v])
        if full in _NORMALIZED_DB:
            return _NORMALIZED_DB[full]

    # 3. 最长子串匹配：优先匹配最长的期刊名（避免 "science" 截获 "energy environmental science"）
    best_match: Optional[JournalQuality] = None
    best_len = 0
    for name, q in _NORMALIZED_DB.items():
        if len(name) > best_len and (name in v or v in name):
            best_match = q
            best_len = len(name)
    if best_match:
        return best_match

    # 4. 缩写子串匹配
    for abbr, full in _ABBR_MAP.items():
        if abbr in v:
            full_norm = _normalize_venue(full)
            if full_norm in _NORMALIZED_DB:
                return _NORMALIZED_DB[full_norm]

    # arxiv 预印本特殊处理：标记为未收录
    if "arxiv" in v or "preprint" in v:
        return JournalQuality(0.0, "", "预印本（未正式发表）", False)

    return JournalQuality()


def enrich_paper_quality(meta: dict) -> None:
    """给论文 meta dict 补充期刊质量字段（原地修改）。

    在 PaperFetchAgent._real_fetch 收集完所有论文后批量调用。
    补充字段：
    - impact_factor: float（0.0 表示未收录/预印本）
    - cas_zone: str（"1"/"2"/"3"/"4" 或 ""）
    - cas_subcategory: str（如 "材料科学1区Top"）
    - is_top_journal: bool
    """
    venue = meta.get("venue") or ""
    q = lookup_journal_quality(venue)
    meta["impact_factor"] = q.impact_factor
    meta["cas_zone"] = q.cas_zone
    meta["cas_subcategory"] = q.cas_subcategory
    meta["is_top_journal"] = q.is_top


def build_pdf_url(meta: dict) -> str:
    """根据论文 meta 字段构造可下载的 PDF URL。

    优先级：
    1. 已有 pdf_url（arxiv/S2 直返）
    2. arxiv_id → https://arxiv.org/pdf/{arxiv_id}
    3. doi → https://doi.org/{doi}（部分出版商会直接返回 PDF）
    4. 空（无法下载）

    Args:
        meta: 论文元数据 dict

    Returns:
        PDF 可下载 URL 或空字符串。
    """
    # 1. 已有 pdf_url
    pdf_url = (meta.get("pdf_url") or "").strip()
    if pdf_url:
        return pdf_url

    # 2. arxiv_id 构造
    arxiv_id = (meta.get("arxiv_id") or "").strip()
    if arxiv_id:
        return f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    # 3. DOI 构造（部分开放获取期刊 DOI 直接跳转 PDF）
    doi = (meta.get("doi") or "").strip()
    if doi:
        return f"https://doi.org/{doi}"

    return ""
