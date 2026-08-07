"""关键词趋势分析工具。

通过 arXiv API 获取领域论文，从标题/摘要中提取关键词并按年份统计频率，
为 TrendAnalysisAgent 提供数据基础。

设计要点：
- 一次 API 调用获取大批论文（默认 200 篇），减少请求次数
- 关键词提取用简单分词 + 停用词过滤（不依赖 NLP 库）
- 按年份统计关键词出现频率，计算年度增长率
- 失败时返回空数据而非抛异常（不阻塞流程）
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from core.tools.arxiv_search import ArxivPaper, search_arxiv

logger = logging.getLogger(__name__)

# 英文停用词（覆盖常见学术无关词）
_STOPWORDS: set[str] = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "must", "shall", "can",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "their", "we", "our", "us", "he", "she", "his", "her", "him",
    "which", "what", "who", "when", "where", "why", "how", "all",
    "each", "every", "both", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than",
    "too", "very", "just", "also", "via", "using", "used", "use",
    "based", "study", "studies", "studied", "research", "work",
    "paper", "result", "results", "show", "shows", "shown", "showed",
    "propose", "proposed", "propose", "method", "methods", "approach",
    "approaches", "model", "models", "system", "systems", "data",
    "performance", "performance", "high", "low", "new", "novel",
    "recent", "recently", "between", "through", "during", "after",
    "before", "about", "into", "over", "under", "above", "below",
    "up", "down", "out", "off", "then", "here", "there", "any",
    "both", "each", "further", "once", "well", "been", "now",
    "two", "three", "one", "first", "second", "third", "last",
    # arxiv 特有噪声
    "abstract", "title", "author", "authors", "et", "al", "fig",
    "figure", "figures", "table", "tables", "eq", "equation",
    "equations", "ref", "reference", "references", "section",
    "sections", "http", "https", "www", "com", "org",
}

# 关键词最小长度
_MIN_KEYWORD_LEN = 3
# 关键词最大长度（过滤超长噪声）
_MAX_KEYWORD_LEN = 50
# 返回 top-N 关键词
_TOP_N_KEYWORDS = 50


@dataclass
class TrendData:
    """趋势分析结果数据结构。"""

    # 关键词年度频率：{keyword: {year: count}}
    keyword_frequencies: dict[str, dict[int, int]] = field(default_factory=dict)
    # 每年论文总数
    total_papers_by_year: dict[int, int] = field(default_factory=dict)
    # 抓取到的论文元数据（前 20 篇样本，供 LLM 参考）
    sample_papers: list[dict] = field(default_factory=list)
    # 查询关键词
    query: str = ""
    # 总抓取数
    total_fetched: int = 0

    def to_dict(self) -> dict:
        return {
            "keyword_frequencies": self.keyword_frequencies,
            "total_papers_by_year": self.total_papers_by_year,
            "sample_papers": self.sample_papers,
            "query": self.query,
            "total_fetched": self.total_fetched,
        }


def extract_keywords(text: str) -> list[str]:
    """从文本中提取关键词（简单分词 + 停用词过滤）。

    提取规则：
    - 按非字母数字字符分词
    - 转小写
    - 过滤停用词
    - 过滤过短/过长词
    - 保留化学式风格的关键词（含数字和下标，如 Bi2Te3, CsPbBr3）

    Args:
        text: 输入文本（论文标题或摘要）

    Returns:
        关键词列表（可能含重复，供调用方统计频率）
    """
    if not text:
        return []

    # 转小写
    text = text.lower()

    # 按非字母数字字符分词（保留化学式如 bi2te3）
    raw_tokens = re.findall(r"[a-z][a-z0-9]{2,49}", text)

    keywords: list[str] = []
    for token in raw_tokens:
        if token in _STOPWORDS:
            continue
        if len(token) < _MIN_KEYWORD_LEN:
            continue
        if len(token) > _MAX_KEYWORD_LEN:
            continue
        keywords.append(token)

    return keywords


def fetch_keyword_trends(
    query: str,
    max_results: int = 200,
    sort_by: str = "submittedDate",
) -> TrendData:
    """获取关键词的年度频率趋势。

    通过 arXiv API 搜索论文，从标题和摘要中提取关键词，按年份统计频率。

    Args:
        query: 检索关键词（如 "thermoelectric materials"）
        max_results: 最多获取论文数（默认 200）
        sort_by: 排序方式（submittedDate 可获取不同年份的论文）

    Returns:
        TrendData，包含关键词年度频率、每年论文总数、样本论文
    """
    if not query.strip():
        return TrendData()

    # 调用 arXiv API 获取论文
    papers = search_arxiv(
        query=query,
        max_results=max_results,
        sort_by=sort_by,
    )

    if not papers:
        logger.warning("趋势分析：arXiv 检索返回 0 篇论文（query=%r）", query)
        return TrendData(query=query)

    # 统计关键词年度频率
    keyword_frequencies: dict[str, dict[int, int]] = {}
    total_papers_by_year: dict[int, int] = {}

    for paper in papers:
        year = paper.year
        if year is None:
            continue

        # 每年论文总数
        total_papers_by_year[year] = total_papers_by_year.get(year, 0) + 1

        # 从标题和摘要中提取关键词
        text = f"{paper.title} {paper.abstract}"
        keywords = extract_keywords(text)

        # 统计每个关键词在该年份的出现次数
        seen_in_paper: set[str] = set()
        for kw in keywords:
            if kw not in seen_in_paper:
                seen_in_paper.add(kw)
                if kw not in keyword_frequencies:
                    keyword_frequencies[kw] = {}
                keyword_frequencies[kw][year] = keyword_frequencies[kw].get(year, 0) + 1

    # 取 top-N 关键词（按总频率排序）
    keyword_total = {
        kw: sum(years.values()) for kw, years in keyword_frequencies.items()
    }
    top_keywords = sorted(keyword_total, key=keyword_total.get, reverse=True)[:_TOP_N_KEYWORDS]
    filtered_frequencies = {kw: keyword_frequencies[kw] for kw in top_keywords}

    # 样本论文（前 20 篇）
    sample_papers = [p.to_meta_dict() for p in papers[:20]]

    logger.info(
        "趋势分析完成：query=%r, 获取 %d 篇论文, %d 个关键词, 年份范围 %d-%d",
        query,
        len(papers),
        len(filtered_frequencies),
        min(total_papers_by_year.keys()) if total_papers_by_year else 0,
        max(total_papers_by_year.keys()) if total_papers_by_year else 0,
    )

    return TrendData(
        keyword_frequencies=filtered_frequencies,
        total_papers_by_year=total_papers_by_year,
        sample_papers=sample_papers,
        query=query,
        total_fetched=len(papers),
    )


def compute_growth_rates(
    keyword_frequencies: dict[str, dict[int, int]],
) -> dict[str, dict]:
    """计算每个关键词的增长率并分类。

    分类规则：
    - emerging（新兴方向）：最近一年增长率 > 50%
    - stable（稳定方向）：增长率 10%-50%
    - saturated（饱和方向）：增长率 < 10% 或负增长

    Args:
        keyword_frequencies: {keyword: {year: count}}

    Returns:
        {
            "emerging": [{"keyword": ..., "growth_rate": ..., "trend": [...]}],
            "stable": [...],
            "saturated": [...],
            "all_keywords": [{"keyword": ..., "growth_rate": ..., "total_count": ..., "trend": [...]}],
        }
    """
    if not keyword_frequencies:
        return {"emerging": [], "stable": [], "saturated": [], "all_keywords": []}

    all_results: list[dict] = []
    emerging: list[dict] = []
    stable: list[dict] = []
    saturated: list[dict] = []

    for keyword, year_counts in keyword_frequencies.items():
        if not year_counts:
            continue

        # 按年份排序
        sorted_years = sorted(year_counts.keys())
        if len(sorted_years) < 2:
            # 只有一年数据，无法计算增长率
            total = sum(year_counts.values())
            entry = {
                "keyword": keyword,
                "growth_rate": 0.0,
                "total_count": total,
                "trend": [{"year": y, "count": year_counts[y]} for y in sorted_years],
            }
            all_results.append(entry)
            stable.append(entry)
            continue

        # 计算最近一年的增长率
        latest_year = sorted_years[-1]
        prev_year = sorted_years[-2]
        latest_count = year_counts[latest_year]
        prev_count = year_counts[prev_year]

        if prev_count == 0:
            growth_rate = 1.0 if latest_count > 0 else 0.0
        else:
            growth_rate = (latest_count - prev_count) / prev_count

        total_count = sum(year_counts.values())
        trend = [{"year": y, "count": year_counts[y]} for y in sorted_years]

        entry = {
            "keyword": keyword,
            "growth_rate": round(growth_rate, 4),
            "total_count": total_count,
            "latest_year": latest_year,
            "latest_count": latest_count,
            "trend": trend,
        }
        all_results.append(entry)

        # 分类
        if growth_rate > 0.5:
            emerging.append(entry)
        elif growth_rate >= 0.1:
            stable.append(entry)
        else:
            saturated.append(entry)

    # 排序：新兴方向按增长率降序，饱和方向按总频率降序
    emerging.sort(key=lambda x: x["growth_rate"], reverse=True)
    stable.sort(key=lambda x: x["total_count"], reverse=True)
    saturated.sort(key=lambda x: x["total_count"], reverse=True)
    all_results.sort(key=lambda x: x["total_count"], reverse=True)

    return {
        "emerging": emerging,
        "stable": stable,
        "saturated": saturated,
        "all_keywords": all_results,
    }


def placeholder_trends(query: str) -> TrendData:
    """生成占位趋势数据（dry_run 模式用）。

    构造模拟的关键词频率数据，覆盖 3 年范围。
    """
    import random

    base_keywords = [
        "thermoelectric", "perovskite", "zt", "bi2te3", "seebeck",
        "thermal", "conductivity", "electrical", "oxide", "alloy",
        "nanostructure", "doping", "composite", "figure", "merit",
        "band", "structure", "carrier", "scattering", "phonon",
        "cspbbr3", "halide", "inorganic", "organic", "hybrid",
        "machine", "learning", "discovery", "optimization", "screening",
    ]

    years = [2022, 2023, 2024, 2025]
    keyword_frequencies: dict[str, dict[int, int]] = {}

    for kw in base_keywords:
        keyword_frequencies[kw] = {}
        base = random.randint(5, 30)
        for y in years:
            # 模拟增长趋势
            if kw in ("cspbbr3", "halide", "inorganic", "machine", "learning", "discovery", "screening"):
                # 新兴方向：快速增长
                keyword_frequencies[kw][y] = int(base * (1 + 0.6 * (y - 2022)))
            elif kw in ("bi2te3", "zt", "seebeck"):
                # 稳定方向
                keyword_frequencies[kw][y] = int(base * (1 + 0.2 * (y - 2022)))
            else:
                # 饱和方向
                keyword_frequencies[kw][y] = max(1, int(base * (1 - 0.05 * (y - 2022))))

    total_papers_by_year = {y: sum(keyword_frequencies[kw].get(y, 0) for kw in base_keywords) for y in years}

    sample_papers = [
        {
            "title": f"[占位] {query} 相关论文 {i+1}",
            "year": random.choice(years),
            "abstract": f"占位摘要，关键词：{query}",
            "arxiv_id": f"2401.{10000+i:05d}",
        }
        for i in range(10)
    ]

    return TrendData(
        keyword_frequencies=keyword_frequencies,
        total_papers_by_year=total_papers_by_year,
        sample_papers=sample_papers,
        query=query,
        total_fetched=200,
    )
