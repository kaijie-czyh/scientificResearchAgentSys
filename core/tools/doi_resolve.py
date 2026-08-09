"""DOI / 开放获取 PDF 解析工具（OpenAlex 首选 + Crossref/Unpaywall 兜底）。

目标：让尽量多的论文获得可下载的 PDF 链接。

数据源（均免费、无需注册 key）：
1. **OpenAlex**（首选）：按标题搜索一次返回 DOI + best_oa_location.pdf_url 直链。
   限速约 10 req/s，返回 mailto 即可。
2. **Crossref**（兜底反查 DOI）：按标题+作者反查 DOI。
3. **Unpaywall**（兜底找 OA PDF）：按 DOI 查合法 OA 版本。

流程（``resolve_pdf_link`` 一键组合）：
1. meta.pdf_url 已有直链 → 直接用
2. meta.arxiv_id → arxiv PDF 直链
3. 无 DOI → OpenAlex 按标题查（拿 DOI + OA PDF）
4. 有 DOI → OpenAlex 按 DOI 查 OA PDF → Unpaywall 兜底
5. 最后 doi.org 兜底（跳转文章页，可能直接是 PDF）
"""
from __future__ import annotations

import logging
import re
import time
from difflib import SequenceMatcher
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_CONTACT = "sciresearch.agent@outlook.com"
_UA = f"SciResearchAgent/1.0 (mailto:{_CONTACT})"
# 简单内存缓存：key -> {"doi": ..., "pdf": ...}，避免重复请求
_CACHE: dict[str, dict] = {}


def _norm_title(s: str) -> str:
    """归一化标题用于相似度比较。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _similar(a: str, b: str) -> float:
    """两个标题的字符级相似度 0~1。"""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _pick_doi(raw: str) -> str:
    """从 OpenAlex 的 doi 字段提取纯 DOI（去掉 https://doi.org/ 前缀）。"""
    d = (raw or "").strip()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    return d


def query_openalex(
    title: str,
    authors: Optional[list[str]] = None,
    year: Optional[int] = None,
    timeout: int = _TIMEOUT,
) -> list[dict]:
    """OpenAlex 按标题搜索论文。

    Returns:
        list[dict]：每项含 title/doi/pdf_url/score（按相关性降序）。
    """
    t = (title or "").strip()
    if not t:
        return []
    # 清理搜索词：OpenAlex search 参数不接受 ? : 等特殊符号（会 400）
    search_q = re.sub(r"[?:\"']", " ", t)
    search_q = re.sub(r"\s+", " ", search_q).strip()
    if not search_q:
        return []
    try:
        resp = requests.get(
            "https://api.openalex.org/works",
            params={
                "search": search_q,
                "per-page": 5,
                "select": "doi,title,best_oa_location,primary_location,publication_year",
            },
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json().get("results", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("OpenAlex 查询失败（title=%r）: %s", t[:60], e)
        return []

    out = []
    for it in items:
        best = it.get("best_oa_location") or {}
        pdf = (best.get("pdf_url") or "").strip()
        if not pdf:
            # 退而求其次：primary_location 的 landing page
            prim = it.get("primary_location") or {}
            pdf = (prim.get("pdf_url") or "").strip()
        out.append({
            "title": it.get("title") or "",
            "doi": _pick_doi(it.get("doi") or ""),
            "pdf_url": pdf,
            "score": _similar(t, it.get("title") or ""),
            "year": it.get("publication_year"),
        })
    time.sleep(0.1)  # 温和限流
    return out


def resolve_doi_by_title(
    title: str,
    authors: Optional[list[str]] = None,
    year: Optional[int] = None,
    timeout: int = _TIMEOUT,
) -> str:
    """按标题反查 DOI（OpenAlex 首选，Crossref 兜底）。

    Returns:
        DOI 字符串；未命中返回 ""。
    """
    t = (title or "").strip()
    if not t:
        return ""
    cache_key = f"doi:{_norm_title(t)[:120]}"
    if cache_key in _CACHE:
        return _CACHE[cache_key].get("doi", "")

    doi = ""
    # 1. OpenAlex
    try:
        for r in query_openalex(t, authors=authors, year=year, timeout=timeout):
            if r["doi"] and r["score"] >= 0.85:
                doi = r["doi"]
                break
    except Exception:  # noqa: BLE001
        doi = ""

    # 2. Crossref 兜底
    if not doi:
        doi = _crossref_doi(t, authors=authors, year=year, timeout=timeout)

    _CACHE[cache_key] = {"doi": doi, "pdf": ""}
    return doi


def _crossref_doi(
    title: str, authors: Optional[list[str]] = None,
    year: Optional[int] = None, timeout: int = _TIMEOUT,
) -> str:
    """Crossref 按标题反查 DOI（相似度 ≥ 0.85 才采纳）。"""
    t = (title or "").strip()
    if not t:
        return ""
    params: dict = {"query.title": t, "rows": 5, "select": "DOI,title,issued"}
    if year:
        params["filter"] = f"from-pub-date:{year - 1}-01-01,until-pub-date:{year + 1}-12-31"
    try:
        resp = requests.get(
            "https://api.crossref.org/works",
            params=params,
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("Crossref 查询失败（title=%r）: %s", t[:60], e)
        return ""
    best_doi, best_score = "", 0.0
    for it in items:
        for x in (it.get("title") or []):
            s = _similar(t, x)
            if s > best_score:
                best_score = s
                best_doi = it.get("DOI", "") or ""
    return best_doi if best_score >= 0.85 else ""


def find_open_access_pdf(
    doi: str,
    title: str = "",
    email: str = _CONTACT,
    timeout: int = _TIMEOUT,
    force: bool = False,
) -> str:
    """按 DOI（或标题）查找合法开放获取 PDF 直链。

    优先级：OpenAlex 按 DOI/标题 → Unpaywall 按 DOI。

    Args:
        doi: 论文 DOI
        title: 论文标题（doi 为空时用标题查）
        email: Unpaywall 联系邮箱
        timeout: 请求超时
        force: True 时绕过内存缓存强制重新查询（用于下载时重试）

    Returns:
        OA PDF 直链；未找到返回 ""。
    """
    doi = (doi or "").strip().lower()
    cache_key = f"oa:{doi or _norm_title(title)[:120]}"
    if not force and cache_key in _CACHE:
        return _CACHE[cache_key].get("pdf", "")

    pdf = ""
    # 1. OpenAlex：有 DOI 直接查，否则用标题
    try:
        if doi:
            resp = requests.get(
                f"https://api.openalex.org/works/https://doi.org/{doi}",
                params={"select": "doi,best_oa_location"},
                headers={"User-Agent": _UA},
                timeout=timeout,
            )
            if resp.status_code == 200:
                it = resp.json()
                best = it.get("best_oa_location") or {}
                pdf = (best.get("pdf_url") or "").strip()
        if not pdf and title:
            for r in query_openalex(title, timeout=timeout):
                if r["pdf_url"] and r["score"] >= 0.85:
                    pdf = r["pdf_url"]
                    break
    except Exception as e:  # noqa: BLE001
        logger.warning("OpenAlex OA 查询失败（doi=%s）: %s", doi, e)

    # 2. Unpaywall 兜底（仅当有 DOI）
    if not pdf and doi:
        try:
            resp = requests.get(
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": email},
                headers={"User-Agent": _UA},
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                best = data.get("best_oa_location") or {}
                pdf = (best.get("url_for_pdf") or best.get("url") or "").strip()
                if not pdf:
                    for loc in data.get("oa_locations") or []:
                        u = (loc.get("url_for_pdf") or loc.get("url") or "").strip()
                        if u:
                            pdf = u
                            break
            time.sleep(0.3)  # Unpaywall 限流
        except Exception as e:  # noqa: BLE001
            logger.warning("Unpaywall 查询失败（doi=%s）: %s", doi, e)

    _CACHE[cache_key] = {"doi": doi, "pdf": pdf}
    return pdf


def resolve_pdf_link(meta: dict, timeout: int = _TIMEOUT) -> str:
    """一键解析论文可下载 PDF 链接（原地补充 meta["pdf_url"]/meta["doi"]）。

    优先级：
    1. meta.pdf_url 已有直链（S2 openAccessPdf / arxiv 直返）
    2. meta.arxiv_id → arxiv PDF
    3. 无 DOI → OpenAlex 按标题反查 DOI + OA PDF
    4. 有 DOI → OpenAlex / Unpaywall 找 OA PDF
    5. 兜底：有 DOI 则返回 doi.org 链接（跳转文章页，可能直接是 PDF）

    Args:
        meta: 论文元数据 dict（含 title/authors/year/doi/arxiv_id/pdf_url）

    Returns:
        最终 PDF URL（可能为空字符串）。
    """
    # 1. 已有直链
    pdf_url = (meta.get("pdf_url") or "").strip()
    if pdf_url:
        return pdf_url
    # 2. arxiv
    arxiv_id = (meta.get("arxiv_id") or "").strip()
    if arxiv_id:
        meta["pdf_url"] = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        return meta["pdf_url"]
    # 3/4. DOI 链路
    title = meta.get("title") or ""
    doi = (meta.get("doi") or "").strip()
    if not doi:
        doi = resolve_doi_by_title(
            title,
            authors=meta.get("authors"),
            year=meta.get("year"),
            timeout=timeout,
        )
        if doi:
            meta["doi"] = doi
    if doi:
        oa = find_open_access_pdf(doi, title=title, timeout=timeout)
        if oa:
            meta["pdf_url"] = oa
            return oa
        # 兜底：doi.org（部分出版商直接返回 PDF）
        meta["pdf_url"] = f"https://doi.org/{doi}"
        return meta["pdf_url"]
    return ""
