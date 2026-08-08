"""MinerU 开源 PDF 文档解析引擎集成（赛题三·方向三推荐工具）。

MinerU 是 opendatalab 开源的 PDF→结构化内容深度解析工具，支持：
- 多层章节结构（含页码）
- 图/表识别（含 caption 与位置）
- LaTeX 公式抽取
- 参考文献解析
- 多种输出格式（markdown / json / layout）

三种解析模式（优雅降级）：
- api  ：MinerU SaaS API（需 MINERU_API_KEY 环境变量）
- local：本地安装 mineru（如 `pip install magic-pdf`）时通过 subprocess 调用
- fallback：MinerU 不可用时回退到 pypdf 简单文本提取

无 API key / 未安装时 `is_available()` 返回 False，所有调用不抛异常，
调用方应自行检查可用性。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ===========================================================================
# 数据结构
# ===========================================================================


@dataclass
class MinerUSection:
    """结构化章节。"""

    heading: str
    level: int = 1
    page: int = 0
    text: str = ""


@dataclass
class MinerUFigure:
    """结构化图。"""

    caption: str = ""
    page: int = 0
    figure_id: str = ""
    note: str = ""


@dataclass
class MinerUTable:
    """结构化表。"""

    caption: str = ""
    page: int = 0
    table_id: str = ""
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class MinerUDocument:
    """MinerU 解析结果。"""

    doc_id: str
    title: str = ""
    sections: list[MinerUSection] = field(default_factory=list)
    figures: list[MinerUFigure] = field(default_factory=list)
    tables: list[MinerUTable] = field(default_factory=list)
    equations: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    full_text: str = ""
    mode: str = "fallback"  # api / local / fallback
    parse_meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ===========================================================================
# 客户端
# ===========================================================================


class MinerUClient:
    """MinerU 解析客户端。

    使用示例：
        client = MinerUClient()
        if client.is_available():
            doc = client.parse_pdf("paper.pdf")
            print(doc.sections)
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or os.getenv("MINERU_API_KEY", "")
        self.base_url = base_url or os.getenv("MINERU_BASE_URL", "https://api.mineru.net/v1")
        self._mode = self._detect_mode()

    def is_available(self) -> bool:
        """是否可调用 MinerU（API 或本地安装）。"""
        return self._mode in ("api", "local")

    @property
    def mode(self) -> str:
        return self._mode

    def _detect_mode(self) -> str:
        """检测可用的解析模式（api > local > fallback）。"""
        if self.api_key:
            return "api"
        # 检测本地是否安装了 mineru CLI
        if shutil.which("mineru"):
            return "local"
        try:
            import magic_pdf  # noqa: F401
            return "local"
        except ImportError:
            pass
        return "fallback"

    # ----- 主入口 -----

    def parse_pdf(self, pdf_path: str | Path) -> MinerUDocument:
        """解析 PDF 文件，返回结构化文档。

        失败时优雅降级到 fallback 模式（pypdf 简单文本提取）。
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF 文件不存在：{pdf_path}")

        if self._mode == "api":
            try:
                return self._parse_via_api(pdf_path)
            except Exception as e:
                logger.warning("MinerU API 解析失败，降级到 fallback：%s", e)

        if self._mode == "local":
            try:
                return self._parse_via_local(pdf_path)
            except Exception as e:
                logger.warning("MinerU 本地解析失败，降级到 fallback：%s", e)

        # fallback
        return self._parse_fallback(pdf_path)

    # ----- API 模式 -----

    def _parse_via_api(self, pdf_path: Path) -> MinerUDocument:
        """通过 MinerU SaaS API 解析。"""
        # MinerU SaaS API 标准流程：上传文件 → 轮询任务 → 获取结果
        # 此处使用 requests 同步调用；生产环境建议用异步 + 轮询
        try:
            import requests  # type: ignore
        except ImportError:
            raise RuntimeError("需要 requests 库支持 MinerU API 调用")

        # 1. 上传文件
        with open(pdf_path, "rb") as f:
            upload_resp = requests.post(
                f"{self.base_url}/file/upload",
                files={"file": (pdf_path.name, f, "application/pdf")},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=60,
            )
        upload_resp.raise_for_status()
        batch_id = upload_resp.json().get("data", {}).get("batch_id", "")

        # 2. 轮询结果（简单实现：轮询 3 次，每次间隔 5 秒）
        result: Optional[dict] = None
        for _ in range(3):
            import time
            time.sleep(5)
            poll_resp = requests.get(
                f"{self.base_url}/extract/results/batch/{batch_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=60,
            )
            poll_resp.raise_for_status()
            payload = poll_resp.json()
            if payload.get("code") == 200 and payload.get("data"):
                result = payload["data"]
                break

        if not result:
            raise RuntimeError("MinerU API 轮询超时")

        return self._parse_api_result(result, pdf_path)

    def _parse_api_result(self, result: dict, pdf_path: Path) -> MinerUDocument:
        """解析 MinerU API 返回结果。"""
        doc = MinerUDocument(
            doc_id=f"mineru:{pdf_path.stem}",
            title=result.get("title", pdf_path.stem),
            mode="api",
            parse_meta={"source": "mineru_api", "path": str(pdf_path)},
        )

        # 解析 sections
        for sec in result.get("sections", []):
            doc.sections.append(MinerUSection(
                heading=sec.get("heading", ""),
                level=int(sec.get("level", 1)),
                page=int(sec.get("page", 0)),
                text=sec.get("text", ""),
            ))

        # 解析 figures
        for fig in result.get("figures", []):
            doc.figures.append(MinerUFigure(
                caption=fig.get("caption", ""),
                page=int(fig.get("page", 0)),
                figure_id=fig.get("id", ""),
            ))

        # 解析 tables
        for tbl in result.get("tables", []):
            doc.tables.append(MinerUTable(
                caption=tbl.get("caption", ""),
                page=int(tbl.get("page", 0)),
                table_id=tbl.get("id", ""),
                rows=tbl.get("rows", []),
            ))

        # equations & references
        doc.equations = result.get("equations", [])
        doc.references = result.get("references", [])

        # full_text：拼接所有 section 文本
        text_parts: list[str] = []
        if doc.title:
            text_parts.append(f"# {doc.title}\n")
        for sec in doc.sections:
            text_parts.append(f"\n## {sec.heading}\n{sec.text}")
        doc.full_text = "\n".join(text_parts)

        return doc

    # ----- Local 模式 -----

    def _parse_via_local(self, pdf_path: Path) -> MinerUDocument:
        """通过本地 MinerU CLI 解析。"""
        output_dir = pdf_path.parent / f".mineru_{pdf_path.stem}"
        output_dir.mkdir(exist_ok=True)

        # 调用 mineru CLI
        cli = "mineru" if shutil.which("mineru") else "magic-pdf"
        cmd = [
            cli,
            "-p", str(pdf_path),
            "-o", str(output_dir),
            "-m", "auto",
        ]
        logger.info("调用本地 MinerU CLI：%s", " ".join(cmd))
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(f"MinerU CLI 失败：{result.stderr[:200]}")

        # 解析输出（假设输出 markdown）
        md_file = output_dir / f"{pdf_path.stem}.md"
        if not md_file.exists():
            # 尝试其他位置
            md_files = list(output_dir.glob("*.md"))
            if md_files:
                md_file = md_files[0]
            else:
                raise RuntimeError("MinerU CLI 输出无 markdown 文件")

        md_text = md_file.read_text(encoding="utf-8", errors="ignore")
        return self._parse_markdown_to_doc(md_text, pdf_path, mode="local")

    # ----- Fallback 模式 -----

    def _parse_fallback(self, pdf_path: Path) -> MinerUDocument:
        """Fallback：使用 pypdf 简单文本提取。

        不进行深度结构化，仅返回全文 + 按页切分 sections。
        """
        try:
            import pypdf
            reader = pypdf.PdfReader(str(pdf_path))
        except ImportError:
            logger.warning("pypdf 未安装，无法 fallback 解析")
            return MinerUDocument(
                doc_id=f"mineru:{pdf_path.stem}",
                title=pdf_path.stem,
                mode="fallback",
                parse_meta={"error": "pypdf 未安装"},
            )

        sections: list[MinerUSection] = []
        full_parts: list[str] = []

        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            # 按页作为最小 section
            sec = MinerUSection(
                heading=f"Page {i + 1}",
                level=2,
                page=i + 1,
                text=text,
            )
            sections.append(sec)
            full_parts.append(text)

        # 尝试从首页抽取标题（首行非空文本）
        title = ""
        if sections and sections[0].text:
            first_line = sections[0].text.strip().split("\n")[0].strip()
            if 5 <= len(first_line) <= 200:
                title = first_line

        return MinerUDocument(
            doc_id=f"mineru:{pdf_path.stem}",
            title=title or pdf_path.stem,
            sections=sections,
            mode="fallback",
            full_text="\n".join(full_parts),
            parse_meta={"source": "pypdf_fallback", "path": str(pdf_path)},
        )

    # ----- 工具方法 -----

    def _parse_markdown_to_doc(
        self, md_text: str, pdf_path: Path, mode: str
    ) -> MinerUDocument:
        """从 MinerU CLI 输出的 markdown 解析为 MinerUDocument。"""
        import re

        sections: list[MinerUSection] = []
        figures: list[MinerUFigure] = []
        tables: list[MinerUTable] = []
        equations: list[str] = []
        references: list[str] = []

        current_section: Optional[MinerUSection] = None
        lines = md_text.split("\n")

        for line in lines:
            stripped = line.strip()

            # 标题识别：# / ## / ### ...
            m = re.match(r"^(#{1,6})\s+(.+)$", stripped)
            if m:
                level = len(m.group(1))
                heading = m.group(2).strip()
                current_section = MinerUSection(heading=heading, level=level, text="")
                sections.append(current_section)
                continue

            # 图识别：![xxx](yyy) 或 **Figure N:** ...
            if re.match(r"^!\[" , stripped) or "**Figure" in stripped:
                figures.append(MinerUFigure(caption=stripped[:300]))
                continue

            # 表识别：含 |---  或 **Table
            if "|---" in stripped or "**Table" in stripped:
                tables.append(MinerUTable(caption=stripped[:200]))
                continue

            # 公式识别：$$...$$
            if stripped.startswith("$$") and stripped.endswith("$$"):
                equations.append(stripped.strip("$"))
                continue

            # 参考文献：^[1] 或 1. xxx
            if re.match(r"^\[\d+\]", stripped) or re.match(r"^\d+\.\s+[A-Z]", stripped):
                references.append(stripped[:300])

            # 累积到当前 section
            if current_section:
                current_section.text += line + "\n"

        # 抽取标题（首个 # 标题）
        title = ""
        for s in sections:
            if s.level == 1 and s.heading:
                title = s.heading
                break
        if not title:
            title = pdf_path.stem

        return MinerUDocument(
            doc_id=f"mineru:{pdf_path.stem}",
            title=title,
            sections=sections,
            figures=figures,
            tables=tables,
            equations=equations,
            references=references,
            full_text=md_text,
            mode=mode,
            parse_meta={"source": f"mineru_{mode}", "path": str(pdf_path)},
        )


# ===========================================================================
# 模块级便捷函数
# ===========================================================================


_default_client: Optional[MinerUClient] = None


def _get_default_client() -> MinerUClient:
    global _default_client
    if _default_client is None:
        _default_client = MinerUClient()
    return _default_client


def mineru_is_available() -> bool:
    """模块级：MinerU 是否可用（API 或本地安装）。"""
    return _get_default_client().is_available()


def parse_pdf_with_mineru(pdf_path: str | Path) -> MinerUDocument:
    """模块级：解析 PDF，返回结构化文档（不可用时降级 fallback）。"""
    return _get_default_client().parse_pdf(pdf_path)