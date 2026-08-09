"""arxiv API 检索工具。

封装 arxiv 包，返回统一的 ArxivPaper 数据结构。
设计要点：
- 不依赖第三方 arxiv 包时降级为 requests 直调 arxiv API（保证可用性）
- 检索结果按 relevance 排序，默认 top 10
- 失败时返回空列表而非抛异常（research 阶段不容错中断）
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)

# arxiv API endpoint
_ARXIV_API = "http://export.arxiv.org/api/query"
_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass
class ArxivPaper:
    """arxiv 论文统一数据结构。"""

    arxiv_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    year: Optional[int] = None
    published: Optional[str] = None  # ISO date string
    url: str = ""
    pdf_url: str = ""
    primary_category: str = ""
    source_subquery: str = ""  # 标记来自哪个子问题检索

    def to_meta_dict(self) -> dict:
        """转为通用 meta dict（供 PaperFetchAgent 使用）。"""
        return {
            "title": self.title.strip(),
            "authors": list(self.authors),
            "year": self.year,
            "abstract": self.abstract,
            "arxiv_id": self.arxiv_id,
            "url": self.url,
            "pdf_url": self.pdf_url,
            "venue": self.primary_category or "arxiv",
            "source_subquery": self.source_subquery,
        }


def _extract_arxiv_id(url: str) -> str:
    """从 arxiv entry URL 提取 arxiv_id。"""
    # http://arxiv.org/abs/2401.12345v1 → 2401.12345
    m = re.search(r"arxiv\.org/abs/([^/]+?)(v\d+)?$", url)
    if m:
        return m.group(1)
    return url.rsplit("/", 1)[-1]


def _extract_year(published: Optional[str]) -> Optional[int]:
    """从 ISO 日期提取年份。"""
    if not published:
        return None
    try:
        return int(published[:4])
    except (ValueError, IndexError):
        return None


def _normalize_query_years(q: str, year_from: Optional[int], year_to: Optional[int]) -> str:
    """把年份范围合并进 arxiv 查询串（yr: 前缀语法）。

    arxiv API 原生支持 [YYYY TO YYYY] 区间检索。
    - 只给 from：yr:YYYY-  （当年及以后）
    - 只给 to：yr:-YYYY   （当年及以前）
    - 都给：yr:YYYY-YYYY（闭区间）
    年份非法时静默忽略该段。
    """
    q = (q or "").strip()
    if not q:
        return q
    try:
        f = int(year_from) if year_from is not None else None
        t = int(year_to) if year_to is not None else None
    except (TypeError, ValueError):
        return q
    if f is not None and (f < 1000 or f > 2100):
        f = None
    if t is not None and (t < 1000 or t > 2100):
        t = None
    if f is None and t is None:
        return q
    if t is not None and f is not None and t < f:
        f, t = t, f  # 兜底防倒挂
    if f is not None and t is not None:
        yr = f"[{f} TO {t}]"
    elif f is not None:
        yr = f"[{f} TO 2100]"
    else:
        yr = f"[1000 TO {t}]"
    # arxiv 查询语法：标题/摘要/全文全字段上做年份约束
    return f"({q}) AND submittedDate:{yr}"


def search_arxiv(
    query: str,
    max_results: int = 10,
    source_subquery: str = "",
    sort_by: str = "relevance",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    timeout: int = 30,
) -> list[ArxivPaper]:
    """arxiv API 检索。

    Args:
        query: 检索语句（自然语言或关键词，会自动 URL 编码）
        max_results: 最多返回结果数
        source_subquery: 标记来源子问题（用于交叉验证溯源）
        sort_by: relevance / submittedDate / lastUpdatedDate
        year_from: 起始年份（含，None 不限）
        year_to: 结束年份（含，None 不限）
        timeout: 请求超时秒

    Returns:
        list[ArxivPaper]；失败时返回空列表（不抛异常）。
    """
    if not query.strip():
        return []

    search_query = _normalize_query_years(query, year_from, year_to)
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": "descending",
    }

    try:
        resp = requests.get(_ARXIV_API, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("arxiv 检索失败（query=%r）: %s", query, e)
        return []

    # arxiv 建议每秒不超过 1 个请求
    time.sleep(0.5)

    papers = _parse_arxiv_response(resp.text, source_subquery=source_subquery)
    # 客户端兜底：API 过滤偶尔不可靠（如部分镜像），再按年份硬过滤一遍
    if (year_from is not None or year_to is not None) and papers:
        papers = [
            p for p in papers
            if (year_from is None or (p.year or 0) >= year_from)
            and (year_to is None or (p.year or 9999) <= year_to)
        ]
    return papers


def _parse_arxiv_response(xml_text: str, source_subquery: str = "") -> list[ArxivPaper]:
    """解析 arxiv API 返回的 Atom XML。"""
    papers: list[ArxivPaper] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("arxiv 响应 XML 解析失败: %s", e)
        return papers

    for entry in root.findall("atom:entry", _ARXIV_NS):
        try:
            id_url_elem = entry.find("atom:id", _ARXIV_NS)
            title_elem = entry.find("atom:title", _ARXIV_NS)
            summary_elem = entry.find("atom:summary", _ARXIV_NS)
            published_elem = entry.find("atom:published", _ARXIV_NS)
            link_elems = entry.findall("atom:link", _ARXIV_NS)
            author_elems = entry.findall("atom:author", _ARXIV_NS)
            category_elem = entry.find("atom:primary_category", _ARXIV_NS)

            id_url = id_url_elem.text.strip() if id_url_elem is not None and id_url_elem.text else ""
            arxiv_id = _extract_arxiv_id(id_url)
            title = " ".join((title_elem.text or "").split()) if title_elem is not None else ""
            abstract = " ".join((summary_elem.text or "").split()) if summary_elem is not None else ""
            published = published_elem.text.strip() if published_elem is not None and published_elem.text else None

            authors: list[str] = []
            for a in author_elems:
                name_elem = a.find("atom:name", _ARXIV_NS)
                if name_elem is not None and name_elem.text:
                    authors.append(name_elem.text.strip())

            url = id_url
            pdf_url = ""
            for link in link_elems:
                if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                    pdf_url = link.get("href", "")
                    break

            primary_category = ""
            if category_elem is not None:
                primary_category = category_elem.get("term", "")

            papers.append(ArxivPaper(
                arxiv_id=arxiv_id,
                title=title,
                authors=authors,
                abstract=abstract,
                year=_extract_year(published),
                published=published,
                url=url,
                pdf_url=pdf_url,
                primary_category=primary_category,
                source_subquery=source_subquery,
            ))
        except Exception as e:
            logger.warning("解析 arxiv entry 失败: %s", e)
            continue

    return papers
