"""科研 Agent 系统 Web API。

启动方式：
    python -m web.api
    # 或：uvicorn web.api:app --host 0.0.0.0 --port 8000

提供 REST 接口供前端单页应用调用，所有项目状态存内存（Demo 用，不持久化）。
Pipeline 在独立线程中异步执行，人工节点通过 HumanCallbackBridge 桥接到 REST 接口。
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 确保项目根在 sys.path（python -m web.api 已保证，但直接运行脚本时需补充）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 启动前加载 .env
from runtime.cli import _load_env  # noqa: E402

_load_env()

from fastapi import FastAPI, HTTPException, UploadFile, File, Form  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from core.config import get_config  # noqa: E402
from core.knowledge import KnowledgeStore  # noqa: E402
from core.orchestration.node import HumanRequest, HumanResponse  # noqa: E402
from core.state.lifecycle import LifecycleStage  # noqa: E402
from core.state.session import ProjectSession  # noqa: E402
from runtime.pipeline import Pipeline, PipelineResult  # noqa: E402


# ===== 请求/响应模型 =====


class CreateProjectRequest(BaseModel):
    topic: str


class RunProjectRequest(BaseModel):
    force_writing: bool = False


class NoteRequest(BaseModel):
    text: str


class HumanResponseRequest(BaseModel):
    action: str = "continue"  # continue / abort / rollback
    text: Optional[str] = None
    selected_option: Optional[str] = None


# ===== 人工节点桥接 =====


class HumanCallbackBridge:
    """跨线程的人工节点桥接。

    Pipeline 在工作线程中执行，遇到 HumanNode 时调用 callback 阻塞等待；
    主线程的 REST 接口接收用户响应后唤醒 callback，让 Pipeline 继续。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # project_id -> 等待中的人工请求描述（dict 形式，便于序列化）
        self._pending: dict[str, dict[str, Any]] = {}
        # project_id -> threading.Event，用于阻塞 callback
        self._events: dict[str, threading.Event] = {}
        # project_id -> HumanResponse，由 REST 接口注入
        self._responses: dict[str, HumanResponse] = {}

    def make_callback(self, project_id: str):
        def _cb(req: HumanRequest) -> HumanResponse:
            event = threading.Event()
            payload = {
                "prompt": req.prompt,
                "options": list(req.options) if req.options else [],
                "allow_free_text": req.allow_free_text,
                "context": dict(req.context) if req.context else {},
                "appeared_at": datetime.now().isoformat(),
            }
            with self._lock:
                self._pending[project_id] = payload
                self._events[project_id] = event
            # 阻塞直到 REST 接口提交响应或超时
            triggered = event.wait(timeout=3600)
            with self._lock:
                resp = self._responses.pop(project_id, None)
                self._pending.pop(project_id, None)
                self._events.pop(project_id, None)
            if not triggered or resp is None:
                return HumanResponse(action="abort")
            return resp

        return _cb

    def submit(self, project_id: str, response: HumanResponse) -> bool:
        """注入响应并唤醒 callback。返回是否成功（存在等待中的请求）。"""
        with self._lock:
            event = self._events.get(project_id)
            if event is None:
                return False
            self._responses[project_id] = response
        event.set()
        return True

    def get_pending(self, project_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            req = self._pending.get(project_id)
            return dict(req) if req else None

    def clear(self, project_id: str) -> None:
        with self._lock:
            self._pending.pop(project_id, None)
            self._responses.pop(project_id, None)
            event = self._events.pop(project_id, None)
        if event is not None:
            event.set()


# ===== 项目状态 =====


@dataclass
class ProjectState:
    project_id: str
    topic: str
    created_at: str
    status: str = "created"  # created / running / pending_human / completed / failed / aborted
    summary: str = ""
    last_result: Optional[PipelineResult] = None
    error: Optional[str] = None
    thread: Optional[threading.Thread] = None
    node_history: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    recommendation: str = ""
    # 路线 A：构效关系发现产出（从 discovery 子图 context 收集）
    discovery: dict = field(default_factory=dict)
    # 当前运行模式：pipeline / discovery
    run_mode: str = ""


# ===== 全局状态 =====

_CONFIG = get_config()
_PROJECTS: dict[str, ProjectState] = {}
_BRIDGE = HumanCallbackBridge()
_LOCK = threading.Lock()


# ===== Pipeline 工作线程 =====


def _run_pipeline_thread(project_id: str, topic: str, resume: bool, force_writing: bool = False) -> None:
    """工作线程函数：执行 Pipeline 并更新项目状态。"""
    state = _PROJECTS.get(project_id)
    if state is None:
        return
    try:
        state.status = "running"
        pipeline = Pipeline(config=_CONFIG)
        # dry_run 模式下也使用桥接回调，让用户能介入；非 dry_run 同样使用桥接
        human_cb = _BRIDGE.make_callback(project_id)
        result: PipelineResult = pipeline.run_pipeline(
            project_id=project_id,
            topic=topic,
            human_callback=human_cb,
            resume=resume,
            force_writing=force_writing,
        )
        state.last_result = result
        state.status = result.status
        state.summary = result.summary
        state.recommendation = result.recommendation
        state.node_history = result.node_history or []
    except Exception as e:  # noqa: BLE001
        state.status = "failed"
        state.error = f"{type(e).__name__}: {e}"
        state.summary = f"Pipeline 执行异常: {e}"


def _run_discovery_thread(project_id: str, topic: str, resume: bool) -> None:
    """工作线程函数：执行构效关系发现（路线 A）并更新项目状态。"""
    state = _PROJECTS.get(project_id)
    if state is None:
        return
    try:
        state.status = "running"
        state.run_mode = "discovery"
        pipeline = Pipeline(config=_CONFIG)
        human_cb = _BRIDGE.make_callback(project_id)
        result: PipelineResult = pipeline.run_discovery(
            project_id=project_id,
            topic=topic,
            human_callback=human_cb,
            resume=resume,
        )
        state.last_result = result
        state.status = result.status
        state.summary = result.summary
        state.recommendation = result.recommendation
        state.node_history = result.node_history or []
        # 从 node_history 提取 discovery 产出摘要
        state.discovery = _extract_discovery_summary(result.node_history)
    except Exception as e:  # noqa: BLE001
        state.status = "failed"
        state.error = f"{type(e).__name__}: {e}"
        state.summary = f"Discovery 执行异常: {e}"


def _extract_discovery_summary(node_history: list[dict]) -> dict:
    """从节点历史提取 discovery 产出摘要。"""
    summary = {"hypotheses": 0, "candidates": 0, "relationships": 0, "novel": 0, "nodes": []}
    for h in node_history or []:
        node_id = h.get("node_id", "")
        if node_id in ("hypothesis_seed", "search_space", "llm_guided_search",
                        "discovery_validate", "discovery_report"):
            summary["nodes"].append({
                "node_id": node_id,
                "status": h.get("status"),
                "summary": h.get("summary", ""),
            })
            if node_id == "hypothesis_seed" and "个候选构效关系假设" in h.get("summary", ""):
                try:
                    summary["hypotheses"] = int(h["summary"].split("生成")[1].split("个")[0])
                except (IndexError, ValueError):
                    pass
            if node_id == "discovery_validate" and "条验证发现" in h.get("summary", ""):
                try:
                    summary["relationships"] = int(
                        h["summary"].split("验证")[1].split("条")[0])
                    if "条 novel" in h["summary"]:
                        summary["novel"] = int(
                            h["summary"].split("其中 ")[1].split(" 条")[0])
                except (IndexError, ValueError):
                    pass
    return summary


# ===== FastAPI 应用 =====


app = FastAPI(title="科研 Agent 系统", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


STATIC_DIR = Path(__file__).resolve().parent / "static"


# ===== 接口实现 =====


@app.post("/api/projects")
def create_project(req: CreateProjectRequest) -> dict:
    """创建新项目，返回 project_id。"""
    topic = (req.topic or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic 不能为空")
    project_id = f"proj_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    state = ProjectState(
        project_id=project_id,
        topic=topic,
        created_at=datetime.now().isoformat(),
    )
    with _LOCK:
        _PROJECTS[project_id] = state
    return {"project_id": project_id, "topic": topic, "created_at": state.created_at}


@app.get("/api/projects")
def list_projects() -> dict:
    """列出所有项目（供前端刷新后恢复）。"""
    with _LOCK:
        items = [
            {
                "project_id": s.project_id,
                "topic": s.topic,
                "created_at": s.created_at,
                "status": s.status,
                "summary": s.summary,
            }
            for s in _PROJECTS.values()
        ]
    # 按创建时间降序
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"projects": items}


@app.get("/api/projects/{project_id}/status")
def get_status(project_id: str) -> dict:
    state = _require_project(project_id)
    # 当前阶段：从 session 读取（若已启动）
    current_stage = ""
    stage_statuses: dict[str, str] = {}
    try:
        session = ProjectSession.load(project_id, _CONFIG.paths)
        current_stage = session.current_stage().value
        for stage in LifecycleStage.ordered():
            stage_statuses[stage.value] = session.status_of(stage).value
    except Exception:  # noqa: BLE001
        pass

    # 产出物计数
    counts = {"papers": 0, "ideas": 0, "claims": 0, "experiments": 0}
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        counts["papers"] = len(store.list_papers())
        counts["ideas"] = len(store.list_ideas())
        counts["claims"] = len(store.list_claims())
        counts["experiments"] = len(store.list_experiments())
    except Exception:  # noqa: BLE001
        pass

    pending = _BRIDGE.get_pending(project_id)

    return {
        "project_id": project_id,
        "topic": state.topic,
        "created_at": state.created_at,
        "status": state.status,
        "summary": state.summary,
        "recommendation": state.recommendation,
        "error": state.error,
        "current_stage": current_stage,
        "stage_statuses": stage_statuses,
        "node_history": state.node_history,
        "counts": counts,
        "pending_human": pending,
    }


@app.post("/api/projects/{project_id}/run")
def run_project(project_id: str, req: Optional[RunProjectRequest] = None) -> dict:
    """启动/继续 pipeline（异步执行，立即返回）。

    可选 body: {"force_writing": true} —— 实验失败后强制进入论文写作阶段。
    """
    state = _require_project(project_id)
    if state.status == "running":
        raise HTTPException(status_code=409, detail="pipeline 正在运行中")
    if state.status == "pending_human":
        raise HTTPException(
            status_code=409,
            detail="当前等待人工响应，请先提交 human-response 或中止",
        )
    resume = state.status not in ("created",)
    force_writing = (req.force_writing if req else False)
    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(project_id, state.topic, resume, force_writing),
        daemon=True,
    )
    state.thread = thread
    state.error = None
    thread.start()
    msg = "pipeline 已启动"
    if force_writing:
        msg = "pipeline 已启动（强制写作模式：绕过实验成败判断）"
    return {"project_id": project_id, "message": msg, "resumed": resume, "force_writing": force_writing}


@app.post("/api/projects/{project_id}/run-discovery")
def run_discovery(project_id: str) -> dict:
    """启动构效关系发现（路线 A）：research → discovery 子图。

    异步执行，立即返回。结果通过 /status 与 /discoveries 接口查询。
    """
    state = _require_project(project_id)
    if state.status == "running":
        raise HTTPException(status_code=409, detail="任务正在运行中")
    if state.status == "pending_human":
        raise HTTPException(
            status_code=409,
            detail="当前等待人工响应，请先提交 human-response 或中止",
        )
    resume = state.status not in ("created",)
    thread = threading.Thread(
        target=_run_discovery_thread,
        args=(project_id, state.topic, resume),
        daemon=True,
    )
    state.thread = thread
    state.error = None
    thread.start()
    return {"project_id": project_id, "message": "discovery 已启动", "resumed": resume}


@app.get("/api/projects/{project_id}/discoveries")
def get_discoveries(project_id: str) -> dict:
    """获取构效关系发现产出（路线 A）。"""
    state = _require_project(project_id)
    # 从 KnowledgeStore 读取 discovery 阶段产出的 Claim（构效关系发现）
    relationships: list[dict] = []
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        for c in store.list_claims():
            if c.source_stage == "discovery":
                relationships.append({
                    "claim_id": c.claim_id,
                    "statement": c.statement,
                    "status": c.status.value,
                    "evidence_refs": c.evidence_refs,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                })
    except Exception as e:  # noqa: BLE001
        pass
    return {
        "project_id": project_id,
        "discovery_summary": state.discovery,
        "relationships": relationships,
        "run_mode": state.run_mode,
    }


@app.get("/api/projects/{project_id}/papers")
def list_papers(project_id: str) -> dict:
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        papers = store.list_papers()
    except Exception as e:  # noqa: BLE001
        return {"papers": [], "error": f"读取失败: {e}"}
    return {
        "papers": [
            {
                "paper_id": p.paper_id,
                "title": p.title,
                "authors": p.authors,
                "year": p.year,
                "venue": p.venue,
                "arxiv_id": p.arxiv_id,
                "doi": p.doi,
                "abstract": p.abstract,
                "url": p.url or (f"https://arxiv.org/abs/{p.arxiv_id}" if p.arxiv_id else None),
                "doi_url": (f"https://doi.org/{p.doi}" if p.doi else None),
                "pdf_path": p.pdf_path,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "source_stage": p.source_stage,
            }
            for p in papers
        ]
    }


@app.get("/api/projects/{project_id}/claims")
def list_claims(project_id: str, status: Optional[str] = None) -> dict:
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        claims = store.list_claims()
    except Exception as e:  # noqa: BLE001
        return {"claims": [], "error": f"读取失败: {e}"}
    items = []
    for c in claims:
        item = {
            "claim_id": c.claim_id,
            "statement": c.statement,
            "status": c.status.value,
            "role": c.role,
            "evidence_count": len(c.evidence_refs),
            "evidence_refs": c.evidence_refs,
            "source_idea_id": c.source_idea_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "verified_at": c.verified_at.isoformat() if c.verified_at else None,
        }
        items.append(item)
    if status:
        items = [it for it in items if it["status"] == status]
    return {"claims": items}


@app.get("/api/projects/{project_id}/experiments")
def list_experiments(project_id: str) -> dict:
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        exps = store.list_experiments()
    except Exception as e:  # noqa: BLE001
        return {"experiments": [], "error": f"读取失败: {e}"}
    return {
        "experiments": [
            {
                "experiment_id": e.experiment_id,
                "name": e.name,
                "status": e.status.value,
                "verifies_claim_ids": e.verifies_claim_ids,
                "config": e.config,
                "result_summary": e.result_summary,
                "anomaly_notes": e.anomaly_notes,
                "started_at": e.started_at.isoformat() if e.started_at else None,
                "completed_at": e.completed_at.isoformat() if e.completed_at else None,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in exps
        ]
    }


@app.post("/api/projects/{project_id}/notes")
def add_note(project_id: str, req: NoteRequest) -> dict:
    state = _require_project(project_id)
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    note = {
        "note_id": uuid.uuid4().hex,
        "text": text,
        "created_at": datetime.now().isoformat(),
    }
    state.notes.append(note)
    return {"note": note}


@app.get("/api/projects/{project_id}/notes")
def list_notes(project_id: str) -> dict:
    state = _require_project(project_id)
    return {"notes": list(state.notes)}


@app.post("/api/projects/{project_id}/human-response")
def submit_human_response(project_id: str, req: HumanResponseRequest) -> dict:
    state = _require_project(project_id)
    action = req.action or "continue"
    if action not in ("continue", "abort", "rollback"):
        raise HTTPException(status_code=400, detail="action 必须为 continue/abort/rollback")
    resp = HumanResponse(
        text=req.text,
        selected_option=req.selected_option,
        action=action,
    )
    ok = _BRIDGE.submit(project_id, resp)
    if not ok:
        # 没有等待中的人工请求，仍记录状态便于前端展示
        state.summary = "未找到等待中的人工请求，响应已忽略"
        return {"project_id": project_id, "submitted": False, "message": "无等待中的人工请求"}
    return {"project_id": project_id, "submitted": True, "action": action}


# ===== 赛题对齐展示接口 =====


@app.get("/api/projects/{project_id}/research-report")
def get_research_report(project_id: str) -> dict:
    """文献调研报告（赛题基本任务核心产出）。

    返回 cross_validation_report，含 Research Gaps / 共识 / 冲突 / 整体置信度。
    """
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        cv_report = store.get_kv("cross_validation_report")
    except Exception as e:  # noqa: BLE001
        return {"report": None, "error": f"读取失败: {e}"}
    if not cv_report:
        return {
            "report": None,
            "message": "尚未生成调研报告，请先运行 research 阶段（启动 Pipeline 或构效关系发现）",
        }
    # 补充论文计数
    try:
        paper_count = len(store.list_papers())
    except Exception:  # noqa: BLE001
        paper_count = 0
    return {
        "project_id": project_id,
        "report": cv_report,
        "paper_count": paper_count,
    }


@app.get("/api/projects/{project_id}/discovery-detail")
def get_discovery_detail(project_id: str) -> dict:
    """构效关系发现详细数据（路线 A 完整产出）。

    返回：
    - discovery_summary: 计数卡片
    - discovery_hypotheses: 假设列表
    - discovery_search_space: 搜索空间定义
    - discovery_relationships: 验证后的构效关系（含交叉验证结果）
    - discovery_search_trace: MCTS 搜索轨迹（前端可视化）
    - discovery_literature_points: 文献数据点（前端散点图）
    - discovery_report_content: Markdown 报告
    - materials_cross_validation_report: Materials Project 交叉验证报告
    """
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        kv = store.list_kv()
    except Exception as e:  # noqa: BLE001
        return {"error": f"读取失败: {e}"}
    # 也补充 Claim 形式的发现（兼容旧项目）
    relationships_from_claims: list[dict] = []
    try:
        for c in store.list_claims():
            if c.source_stage == "discovery":
                relationships_from_claims.append({
                    "claim_id": c.claim_id,
                    "statement": c.statement,
                    "status": c.status.value,
                    "evidence_refs": c.evidence_refs,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                })
    except Exception:  # noqa: BLE001
        pass
    kv_relationships = kv.get("discovery_relationships", [])
    # 优先用 KV 完整版（含 cross_validation 字段），无则用 Claim 版
    relationships = kv_relationships if kv_relationships else relationships_from_claims
    return {
        "project_id": project_id,
        "discovery_summary": kv.get("discovery_summary", {}),
        "discovery_hypotheses": kv.get("discovery_hypotheses", []),
        "discovery_search_space": kv.get("discovery_search_space", {}),
        "discovery_relationships": relationships,
        "discovery_search_trace": kv.get("discovery_search_trace", {}),
        "discovery_literature_points": kv.get("discovery_literature_points", []),
        "discovery_report_content": kv.get("discovery_report_content", ""),
        "discovery_report_artifact_id": kv.get("discovery_report_artifact_id", ""),
        "materials_cross_validation_report": kv.get("materials_cross_validation_report", {}),
    }


@app.get("/api/projects/{project_id}/materials-cross-validation")
def get_materials_cross_validation(project_id: str) -> dict:
    """Materials Project 交叉验证报告（赛题路线 A 硬要求）。"""
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        report = store.get_kv("materials_cross_validation_report")
    except Exception as e:  # noqa: BLE001
        return {"report": None, "error": f"读取失败: {e}"}
    if not report:
        return {
            "report": None,
            "message": "尚未生成交叉验证报告，请先运行构效关系发现",
        }
    return {"project_id": project_id, "report": report}


@app.get("/api/projects/{project_id}/method-alignment")
def get_method_alignment(project_id: str) -> dict:
    """方法↔代码对齐（公式 LaTeX ↔ 实验代码关键词匹配）。

    从方法 Artifact 抽取 LaTeX 公式，与实验代码做关键词匹配，
    标注 mapped / partial / missing。
    """
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        # 方法 Artifact
        method_content = ""
        method_artifacts: list[dict] = []
        for art in store.list_artifact_versions("") if hasattr(store, "list_artifact_versions") else []:
            method_artifacts.append({"artifact_id": art.artifact_id, "title": art.title})
        # 简化版：扫描所有 METHOD_DOC 类型 Artifact
        try:
            with store._connect() as conn:  # type: ignore[attr-defined]
                rows = conn.execute(
                    "SELECT artifact_id, content FROM artifacts WHERE artifact_type = 'method_doc' ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
                method_artifacts = [
                    {"artifact_id": r["artifact_id"], "content": r["content"][:5000]}
                    for r in rows
                ]
                if rows:
                    method_content = rows[0]["content"]
        except Exception:  # noqa: BLE001
            pass
        # 实验代码（取最近实验的 config，可能含代码片段）
        experiments = store.list_experiments()
        experiment_code_snippets: list[dict] = []
        for exp in experiments[:5]:
            experiment_code_snippets.append({
                "experiment_id": exp.experiment_id,
                "name": exp.name,
                "status": exp.status.value,
                "config_keys": list(exp.config.keys()) if exp.config else [],
                "result_summary": exp.result_summary,
            })
    except Exception as e:  # noqa: BLE001
        return {"error": f"读取失败: {e}"}

    # 简化版对齐：从 method_content 抽取 LaTeX 公式标记
    import re
    formulas: list[dict] = []
    if method_content:
        # 抽取 $$...$$ / \[...\] / equation 环境的公式
        formula_patterns = [
            (r"\$\$(.+?)\$\$", "display"),
            (r"\\\[(.+?)\\\]", "display"),
            (r"\\begin\{equation\}(.+?)\\end\{equation\}", "display"),
            (r"\$([^$\n]+?)\$", "inline"),
        ]
        seen = set()
        for pat, kind in formula_patterns:
            for m in re.finditer(pat, method_content, re.DOTALL):
                f = m.group(1).strip()
                if f and f not in seen and len(f) < 500:
                    seen.add(f)
                    # 关键词匹配：在实验代码片段中查找公式变量
                    keywords = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", f)
                    keywords = [k for k in keywords if len(k) > 2 and k not in
                                ("frac", "sum", "int", "mathrm", "text", "left", "right",
                                 "begin", "end", "alpha", "beta", "gamma", "delta", "theta",
                                 "lambda", "mu", "sigma", "phi", "psi", "omega")]
                    matched_kw = []
                    for kw in keywords[:10]:
                        for exp in experiments:
                            cfg_str = str(exp.config or "")
                            if kw in cfg_str:
                                matched_kw.append(kw)
                                break
                    status = "mapped" if matched_kw else (
                        "partial" if keywords else "missing"
                    )
                    formulas.append({
                        "formula": f,
                        "kind": kind,
                        "keywords": keywords[:10],
                        "matched_keywords": matched_kw,
                        "alignment_status": status,
                    })
    return {
        "project_id": project_id,
        "method_artifacts": method_artifacts,
        "method_content_preview": method_content[:2000] if method_content else "",
        "experiment_code_snippets": experiment_code_snippets,
        "formulas": formulas,
        "alignment_summary": {
            "total_formulas": len(formulas),
            "mapped": sum(1 for f in formulas if f["alignment_status"] == "mapped"),
            "partial": sum(1 for f in formulas if f["alignment_status"] == "partial"),
            "missing": sum(1 for f in formulas if f["alignment_status"] == "missing"),
        },
    }


@app.get("/api/projects/{project_id}/dashboard")
def get_dashboard(project_id: str) -> dict:
    """首页 Dashboard 聚合数据（赛题对齐展示）。

    一次返回所有计数 + 调研报告摘要 + 发现摘要 + 交叉验证摘要，
    让前端 Dashboard 单次拉取即可渲染。
    """
    state = _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        counts = {
            "papers": len(store.list_papers()),
            "ideas": len(store.list_ideas()),
            "claims": len(store.list_claims()),
            "experiments": len(store.list_experiments()),
            "discovery_claims": sum(
                1 for c in store.list_claims() if c.source_stage == "discovery"
            ),
        }
        cv_report = store.get_kv("cross_validation_report") or {}
        discovery_summary = store.get_kv("discovery_summary") or {}
        materials_cv = store.get_kv("materials_cross_validation_report") or {}
    except Exception as e:  # noqa: BLE001
        return {"error": f"读取失败: {e}"}

    # 方法对齐摘要（公式数）
    method_alignment_summary = {"total_formulas": 0}
    try:
        from core.artifacts import ArtifactManager as _AM
        from core.knowledge import ArtifactType
        am = _AM(_CONFIG.paths.project_db(project_id))
        # 统计 FORMULA + METHOD_DOC 类型产出
        total_formulas = 0
        for at in (ArtifactType.FORMULA, ArtifactType.METHOD_DOC):
            try:
                arts = am.list_artifacts(artifact_type=at)
                for a in arts:
                    content = a.content or {}
                    if isinstance(content, dict):
                        formulas = content.get("formula_code_map") or content.get("formulas") or []
                        total_formulas += len(formulas) if isinstance(formulas, list) else 0
                    else:
                        total_formulas += 1
            except Exception:  # noqa: BLE001
                pass
        method_alignment_summary["total_formulas"] = total_formulas
    except Exception:  # noqa: BLE001
        pass

    # 论文写作摘要
    writing_summary = {"artifact_count": 0}
    try:
        from core.artifacts import ArtifactManager as _AM
        from core.knowledge import ArtifactType
        am = _AM(_CONFIG.paths.project_db(project_id))
        art_count = 0
        for wt in (ArtifactType.PAPER_DRAFT, ArtifactType.REVIEW_NOTE):
            try:
                art_count += len(am.list_artifacts(artifact_type=wt))
            except Exception:  # noqa: BLE001
                pass
        writing_summary["artifact_count"] = art_count
    except Exception:  # noqa: BLE001
        pass

    # 阶段状态
    stage_statuses: dict[str, str] = {}
    current_stage = ""
    try:
        session = ProjectSession.load(project_id, _CONFIG.paths)
        current_stage = session.current_stage().value
        for stage in LifecycleStage.ordered():
            stage_statuses[stage.value] = session.status_of(stage).value
    except Exception:  # noqa: BLE001
        pass

    return {
        "project_id": project_id,
        "topic": state.topic,
        "status": state.status,
        "summary": state.summary,
        "current_stage": current_stage,
        "stage_statuses": stage_statuses,
        "counts": counts,
        "research_report_summary": {
            "gaps_count": len(cv_report.get("gaps", [])),
            "consensus_count": len(cv_report.get("consensus", [])),
            "conflicts_count": len(cv_report.get("conflicts", [])),
            "overall_confidence": cv_report.get("overall_confidence", 0),
        },
        "discovery_summary": discovery_summary,
        "materials_cv_summary": {
            "total_discoveries": materials_cv.get("total_discoveries", 0),
            "mp_validated": materials_cv.get("mp_validated", 0),
            "rule_validated": materials_cv.get("rule_validated", 0),
            "overall_confidence": materials_cv.get("overall_confidence", 0),
            "source": materials_cv.get("source", ""),
        },
        "method_alignment_summary": method_alignment_summary,
        "writing_summary": writing_summary,
        "pending_human": _BRIDGE.get_pending(project_id),
    }


# ===== 产出物下载 =====


@app.get("/api/projects/{project_id}/download/{artifact_type}")
def download_artifact(project_id: str, artifact_type: str, format: str = "md"):
    """下载关键产出物，支持 md / docx / pdf 三种格式。

    支持类型：
    - research-report：文献调研报告
    - discovery-report：构效关系发现报告
    - experiment-code：实验代码（仅 md/py 格式）
    - experiment-results：实验结果（含 metrics）
    - method-doc：方法文档
    - paper-draft：论文稿
    - claims-summary：Claim 汇总
    - ideas-summary：研究思路汇总
    - full-report：全流程综合报告
    """
    _require_project(project_id)
    store = KnowledgeStore(_CONFIG.paths.project_db(project_id))

    # 实验代码仅支持原文下载（py 格式）
    if artifact_type == "experiment-code":
        exp_dir = _CONFIG.paths.project_dir(project_id) / "experiments"
        code_path = exp_dir / "run_exp.py"
        if code_path.exists():
            code = code_path.read_text(encoding="utf-8")
        else:
            exps = store.list_experiments()
            code = f"# 无 run_exp.py，共有 {len(exps)} 条实验记录"
        return _make_download(code, "run_exp.py", "text/x-python", "md")

    # 其余类型先统一生成 Markdown 内容
    md_content = ""
    base_filename = ""

    if artifact_type == "research-report":
        report = store.get_kv("cross_validation_report") or {}
        md_content = _build_research_report_md(report)
        base_filename = "research_report"

    elif artifact_type == "discovery-report":
        content = store.get_kv("discovery_report_content") or ""
        if not content:
            summary = store.get_kv("discovery_summary") or {}
            content = f"# 构效关系发现报告\n\n{json.dumps(summary, ensure_ascii=False, indent=2)}"
        md_content = content
        base_filename = "discovery_report"

    elif artifact_type == "method-doc":
        try:
            from core.artifacts import ArtifactManager
            from core.knowledge import ArtifactType
            am = ArtifactManager(_CONFIG.paths.project_db(project_id))
            arts = am.list_artifacts(artifact_type=ArtifactType.METHOD_DOC)
            if arts:
                md_content = am.read_content(arts[0]) or str(arts[0].content or "")
            else:
                md_content = "# 无方法文档"
        except Exception:
            md_content = "# 无方法文档"
        base_filename = "method_doc"

    elif artifact_type == "paper-draft":
        try:
            from core.artifacts import ArtifactManager
            from core.knowledge import ArtifactType
            am = ArtifactManager(_CONFIG.paths.project_db(project_id))
            arts = am.list_artifacts(artifact_type=ArtifactType.PAPER_DRAFT)
            if arts:
                md_content = am.read_content(arts[0]) or str(arts[0].content or "")
            else:
                md_content = "# 无论文稿"
        except Exception:
            md_content = "# 无论文稿"
        base_filename = "paper_draft"

    elif artifact_type == "claims-summary":
        md_content = _build_claims_summary_md(store)
        base_filename = "claims_summary"

    elif artifact_type == "ideas-summary":
        md_content = _build_ideas_summary_md(store)
        base_filename = "ideas_summary"

    elif artifact_type == "experiment-results":
        md_content = _build_experiment_results_md(store, project_id)
        base_filename = "experiment_results"

    elif artifact_type == "full-report":
        md_content = _build_full_report_md(store, project_id)
        base_filename = "full_report"

    else:
        raise HTTPException(status_code=400, detail=f"不支持的下载类型: {artifact_type}")

    # 按格式转换
    fmt = (format or "md").lower()
    if fmt == "docx":
        return _md_to_docx_response(md_content, base_filename)
    elif fmt == "pdf":
        return _md_to_pdf_response(md_content, base_filename)
    else:
        return _make_download(md_content, f"{base_filename}.md", "text/markdown", "md")


def _build_research_report_md(report: dict) -> str:
    """把 cross_validation_report 转为 Markdown。"""
    md = "# 文献调研报告\n\n"
    md += f"**综合置信度**: {report.get('overall_confidence', 0):.2f}\n\n"

    gaps = report.get("gaps", [])
    if gaps:
        md += "## Research Gaps\n\n"
        for i, g in enumerate(gaps, 1):
            md += f"{i}. {g}\n"
        md += "\n"

    consensus = report.get("consensus", [])
    if consensus:
        md += "## 共识\n\n"
        for i, c in enumerate(consensus, 1):
            md += f"{i}. {c}\n"
        md += "\n"

    conflicts = report.get("conflicts", [])
    if conflicts:
        md += "## 冲突结论\n\n"
        for c in conflicts:
            if isinstance(c, dict):
                md += f"- **{c.get('topic', '?')}**: {c.get('description', '')}\n"
                if c.get("positions"):
                    for pos in c["positions"]:
                        md += f"  - {pos}\n"
            else:
                md += f"- {c}\n"
        md += "\n"

    return md


def _make_download(content: str, filename: str, media_type: str, fmt: str = "md") -> Response:
    """构造文件下载响应。"""
    return Response(
        content=content.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_claims_summary_md(store: KnowledgeStore) -> str:
    """构建 Claim 汇总 Markdown。"""
    claims = store.list_claims()
    md = "# Claim 汇总\n\n"
    for c in claims:
        md += f"## {c.claim_id}\n\n"
        md += f"**陈述**: {c.statement}\n\n"
        md += f"**状态**: {c.status.value}\n\n"
        md += f"**角色**: {c.role}\n\n"
        if c.evidence_refs:
            md += "**证据**:\n"
            for ref in c.evidence_refs:
                md += f"- {ref.get('type', '?')}: {ref.get('id', '?')}"
                if ref.get("chunk_id"):
                    md += f" (chunk: {ref['chunk_id']})"
                md += "\n"
        md += "\n"
    return md


def _build_ideas_summary_md(store: KnowledgeStore) -> str:
    """构建研究思路汇总 Markdown。"""
    ideas = store.list_ideas()
    md = "# 研究思路汇总\n\n"
    md += f"共 {len(ideas)} 个思路\n\n---\n\n"
    for idea in ideas:
        md += f"## {idea.idea_id}\n\n"
        md += f"**状态**: {idea.status}\n\n"
        md += f"**思路**: {idea.text}\n\n"
        if idea.constraints:
            md += f"**约束**: {'; '.join(idea.constraints)}\n\n"
        if idea.source_paper_ids:
            md += f"**来源论文**: {', '.join(idea.source_paper_ids)}\n\n"
        if idea.validation_notes:
            md += f"**验证**: {json.dumps(idea.validation_notes, ensure_ascii=False)}\n\n"
        md += "---\n\n"
    return md


def _build_experiment_results_md(store: KnowledgeStore, project_id: str) -> str:
    """构建实验结果 Markdown（含 metrics）。"""
    experiments = store.list_experiments()
    md = "# 实验结果汇总\n\n"
    md += f"共 {len(experiments)} 个实验\n\n---\n\n"
    for exp in experiments:
        md += f"## {exp.name or exp.experiment_id}\n\n"
        md += f"**状态**: {exp.status.value if hasattr(exp.status, 'value') else exp.status}\n\n"
        if hasattr(exp, 'metrics') and exp.metrics:
            md += f"**Metrics**: {json.dumps(exp.metrics, ensure_ascii=False, indent=2)}\n\n"
        if exp.result_summary:
            md += f"**结果摘要**:\n```\n{exp.result_summary[:3000]}\n```\n\n"
        if exp.anomaly_notes:
            md += f"**异常**: {exp.anomaly_notes[:1000]}\n\n"
        if exp.verifies_claim_ids:
            md += f"**验证 Claim**: {', '.join(exp.verifies_claim_ids)}\n\n"
        md += "---\n\n"
    return md


def _build_full_report_md(store: KnowledgeStore, project_id: str) -> str:
    """构建全流程综合报告 Markdown。"""
    parts: list[str] = []

    # 标题
    topic = store.get_kv("research_topic") or "(未设置主题)"
    parts.append(f"# 科研项目全流程报告\n\n**研究主题**: {topic}\n\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n")

    # 1. 调研报告
    report = store.get_kv("cross_validation_report") or {}
    if report:
        parts.append("---\n\n# 第一部分：文献调研\n\n")
        parts.append(_build_research_report_md(report))

    # 2. 研究思路
    ideas = store.list_ideas()
    if ideas:
        parts.append("\n\n---\n\n# 第二部分：研究思路\n\n")
        parts.append(_build_ideas_summary_md(store))

    # 3. Claim 汇总
    claims = store.list_claims()
    if claims:
        parts.append("\n\n---\n\n# 第三部分：Claim 汇总\n\n")
        parts.append(_build_claims_summary_md(store))

    # 4. 方法文档
    try:
        from core.artifacts import ArtifactManager
        from core.knowledge import ArtifactType
        am = ArtifactManager(_CONFIG.paths.project_db(project_id))
        arts = am.list_artifacts(artifact_type=ArtifactType.METHOD_DOC)
        if arts:
            content = am.read_content(arts[0]) or str(arts[0].content or "")
            parts.append("\n\n---\n\n# 第四部分：方法设计\n\n")
            parts.append(content)
    except Exception:
        pass

    # 5. 实验结果
    experiments = store.list_experiments()
    if experiments:
        parts.append("\n\n---\n\n# 第五部分：实验结果\n\n")
        parts.append(_build_experiment_results_md(store, project_id))

    # 6. 论文稿
    try:
        from core.artifacts import ArtifactManager
        from core.knowledge import ArtifactType
        am = ArtifactManager(_CONFIG.paths.project_db(project_id))
        arts = am.list_artifacts(artifact_type=ArtifactType.PAPER_DRAFT)
        if arts:
            content = am.read_content(arts[0]) or str(arts[0].content or "")
            parts.append("\n\n---\n\n# 第六部分：论文稿\n\n")
            parts.append(content)
    except Exception:
        pass

    return "".join(parts) if parts else "# 全流程报告\n\n（暂无产出）"


# ===== 格式转换：Markdown → DOCX / PDF =====


def _md_to_docx_response(md_content: str, base_filename: str) -> Response:
    """将 Markdown 转为 DOCX 并返回下载响应。"""
    import io
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    # 设置默认字体
    style = doc.styles["Normal"]
    style.font.size = Pt(11)

    in_code_block = False
    for line in md_content.split("\n"):
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            p = doc.add_paragraph(line)
            p.style = doc.styles["Normal"]
            run = p.runs[0] if p.runs else p.add_run("")
            run.font.name = "Consolas"
            run.font.size = Pt(9)
            continue

        # 标题
        if stripped.startswith("### "):
            doc.add_heading(stripped[4:], level=3)
        elif stripped.startswith("## "):
            doc.add_heading(stripped[3:], level=2)
        elif stripped.startswith("# "):
            doc.add_heading(stripped[2:], level=1)
        elif stripped.startswith("---"):
            doc.add_paragraph("─" * 40)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            doc.add_paragraph(stripped[2:], style="List Bullet")
        elif stripped and stripped[0].isdigit() and ". " in stripped[:5]:
            idx = stripped.index(". ")
            doc.add_paragraph(stripped[idx + 2:], style="List Number")
        elif stripped:
            doc.add_paragraph(stripped)
        # 空行跳过

    buf = io.BytesIO()
    doc.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{base_filename}.docx"'},
    )


def _md_to_pdf_response(md_content: str, base_filename: str) -> Response:
    """将 Markdown 转为 PDF 并返回下载响应。

    使用 fpdf2 + Windows 系统中文字体（Microsoft YaHei / SimHei）。
    """
    import io
    from fpdf import FPDF

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # 注册中文字体（Windows 系统字体）
    font_name = "chinese"
    font_paths = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    font_loaded = False
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                pdf.add_font(font_name, "", fp, uni=True)
                font_loaded = True
                break
            except Exception:
                continue
    if not font_loaded:
        # 兜底：用内置字体（不支持中文，但不会崩溃）
        font_name = "Helvetica"

    pdf.set_font(font_name, size=11)

    in_code_block = False
    for line in md_content.split("\n"):
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            pdf.set_font(font_name, size=8)
            # 截断超长行
            safe = stripped[:120] if len(stripped) > 120 else stripped
            pdf.multi_cell(0, 5, safe)
            pdf.set_font(font_name, size=11)
            continue

        if stripped.startswith("### "):
            pdf.set_font(font_name, size=13)
            pdf.ln(3)
            pdf.multi_cell(0, 7, stripped[4:])
            pdf.set_font(font_name, size=11)
        elif stripped.startswith("## "):
            pdf.set_font(font_name, size=15)
            pdf.ln(5)
            pdf.multi_cell(0, 8, stripped[3:])
            pdf.set_font(font_name, size=11)
        elif stripped.startswith("# "):
            pdf.set_font(font_name, size=18)
            pdf.ln(8)
            pdf.multi_cell(0, 10, stripped[2:])
            pdf.set_font(font_name, size=11)
        elif stripped.startswith("---"):
            pdf.ln(3)
            pdf.multi_cell(0, 5, "=" * 60)
            pdf.ln(2)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            pdf.multi_cell(0, 6, f"  • {stripped[2:]}")
        elif stripped:
            pdf.multi_cell(0, 6, stripped)
        else:
            pdf.ln(3)

    buf = io.BytesIO()
    pdf.output(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{base_filename}.pdf"'},
    )


# ===== 文件上传 =====


@app.post("/api/projects/{project_id}/upload-paper")
async def upload_paper(
    project_id: str,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
):
    """上传 PDF/文本文献，入库为 Paper 实体。

    支持 .pdf / .txt / .md 文件。
    - PDF：优先使用 MinerU（赛题推荐）深度解析为结构化 Markdown，无 MinerU 时降级为 PyMuPDF 纯文本
    - txt/md：直接作为摘要与 chunk 素材
    """
    _require_project(project_id)
    store = KnowledgeStore(_CONFIG.paths.project_db(project_id))

    # 读取文件内容
    raw = await file.read()
    filename = file.filename or "uploaded.txt"
    ext = Path(filename).suffix.lower()

    paper_id = f"paper_{uuid.uuid4().hex[:12]}"
    abstract = ""
    pdf_path = None
    parsed_markdown = ""
    parsed_tables: list[str] = []
    parsed_formulas: list[str] = []
    parse_source = ""

    if ext == ".pdf":
        # PDF：保存到项目目录
        upload_dir = _CONFIG.paths.project_dir(project_id) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = upload_dir / filename
        pdf_path.write_bytes(raw)

        # 使用 MinerU 解析 PDF（赛题推荐工具）
        from core.tools import mineru_is_available, mineru_parse_pdf
        if mineru_is_available():
            try:
                parsed_doc = mineru_parse_pdf(pdf_path)
                parsed_markdown = parsed_doc.markdown
                parsed_tables = parsed_doc.tables
                parsed_formulas = parsed_doc.formulas
                parse_source = parsed_doc.source
                abstract = parsed_markdown[:5000] if parsed_markdown else f"(MinerU 解析为空: {filename})"
            except Exception as e:
                abstract = f"(MinerU 解析失败: {e}，PDF 已保存: {filename})"
        else:
            # 降级：用 PyMuPDF 提取纯文本
            try:
                import fitz
                doc = fitz.open(str(pdf_path))
                text_parts = [page.get_text() for page in doc]
                doc.close()
                parsed_markdown = "\n\n".join(text_parts)
                abstract = parsed_markdown[:5000] if parsed_markdown else f"(PDF 文本提取为空: {filename})"
                parse_source = "fallback"
            except Exception:
                abstract = f"(PDF 文件已上传: {filename}，需安装 MinerU 或 PyMuPDF 提取文本)"
    elif ext in (".txt", ".md"):
        abstract = raw.decode("utf-8", errors="replace")[:5000]
        parsed_markdown = abstract
    else:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")

    from core.knowledge import Paper
    paper = Paper(
        paper_id=paper_id,
        title=title or filename,
        authors=[],
        abstract=abstract,
        url=None,
        pdf_path=str(pdf_path) if pdf_path else None,
        source_stage="upload",
        metadata={
            "parse_source": parse_source,
            "table_count": len(parsed_tables),
            "formula_count": len(parsed_formulas),
        },
    )
    store.save_paper(paper)

    # 切分为 chunk（优先使用解析后的全文，而非仅 abstract）
    chunk_count = 0
    chunk_source = parsed_markdown if parsed_markdown else abstract
    if chunk_source and len(chunk_source) > 50:
        from core.tools import split_into_chunks
        from core.knowledge import PaperChunk
        text_chunks = split_into_chunks(chunk_source, max_tokens=500, overlap_tokens=50)
        paper_chunks = [
            PaperChunk(
                chunk_id=f"{paper_id}_c{tc.index}",
                paper_id=paper_id,
                chunk_index=tc.index,
                text=tc.text,
            )
            for tc in text_chunks
        ]
        if paper_chunks:
            store.save_paper_chunks(paper_chunks)
            chunk_count = len(paper_chunks)

    return {
        "paper_id": paper_id,
        "title": paper.title,
        "filename": filename,
        "chunks": chunk_count,
        "parse_source": parse_source or ("mineru" if mineru_is_available() else "text"),
        "tables": len(parsed_tables),
        "formulas": len(parsed_formulas),
        "message": "文献上传成功" + (f"（MinerU 解析: {len(parsed_tables)} 表格, {len(parsed_formulas)} 公式）" if parse_source == "mineru" else ""),
    }


@app.post("/api/projects/{project_id}/upload-topic")
async def upload_topic(
    project_id: str,
    file: UploadFile = File(...),
):
    """上传主题描述文件（.txt/.md），覆盖项目主题。"""
    state = _require_project(project_id)
    raw = await file.read()
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise HTTPException(status_code=400, detail="文件内容为空")
    state.topic = text
    return {"project_id": project_id, "topic": text, "message": "主题已更新"}


# ===== 辅助 =====


def _require_project(project_id: str) -> ProjectState:
    with _LOCK:
        state = _PROJECTS.get(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    return state


# ===== 静态资源 =====


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# 挂载静态目录（/static/...）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.api:app", host="0.0.0.0", port=8000, reload=False)
