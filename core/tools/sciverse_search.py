"""Sciverse 科学智能数据库 API 检索工具。

赛题（GOAI 赛道三·方向三）明确推荐使用 Sciverse：
- 465M knowledge records, 28.32M AI-Ready full texts
- 5 RESTful APIs: agentic-search / meta-search / content / resource / meta-catalog
- 支持 MCP/Skill 接入，调用记录天然构成可审计证据链

与 arxiv/S2 的互补定位：
- arxiv：最新预印本（含 abstract）
- S2：引用图谱/venue/影响力（限流严重）
- Sciverse：Agent-ready 证据层——返回片段级证据（非仅论文列表），可回读原文上下文

设计要点：
- 无 token 时优雅降级返回空结果（不阻塞流程）
- 证据片段级返回，天然适配 PaperIngestAgent 的 chunk 化
- meta-catalog 先发现字段再查询，避免「字段幻觉」
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

_SCIVERSE_BASE = "https://api.sciverse.space"
_TIMEOUT = 30


def _get_token() -> Optional[str]:
    """从环境变量读取 Sciverse API Token。"""
    return os.environ.get("SCIVERSE_API_TOKEN") or os.environ.get("SCIVERSE_TOKEN")


def _headers() -> dict[str, str]:
    token = _get_token()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def is_available() -> bool:
    """Sciverse 是否可用（有 token）。"""
    return _get_token() is not None


@dataclass
class SciverseEvidence:
    """Sciverse 证据片段（agentic-search 返回的粒度）。"""

    doc_id: str = ""
    title: str = ""
    snippet: str = ""  # 证据文本片段
    score: float = 0.0  # 相关性分数
    offset: int = 0  # 原文偏移（用于 content 回读）
    source: str = "sciverse"
    # 元数据
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    doi: Optional[str] = None
    url: str = ""

    def to_meta_dict(self) -> dict:
        """转为通用 paper meta dict（兼容 PaperFetchAgent）。"""
        return {
            "title": self.title.strip(),
            "authors": list(self.authors),
            "year": self.year,
            "abstract": self.snippet,  # 证据片段作为摘要
            "doi": self.doi,
            "venue": self.venue,
            "citation_count": 0,
            "url": self.url,
            "arxiv_id": None,
            "source_subquery": "",
            "source": "sciverse",
            "doc_id": self.doc_id,
            "offset": self.offset,
            "evidence_score": self.score,
        }


# ===== meta-catalog：字段发现 =====

def meta_catalog(timeout: int = _TIMEOUT) -> Optional[dict]:
    """获取 Sciverse 可用字段与算子（避免字段幻觉）。

    Returns:
        catalog dict（含 fields/filters/sorters）；失败返回 None。
    """
    token = _get_token()
    if not token:
        logger.debug("Sciverse token 未配置，meta_catalog 跳过")
        return None
    try:
        resp = requests.get(
            f"{_SCIVERSE_BASE}/meta-catalog",
            headers=_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning("Sciverse meta_catalog 失败: %s", e)
        return None


# ===== agentic-search：语义证据检索 =====

def agentic_search(
    query: str,
    max_results: int = 10,
    source_subquery: str = "",
    timeout: int = _TIMEOUT,
) -> list[SciverseEvidence]:
    """Sciverse agentic-search：自然语言→证据片段。

    与 arxiv/S2 的关键差异：返回片段级证据（非仅论文元数据），
    每个结果含 doc_id + offset，可用 read_content 回读原文上下文。

    Args:
        query: 自然语言检索语句
        max_results: 最多返回结果数
        source_subquery: 标记来源子问题（便于交叉验证）
        timeout: 请求超时秒

    Returns:
        list[SciverseEvidence]；无 token 或失败返回空列表。
    """
    token = _get_token()
    if not token:
        logger.debug("Sciverse token 未配置，agentic_search 跳过")
        return []
    if not query.strip():
        return []

    try:
        resp = requests.post(
            f"{_SCIVERSE_BASE}/agentic-search",
            headers=_headers(),
            json={"query": query, "limit": min(max_results, 50)},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Sciverse agentic_search 失败（query=%r）: %s", query[:60], e)
        return []

    # 限流保护
    time.sleep(0.5)

    data = resp.json()
    return _parse_agentic_response(data, source_subquery=source_subquery)


def _parse_agentic_response(
    data: dict, source_subquery: str = ""
) -> list[SciverseEvidence]:
    """解析 agentic-search 返回。"""
    evidences: list[SciverseEvidence] = []
    items = data.get("results", []) or data.get("data", []) or []
    for item in items:
        try:
            ev = SciverseEvidence(
                doc_id=item.get("doc_id", "") or item.get("id", ""),
                title=(item.get("title") or "").strip(),
                snippet=item.get("snippet") or item.get("content") or item.get("text", ""),
                score=float(item.get("score", 0.0) or 0.0),
                offset=int(item.get("offset", 0) or 0),
                authors=item.get("authors", []) or [],
                year=item.get("year"),
                venue=item.get("venue", "") or "",
                doi=item.get("doi"),
                url=item.get("url", ""),
            )
            if source_subquery:
                ev.source = f"sciverse:{source_subquery}"
            evidences.append(ev)
        except Exception as e:
            logger.warning("解析 Sciverse entry 失败: %s", e)
            continue
    return evidences


# ===== meta-search：结构化元数据检索 =====

def meta_search(
    query: str,
    filters: Optional[dict] = None,
    max_results: int = 10,
    source_subquery: str = "",
    timeout: int = _TIMEOUT,
) -> list[SciverseEvidence]:
    """Sciverse meta-search：结构化字段过滤检索。

    先调 meta_catalog 确认可用字段，再构造 filter。
    filters 示例：{"year_from": 2023, "venue": "Nature", "open_access": True}

    Args:
        query: 检索语句
        filters: 结构化过滤条件
        max_results: 最多返回数
        source_subquery: 标记来源子问题

    Returns:
        list[SciverseEvidence]；无 token 或失败返回空列表。
    """
    token = _get_token()
    if not token:
        return []
    if not query.strip():
        return []

    payload: dict[str, Any] = {"query": query, "limit": min(max_results, 50)}
    if filters:
        payload["filters"] = filters

    try:
        resp = requests.post(
            f"{_SCIVERSE_BASE}/meta-search",
            headers=_headers(),
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Sciverse meta_search 失败: %s", e)
        return []

    time.sleep(0.5)
    data = resp.json()
    return _parse_agentic_response(data, source_subquery=source_subquery)


# ===== content：原文上下文回读 =====

def read_content(
    doc_id: str,
    offset: int = 0,
    length: int = 2000,
    timeout: int = _TIMEOUT,
) -> Optional[str]:
    """Sciverse content：从 doc_id + offset 回读原文上下文。

    用于证据核验：agentic-search 命中片段后，回读上下文确认证据可信。

    Args:
        doc_id: 文档 ID（来自 agentic-search 结果）
        offset: 原文偏移
        length: 读取长度

    Returns:
        原文文本；失败返回 None。
    """
    token = _get_token()
    if not token or not doc_id:
        return None
    try:
        resp = requests.get(
            f"{_SCIVERSE_BASE}/content",
            headers=_headers(),
            params={"doc_id": doc_id, "offset": offset, "length": length},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("content") or data.get("text", "")
    except requests.RequestException as e:
        logger.warning("Sciverse read_content 失败（doc_id=%r）: %s", doc_id, e)
        return None
