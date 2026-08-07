"""论文 URL 解析工具。

Sciverse 真实 API 响应不含 URL/DOI 字段（仅有 doc_id 哈希、标题、venue 等）。
为满足「检索结果可点击溯源」的需求，本文档提供：
1. CrossRef 按标题反查 DOI → 给出 https://doi.org/<doi> 权威链接
2. 反查失败 → Google Scholar 搜索链接兜底（总能打开，用户可直达论文页）

均只读外部公共 API，失败返回兜底链接而非抛错，不阻塞入库流程。
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 8
# CrossRef 合规 User-Agent（其 API 政策要求带 mailto 联系方式）
_UA = "SciResearchAgent/1.0 (GOAI contest; mailto:sci-agent@example.com)"

# 补充材料 DOI 后缀，如 10.1021/acsami.9b15166.s001（优先取主版本）
_SUPPLEMENT_RE = re.compile(r"\.s\d+$", re.IGNORECASE)


def _crossref_doi(title: str) -> Optional[str]:
    """CrossRef 按标题反查主版本 DOI；失败返回 None。"""
    try:
        resp = requests.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": title, "rows": 5},
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", []) or []
        # 优先非补充材料条目（.s001 等），避免跳到 SI 文件
        for it in items:
            doi = it.get("DOI")
            if doi and not _SUPPLEMENT_RE.search(doi):
                return doi
        for it in items:
            if it.get("DOI"):
                return it["DOI"]
    except Exception as e:  # noqa: BLE001
        logger.debug("CrossRef 反查失败（title=%r）: %s", (title or "")[:60], e)
    return None


def _scholar_fallback(title: str) -> str:
    """Google Scholar 搜索链接兜底。"""
    q = quote((title or "").strip())
    return f"https://scholar.google.com/scholar?q={q}"


def resolve_paper_url(
    title: str, venue: str = "", year: Optional[int] = None
) -> str:
    """解析论文可访问 URL。

    Args:
        title: 论文标题（必填）
        venue: 期刊/会议名（仅日志用）
        year: 发表年份（仅日志用）

    Returns:
        权威 DOI 链接；反查失败时返回 Google Scholar 搜索链接（兜底）。
    """
    title = (title or "").strip()
    if not title:
        return ""
    doi = _crossref_doi(title)
    if doi:
        return f"https://doi.org/{doi}"
    logger.debug("CrossRef 未命中（%r, %s, %s），回退 Scholar", title[:60], venue, year)
    return _scholar_fallback(title)
