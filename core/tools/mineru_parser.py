"""MinerU 文档解析引擎集成（赛题推荐工具）。

赛题方向三明确推荐：「MinerU（开源文档解析引擎，支持 PDF 到结构化内容的深度解析）」。
本工具提供：
1. MinerU Python API / CLI 接入（可选，需安装 mineru 包）
   - 安装：pip install mineru 或 uv pip install -U "mineru[all]"
   - GitHub: https://github.com/opendatalab/MinerU
2. 无 MinerU 时的优雅降级（返回原始文本，不阻塞流程）

MinerU 能力：
- PDF → Markdown + JSON（含表格 HTML、公式 LaTeX、阅读顺序）
- 支持 109 种语言 OCR
- 跨页表格合并、页眉页脚去除
- 输入：PDF / 图片 / DOCX / PPTX
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ParsedDocument:
    """MinerU 解析后的结构化文档。"""

    markdown: str = ""               # 完整 Markdown 文本（含表格 HTML、公式 LaTeX）
    tables: list[str] = field(default_factory=list)  # 表格 HTML 列表
    formulas: list[str] = field(default_factory=list)  # 公式 LaTeX 列表
    chunks: list[str] = field(default_factory=list)  # 按段落切分的文本块
    source: str = "unknown"          # mineru / cli / fallback
    raw_json: Optional[dict] = None  # MinerU 原始 JSON 输出


def is_available() -> bool:
    """检查 MinerU 是否可用（Python API 或 CLI）。"""
    # 检查 Python API
    try:
        import mineru  # noqa: F401
        return True
    except ImportError:
        pass
    # 检查 CLI
    try:
        result = subprocess.run(
            ["mineru", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def parse_pdf(pdf_path: str | Path) -> ParsedDocument:
    """解析 PDF 文件为结构化文档。

    优先使用 MinerU Python API，其次 CLI，最后降级为纯文本提取。

    Args:
        pdf_path: PDF 文件路径

    Returns:
        ParsedDocument
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.warning("PDF 文件不存在: %s", pdf_path)
        return ParsedDocument(source="fallback")

    # 尝试 Python API
    doc = _parse_via_python_api(pdf_path)
    if doc is not None:
        return doc

    # 尝试 CLI
    doc = _parse_via_cli(pdf_path)
    if doc is not None:
        return doc

    # 降级：用 PyMuPDF 提取纯文本
    return _parse_via_fallback(pdf_path)


def _parse_via_python_api(pdf_path: Path) -> Optional[ParsedDocument]:
    """通过 MinerU Python API 解析。"""
    try:
        from mineru import DocumentParser
    except ImportError:
        return None

    try:
        parser = DocumentParser(enable_table_merge=True)
        result = parser.parse(str(pdf_path))

        # 提取表格与公式
        tables = []
        formulas = []
        if hasattr(result, "tables"):
            for t in result.tables:
                try:
                    tables.append(t.to_html())
                except Exception:
                    tables.append(str(t))
        if hasattr(result, "equations"):
            for eq in result.equations:
                try:
                    formulas.append(eq.latex)
                except Exception:
                    formulas.append(str(eq))

        # 获取 Markdown 文本
        markdown = ""
        if hasattr(result, "markdown"):
            markdown = result.markdown
        elif hasattr(result, "content"):
            markdown = result.content
        elif hasattr(result, "text"):
            markdown = result.text

        # 按段落切分
        chunks = [c.strip() for c in markdown.split("\n\n") if c.strip()]

        return ParsedDocument(
            markdown=markdown,
            tables=tables,
            formulas=formulas,
            chunks=chunks,
            source="mineru",
        )
    except Exception as e:
        logger.warning("MinerU Python API 解析失败: %s", e)
        return None


def _parse_via_cli(pdf_path: Path) -> Optional[ParsedDocument]:
    """通过 MinerU CLI 解析。"""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["mineru", "-p", str(pdf_path), "-o", tmpdir],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                logger.warning("MinerU CLI 返回码 %d: %s", result.returncode, result.stderr[:200])
                return None

            # 查找输出文件（MinerU 输出 .md 和 .json）
            md_files = list(Path(tmpdir).rglob("*.md"))
            json_files = list(Path(tmpdir).rglob("*.json"))

            markdown = ""
            raw_json = None

            if md_files:
                markdown = md_files[0].read_text(encoding="utf-8")
            if json_files:
                import json
                try:
                    raw_json = json.loads(json_files[0].read_text(encoding="utf-8"))
                except Exception:
                    pass

            if not markdown:
                return None

            chunks = [c.strip() for c in markdown.split("\n\n") if c.strip()]

            # 从 Markdown 中提取表格（HTML <table> 块）
            tables = _extract_tables_from_md(markdown)
            # 从 Markdown 中提取公式（$...$ 或 $$...$$）
            formulas = _extract_formulas_from_md(markdown)

            return ParsedDocument(
                markdown=markdown,
                tables=tables,
                formulas=formulas,
                chunks=chunks,
                source="cli",
                raw_json=raw_json,
            )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        logger.warning("MinerU CLI 解析超时（300s）")
        return None
    except Exception as e:
        logger.warning("MinerU CLI 解析失败: %s", e)
        return None


def _parse_via_fallback(pdf_path: Path) -> ParsedDocument:
    """降级方案：用 PyMuPDF 提取纯文本。"""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()

        markdown = "\n\n".join(text_parts)
        chunks = [c.strip() for c in markdown.split("\n\n") if c.strip()]

        return ParsedDocument(
            markdown=markdown,
            tables=[],
            formulas=[],
            chunks=chunks,
            source="fallback",
        )
    except Exception as e:
        logger.warning("PyMuPDF 降级解析失败: %s", e)
        return ParsedDocument(source="fallback")


def _extract_tables_from_md(markdown: str) -> list[str]:
    """从 Markdown 中提取 HTML 表格块。"""
    tables = []
    import re
    # 匹配 <table>...</table>
    for m in re.finditer(r"<table[^>]*>.*?</table>", markdown, re.DOTALL | re.IGNORECASE):
        tables.append(m.group())
    # 匹配 Markdown 管道表格
    for m in re.finditer(r"(\|[^\n]+\|\n)+", markdown):
        tables.append(m.group().strip())
    return tables


def _extract_formulas_from_md(markdown: str) -> list[str]:
    """从 Markdown 中提取 LaTeX 公式。"""
    import re
    formulas = []
    # $$...$$ 块公式
    for m in re.finditer(r"\$\$(.+?)\$\$", markdown, re.DOTALL):
        formulas.append(m.group(1).strip())
    # $...$ 行内公式（非贪婪，排除 $$）
    for m in re.finditer(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", markdown):
        formulas.append(m.group(1).strip())
    return formulas
