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

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
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
                "appeared_at": datetime.utcnow().isoformat(),
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


def _run_pipeline_thread(project_id: str, topic: str, resume: bool) -> None:
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
        created_at=datetime.utcnow().isoformat(),
    )
    with _LOCK:
        _PROJECTS[project_id] = state
    return {"project_id": project_id, "topic": topic, "created_at": state.created_at}


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
def run_project(project_id: str) -> dict:
    """启动/继续 pipeline（异步执行，立即返回）。"""
    state = _require_project(project_id)
    if state.status == "running":
        raise HTTPException(status_code=409, detail="pipeline 正在运行中")
    if state.status == "pending_human":
        raise HTTPException(
            status_code=409,
            detail="当前等待人工响应，请先提交 human-response 或中止",
        )
    resume = state.status not in ("created",)
    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(project_id, state.topic, resume),
        daemon=True,
    )
    state.thread = thread
    state.error = None
    thread.start()
    return {"project_id": project_id, "message": "pipeline 已启动", "resumed": resume}


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
                "abstract": p.abstract,
                "url": p.url,
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
        "created_at": datetime.utcnow().isoformat(),
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
        "pending_human": _BRIDGE.get_pending(project_id),
    }


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
