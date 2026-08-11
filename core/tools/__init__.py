"""科研工具集（外部 API / 数据检索）。

模块化设计：每个工具独立，便于按需导入与替换。
- arxiv_search: arxiv API 检索（research 阶段真实文献抓取）
- text_split: 文本切分（PaperIngestAgent chunk 化）
- semantic_scholar: Semantic Scholar API 检索（补充 arxiv 之外的引用图谱）
- sciverse_search: Sciverse 科学智能数据库（赛题推荐，证据片段级检索）
- code_runner: 沙盒代码运行（experiment 阶段执行 LLM 生成的代码）
- materials_search: 构效关系搜索（路线 A：MCTS + 文献代理模型 + LLM 融合）
- materials_project: Materials Project API 交叉验证（赛题路线 A 公开数据库交叉验证要求）
- data_provenance: 外部数据源 / API 的元信息登记（赛题 §5.3 合规披露支撑）
"""
from __future__ import annotations

from core.tools.arxiv_search import ArxivPaper, search_arxiv
from core.tools.code_runner import (
    RunResult,
    check_syntax,
    get_execution_mode,
    is_remote_mode,
    run_python_code,
    run_python_code_remote,
)
from core.tools.data_provenance import (
    DATA_SOURCES,
    get_source,
    list_sources,
    summarize as data_sources_summary,
    to_markdown_table as data_sources_to_markdown,
)
from core.tools.materials_project import (
    CrossValidationReport,
    CrossValidationResult,
    cross_validate_discovery as mp_cross_validate_discovery,
    is_available as mp_is_available,
    query_material_by_formula as mp_query_material,
    report_to_dict as mp_report_to_dict,
)
from core.tools.mineru_parse import (
    MinerUClient,
    MinerUDocument,
    MinerUFigure,
    MinerUSection,
    MinerUTable,
    mineru_is_available,
    parse_pdf_with_mineru,
)
from core.tools.materials_search import (
    LiteraturePoint,
    MCTSSearcher,
    SearchCandidate,
    SearchVariable,
    SurrogateModel,
    build_literature_points,
    build_search_variables,
    perturb_config,
)
from core.tools.oqmd_nomad import (
    OQMDClient,
    oqmd_is_available,
    query_oqmd_by_formula,
)
from core.tools.sciverse_search import (
    SciverseEvidence,
    agentic_search as sciverse_agentic_search,
    is_available as sciverse_is_available,
    meta_catalog as sciverse_meta_catalog,
    meta_search as sciverse_meta_search,
    read_content as sciverse_read_content,
)
from core.tools.semantic_scholar import S2Paper, search_semantic_scholar
from core.tools.journal_quality import JournalQuality, enrich_paper_quality, lookup_journal_quality, build_pdf_url
from core.tools.doi_resolve import (
    find_open_access_pdf,
    resolve_doi_by_title,
    resolve_pdf_link,
)
from core.tools.text_split import split_into_chunks

__all__ = [
    "ArxivPaper",
    "search_arxiv",
    "S2Paper",
    "search_semantic_scholar",
    "SciverseEvidence",
    "sciverse_agentic_search",
    "sciverse_meta_catalog",
    "sciverse_meta_search",
    "sciverse_read_content",
    "sciverse_is_available",
    "split_into_chunks",
    "RunResult",
    "run_python_code",
    "run_python_code_remote",
    "check_syntax",
    "get_execution_mode",
    "is_remote_mode",
    "LiteraturePoint",
    "MCTSSearcher",
    "SearchCandidate",
    "SearchVariable",
    "SurrogateModel",
    "build_literature_points",
    "build_search_variables",
    "perturb_config",
    "CrossValidationReport",
    "CrossValidationResult",
    "mp_cross_validate_discovery",
    "mp_is_available",
    "mp_query_material",
    "mp_report_to_dict",
    "DATA_SOURCES",
    "list_sources",
    "get_source",
    "data_sources_summary",
    "data_sources_to_markdown",
    "MinerUClient",
    "MinerUDocument",
    "MinerUFigure",
    "MinerUSection",
    "MinerUTable",
    "mineru_is_available",
    "parse_pdf_with_mineru",
    "OQMDClient",
    "oqmd_is_available",
    "query_oqmd_by_formula",
    "JournalQuality",
    "enrich_paper_quality",
    "lookup_journal_quality",
    "build_pdf_url",
    "find_open_access_pdf",
    "resolve_doi_by_title",
    "resolve_pdf_link",
]

