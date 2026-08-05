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
from typing import Any, Callable, Optional

# 确保项目根在 sys.path（python -m web.api 已保证，但直接运行脚本时需补充）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 启动前加载 .env
from runtime.cli import _load_env  # noqa: E402

_load_env()


def _guard_single_instance() -> None:
    """启动时检测端口冲突，防止多 uvicorn 实例分裂内存状态。

    多实例会导致 _PROJECTS / _BRIDGE（进程内存单例）互相不可见：
    人工节点提交失败、项目进度丢失。本函数在模块 import 时探测端口。

    兼容性：
    - python -m web.api：import 时端口尚未被自己绑定，能准确识别旧实例。
    - uvicorn web.api:app / --reload：worker 子进程 import 本模块时端口尚未被自己
      绑定，不会误判；若检测到旧实例则报错退出（reload supervisor 会反复重启，
      必须先清理旧实例）。
    - 测试/嵌入式场景：设 SRA_WEB_SKIP_GUARD=1 跳过。
    """
    if os.environ.get("SRA_WEB_SKIP_GUARD", "").lower() in ("1", "true", "yes"):
        return
    import socket

    port = int(os.environ.get("SRA_WEB_PORT", "8001"))
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", port))
    except OSError:
        print(
            f"[FATAL] 端口 {port} 已被占用，检测到另一个 uvicorn 实例在运行。\n"
            "多实例会导致项目状态互相不可见（人工节点提交失败、进度丢失）。\n"
            "请先停止旧实例再启动：\n"
            f"  netstat -ano | findstr :{port}\n"
            "  taskkill /PID <pid> /F",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        probe.close()


_guard_single_instance()

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


class TopicDiscoveryRequest(BaseModel):
    """方向推荐请求。"""
    interest: str


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

    def make_callback(
        self,
        project_id: str,
        on_pending: Optional[Callable[[], None]] = None,
        on_resume: Optional[Callable[[], None]] = None,
    ):
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
            # 通知外部（工作线程）项目进入人工等待状态
            if on_pending is not None:
                on_pending()
            # 阻塞直到 REST 接口提交响应或超时
            triggered = event.wait(timeout=3600)
            # 收到响应/超时后恢复 running
            if on_resume is not None:
                on_resume()
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
    # 方向推荐产出（从 topic_discovery 子图收集）
    topic_discovery: dict = field(default_factory=dict)
    # 当前运行模式：pipeline / discovery / topic_discovery
    run_mode: str = ""


# ===== 全局状态 =====

_CONFIG = get_config()
_PROJECTS: dict[str, ProjectState] = {}
_BRIDGE = HumanCallbackBridge()
_LOCK = threading.Lock()


# ===== Pipeline 工作线程 =====


def _set_state_status(state: ProjectState, status: str) -> None:
    """线程安全更新项目状态。"""
    with _LOCK:
        state.status = status


def _run_pipeline_thread(project_id: str, topic: str, resume: bool) -> None:
    """工作线程函数：执行 Pipeline 并更新项目状态。"""
    state = _PROJECTS.get(project_id)
    if state is None:
        return
    try:
        _set_state_status(state, "running")
        pipeline = Pipeline(config=_CONFIG)
        # dry_run 模式下也使用桥接回调，让用户能介入；非 dry_run 同样使用桥接
        # 人工等待期间将 status 置为 pending_human，前端可明确感知
        human_cb = _BRIDGE.make_callback(
            project_id,
            on_pending=lambda: _set_state_status(state, "pending_human"),
            on_resume=lambda: _set_state_status(state, "running"),
        )
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
        _set_state_status(state, "running")
        state.run_mode = "discovery"
        pipeline = Pipeline(config=_CONFIG)
        human_cb = _BRIDGE.make_callback(
            project_id,
            on_pending=lambda: _set_state_status(state, "pending_human"),
            on_resume=lambda: _set_state_status(state, "running"),
        )
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


def _run_topic_discovery_thread(project_id: str, interest: str, resume: bool) -> None:
    """工作线程函数：执行方向推荐并更新项目状态。"""
    state = _PROJECTS.get(project_id)
    if state is None:
        return
    try:
        _set_state_status(state, "running")
        state.run_mode = "topic_discovery"
        pipeline = Pipeline(config=_CONFIG)
        human_cb = _BRIDGE.make_callback(
            project_id,
            on_pending=lambda: _set_state_status(state, "pending_human"),
            on_resume=lambda: _set_state_status(state, "running"),
        )
        def _publish_recommendations(recs: list[dict]) -> None:
            """推荐就绪时提前写入 state，供 pending_human 状态下前端展示卡片。"""
            with _LOCK:
                state.topic_discovery = {
                    **state.topic_discovery,
                    "recommendations": recs,
                    "interest": interest,
                }

        result: PipelineResult = pipeline.run_topic_discovery(
            project_id=project_id,
            interest=interest,
            human_callback=human_cb,
            auto_research=False,
            resume=resume,
            on_recommendations=_publish_recommendations,
        )
        state.last_result = result
        state.status = result.status
        state.summary = result.summary
        state.recommendation = result.recommendation
        state.node_history = result.node_history or []
        # 存储方向推荐完整数据（推荐列表 + 选择结果）
        state.topic_discovery = result.extra or {}
    except Exception as e:  # noqa: BLE001
        state.status = "failed"
        state.error = f"{type(e).__name__}: {e}"
        state.summary = f"TopicDiscovery 执行异常: {e}"


def _extract_topic_discovery_summary(node_history: list[dict]) -> dict:
    """从节点历史提取方向推荐产出摘要。"""
    summary = {
        "trends_fetched": False,
        "emerging_count": 0,
        "stable_count": 0,
        "saturated_count": 0,
        "recommendations_count": 0,
        "selected_topic": "",
        "nodes": [],
    }
    for h in node_history or []:
        node_id = h.get("node_id", "")
        if node_id in ("trend_fetch", "trend_analysis", "topic_recommend", "topic_select"):
            summary["nodes"].append({
                "node_id": node_id,
                "status": h.get("status"),
                "summary": h.get("summary", ""),
            })
            if node_id == "trend_fetch":
                summary["trends_fetched"] = True
            if node_id == "trend_analysis":
                # 从 summary 提取数量
                s = h.get("summary", "")
                try:
                    if "个新兴方向" in s:
                        summary["emerging_count"] = int(s.split("个新兴方向")[0].split("：")[-1])
                    if "个稳定方向" in s:
                        summary["stable_count"] = int(s.split("个稳定方向")[0].split("，")[-1])
                    if "个饱和方向" in s:
                        summary["saturated_count"] = int(s.split("个饱和方向")[0].split("，")[-1])
                except (IndexError, ValueError):
                    pass
            if node_id == "topic_recommend":
                s = h.get("summary", "")
                try:
                    if "个推荐主题" in s:
                        summary["recommendations_count"] = int(s.split("生成")[1].split("个")[0])
                except (IndexError, ValueError):
                    pass
            if node_id == "topic_select":
                s = h.get("summary", "")
                # 用户选择的主题会通过 context 写入，这里只是标记
                summary["selected_topic"] = ""
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
        "topic_discovery": state.topic_discovery,
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


@app.post("/api/projects/{project_id}/run-topic-discovery")
def run_topic_discovery(project_id: str, req: TopicDiscoveryRequest) -> dict:
    """启动方向推荐（topic_discovery 子图）。

    在用户给定主题之前，主动分析领域趋势、推荐研究主题。
    异步执行，推荐结果通过 /recommendations 接口查询，
    用户选择通过 /human-response 接口提交。
    """
    state = _require_project(project_id)
    interest = (req.interest or "").strip()
    if not interest:
        raise HTTPException(status_code=400, detail="interest 不能为空")
    if state.status == "running":
        raise HTTPException(status_code=409, detail="任务正在运行中")
    if state.status == "pending_human":
        raise HTTPException(
            status_code=409,
            detail="当前等待人工响应，请先提交 human-response 或中止",
        )
    resume = state.status not in ("created",)
    thread = threading.Thread(
        target=_run_topic_discovery_thread,
        args=(project_id, interest, resume),
        daemon=True,
    )
    state.thread = thread
    state.error = None
    state.topic = interest  # 更新 topic 为用户输入的研究兴趣
    thread.start()
    return {"project_id": project_id, "message": "topic_discovery 已启动", "interest": interest}


@app.get("/api/projects/{project_id}/recommendations")
def get_recommendations(project_id: str) -> dict:
    """获取方向推荐产出（推荐主题列表 + 趋势分析摘要）。"""
    state = _require_project(project_id)
    return {
        "project_id": project_id,
        "topic_discovery_summary": state.topic_discovery,
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
        # 没有等待中的人工请求：可能已提交、已超时、被清除，或多实例分裂
        raise HTTPException(
            status_code=409,
            detail=(
                "当前没有等待中的人工请求：可能已提交、已超时或被清除。"
                "请刷新页面查看最新状态。"
            ),
        )
    return {"project_id": project_id, "submitted": True, "action": action}


# ===== 辅助 =====


def _require_project(project_id: str) -> ProjectState:
    with _LOCK:
        state = _PROJECTS.get(project_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"项目不存在: {project_id}"
                "（若刚重启服务，内存项目列表已丢失，请重新创建项目；"
                "若访问了错误端口，请确认 8001 单实例）"
            ),
        )
    return state


# ===== 静态资源 =====


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# 挂载静态目录（/static/...）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("SRA_WEB_PORT", "8001"))
    uvicorn.run("web.api:app", host="0.0.0.0", port=port, reload=False)
