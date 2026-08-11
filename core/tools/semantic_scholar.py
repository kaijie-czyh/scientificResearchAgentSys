"""Semantic Scholar API 检索工具。

补充 arxiv 之外的引用图谱与权威 venue 信息。
- arxiv 适合抓最新预印本（含 abstract 全文）
- S2 适合抓引用数/venue/影响力（用于 PaperRelevanceFilter 评分）

设计要点：
- 失败时返回空列表（不阻塞流程）
- 用 requests 直调 S2 Graph API
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"

# S2 字段（按需精简，避免请求体过大）
_S2_FIELDS = "title,authors,year,abstract,venue,externalIds,citationCount,url,openAccessPdf"


@dataclass
class S2Paper:
    """Semantic Scholar 论文统一数据结构。"""

    s2_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    abstract: str = ""
    venue: str = ""
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    citation_count: int = 0
    url: str = ""
    pdf_url: str = ""
    source_subquery: str = ""

    def to_meta_dict(self) -> dict:
        """转为通用 meta dict。"""
        return {
            "title": self.title.strip(),
            "authors": list(self.authors),
            "year": self.year,
            "abstract": self.abstract,
            "arxiv_id": self.arxiv_id,
            "doi": self.doi,
            "venue": self.venue,
            "citation_count": self.citation_count,
            "url": self.url,
            "pdf_url": self.pdf_url,
            "source_subquery": self.source_subquery,
        }


def search_semantic_scholar(
    query: str,
    max_results: int = 10,
    source_subquery: str = "",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    timeout: int = 30,
) -> list[S2Paper]:
    """Semantic Scholar Graph API 检索。

    Args:
        query: 检索语句
        max_results: 最多返回结果数
        source_subquery: 标记来源子问题
        year_from: 仅返回此年份之后的论文（含，None 不限）
        year_to: 仅返回此年份之前的论文（含，None 不限）
        timeout: 请求超时秒

    Returns:
        list[S2Paper]；失败返回空列表。
    """
    if not query.strip():
        return []

    params = {
        "query": query,
        "limit": min(max_results, 100),  # S2 单次最多 100
        "fields": _S2_FIELDS,
    }
    # S2 year 参数支持 "from-to" / "from-" / "-to"
    if year_from is not None and year_to is not None:
        params["year"] = f"{year_from}-{year_to}"
    elif year_from is not None:
        params["year"] = f"{year_from}-"
    elif year_to is not None:
        params["year"] = f"-{year_to}"

    try:
        resp = requests.get(_S2_API, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("S2 检索失败（query=%r）: %s", query, e)
        return []

    # S2 限流：每秒不超过 1 个请求（无 API key 时更严格）
    time.sleep(1.0)

    data = resp.json()
    papers = _parse_s2_response(data, source_subquery=source_subquery)
    # 客户端兜底：年份硬过滤（API 偶发返回边界外结果）
    if (year_from is not None or year_to is not None) and papers:
        papers = [
            p for p in papers
            if (year_from is None or (p.year or 0) >= year_from)
            and (year_to is None or (p.year or 9999) <= year_to)
        ]
    return papers


def _parse_s2_response(data: dict, source_subquery: str = "") -> list[S2Paper]:
    """解析 S2 API 返回 JSON。"""
    papers: list[S2Paper] = []
    items = data.get("data", []) or []
    for item in items:
        try:
            external_ids = item.get("externalIds", {}) or {}
            authors_raw = item.get("authors", []) or []
            authors = [a.get("name", "") for a in authors_raw if a.get("name")]

            papers.append(S2Paper(
                s2_id=item.get("paperId", ""),
                title=(item.get("title") or "").strip(),
                authors=authors,
                year=item.get("year"),
                abstract=item.get("abstract") or "",
                venue=item.get("venue") or "",
                arxiv_id=external_ids.get("ArXiv"),
                doi=external_ids.get("DOI"),
                citation_count=item.get("citationCount", 0) or 0,
                url=item.get("url", ""),
                pdf_url=(item.get("openAccessPdf") or {}).get("url", "") if item.get("openAccessPdf") else "",
                source_subquery=source_subquery,
            ))
        except Exception as e:
            logger.warning("解析 S2 entry 失败: %s", e)
            continue

    return papers
