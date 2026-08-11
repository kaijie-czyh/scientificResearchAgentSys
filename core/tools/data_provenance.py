"""数据源合规登记（赛题 §5.3 依赖、数据来源与合规披露 支撑模块）。

目的：
- 把系统中使用的所有外部数据源 / API / 商业服务的元信息集中登记
- 为方案文档 §5.3「合规披露」与前端 dashboard「数据源合规性」卡片提供单一事实来源
- 保持纯只读（无网络请求、无副作用）：仅返回登记的元数据，便于离线审阅与复现声明

字段含义（对应赛题"开源依赖、商业服务、外部数据来源、许可证与关键版本"要求）：
- name: 数据源名称（中文）
- category: 数据类别（paper_db / material_db / retrieval_api / parser / model_runtime）
- url: 官方地址
- license: 许可证 / 授权方式
- access: 接入方式（无需 token / 需要 token / 商业 API）
- env_key: 用于接入的环境变量名（无需 token 时为空）
- usage: 在本系统中的使用位置与范围
- required: 是否必需（False 表示可降级）
- fallback: 无 key / 不可用时的降级策略
"""
from __future__ import annotations

from typing import Optional


# ===== 登记表 =====
# 顺序与方案文档 §5.3「数据来源与合规披露表」保持一致。
# 新增数据源时追加到末尾，并同步前端 dashboard 与方案文档。

DATA_SOURCES: list[dict] = [
    # ----- 文献检索（论文数据库）-----
    {
        "name": "arXiv",
        "category": "paper_db",
        "url": "https://arxiv.org/",
        "license": "arXiv 使用许可（开放获取，CC-BY 4.0 等学术使用许可）",
        "access": "无需 token",
        "env_key": "",
        "usage": "research 阶段 paper_fetch：候选论文元数据 + 摘要全文检索",
        "required": True,
        "fallback": "不可用时主流程失败（最小依赖），由 S2/Sciverse 补充",
    },
    {
        "name": "Semantic Scholar",
        "category": "paper_db",
        "url": "https://www.semanticscholar.org/",
        "license": "学术使用免费接口（请遵守 S2 API Terms of Use）",
        "access": "无需 token（限速更紧；可选 S2_API_KEY 提升限速）",
        "env_key": "S2_API_KEY",
        "usage": "research 阶段 paper_fetch：补充引用图谱 / venue / 影响力字段",
        "required": False,
        "fallback": "无 key 时直接调用公共 API；调用失败仅 warning，不阻塞",
    },
    {
        "name": "Sciverse",
        "category": "retrieval_api",
        "url": "https://www.sciverse.com/ （赛题推荐智能检索数据库）",
        "license": "需 SCIVERSE_API_TOKEN，凭 token 学术使用",
        "access": "需要 token",
        "env_key": "SCIVERSE_API_TOKEN",
        "usage": "discovery 阶段：证据片段级检索，为构效关系发现提供原文锚点",
        "required": False,
        "fallback": "无 token / 不可用时跳过，evidence_chain 仅来自 arxiv + S2",
    },
    # ----- 材料数据库（赛题路线 A 硬要求）-----
    {
        "name": "Materials Project",
        "category": "material_db",
        "url": "https://next-gen.materialsproject.org/",
        "license": "CC-BY 4.0（学术使用，引用须注明 Materials Project）",
        "access": "需要 token（X-API-KEY）",
        "env_key": "MATERIALS_PROJECT_API_KEY",
        "usage": "discovery 阶段 cross_validate_discovery：按化学式查询 band_gap/density/structure，做构效关系的数据库交叉验证",
        "required": False,
        "fallback": "无 key 时优雅降级到规则交叉验证（_THERMOELECTRIC_KNOWN_RANGES 12 个已知热电体系范围检查）",
    },
    {
        "name": "OQMD",
        "category": "material_db",
        "url": "https://oqmd.org/",
        "license": "CC-BY 4.0（开放量子材料数据库）",
        "access": "无需 token（公共查询接口）",
        "env_key": "",
        "usage": "discovery 阶段：含能/形成能查询，为构效关系提供热力学一致性证据",
        "required": False,
        "fallback": "查询失败仅 warning，不阻塞；规则交叉验证照常运行",
    },
    {
        "name": "Nomad",
        "category": "material_db",
        "url": "https://nomad-lab.eu/",
        "license": "CC-BY 4.0（公开材料数据档案）",
        "access": "无需 token",
        "env_key": "",
        "usage": "discovery 阶段：实验测量值交叉验证（可选源）",
        "required": False,
        "fallback": "查询失败仅 warning，不阻塞",
    },
    # ----- PDF 解析 -----
    {
        "name": "MinerU",
        "category": "parser",
        "url": "https://mineru.net/",
        "license": "学术使用接口（具体见 MinerU 服务条款）",
        "access": "需要 token",
        "env_key": "MINERU_API_TOKEN",
        "usage": "实验上传的论文 PDF 解析（figure / table / section 抽取）",
        "required": False,
        "fallback": "无 token 时仅做简单文本抽取，保留 PDF 上传但深度解析受限",
    },
    # ----- LLM 推理运行时 -----
    {
        "name": "LLM Provider",
        "category": "model_runtime",
        "url": "由 LLMConfig 决定（OpenAI / Anthropic / 本地 vLLM 等）",
        "license": "按 provider 商业条款（如使用闭源模型，按其许可证披露）",
        "access": "需要 key（取决于 provider）",
        "env_key": "由运行时 LLMConfig 注入（OPENAI_API_KEY / ANTHROPIC_API_KEY / 等）",
        "usage": "所有 Agent 节点（LLMGuidedSearch、CrossValidate、ReportGenerator 等）的推理后端",
        "required": True,
        "fallback": "无 key 时 pipeline 进入 dry_run 模式（占位数据，不调用 LLM）",
    },
]


def list_sources() -> list[dict]:
    """返回全部数据源元数据（按登记顺序）。"""
    return list(DATA_SOURCES)


def get_source(name: str) -> Optional[dict]:
    """按名称查找数据源（大小写敏感）。"""
    for src in DATA_SOURCES:
        if src["name"] == name:
            return src
    return None


def summarize() -> dict:
    """汇总统计：用于 dashboard 卡片顶部展示。

    Returns:
        dict 含 total / by_category / required_count / token_required_count
    """
    by_category: dict[str, int] = {}
    required_count = 0
    token_required_count = 0
    for src in DATA_SOURCES:
        cat = src["category"]
        by_category[cat] = by_category.get(cat, 0) + 1
        if src["required"]:
            required_count += 1
        if src["env_key"]:
            token_required_count += 1
    return {
        "total": len(DATA_SOURCES),
        "by_category": by_category,
        "required_count": required_count,
        "token_required_count": token_required_count,
    }


def to_markdown_table() -> str:
    """导出为 Markdown 表格（直接贴入方案文档 §5.3）。"""
    headers = ["名称", "类别", "地址", "许可证", "接入", "环境变量", "必选", "降级"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for src in DATA_SOURCES:
        cells = [
            src["name"],
            src["category"],
            src["url"],
            src["license"],
            src["access"],
            src["env_key"] or "—",
            "✓" if src["required"] else "—",
            src["fallback"],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)