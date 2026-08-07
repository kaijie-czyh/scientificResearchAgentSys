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
    # 当前正在执行的节点 + 下一步候选（长任务实时进度提示）
    current_node: Optional[dict] = None
    next_nodes: list[str] = field(default_factory=list)
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


def _scan_existing_projects() -> None:
    """启动时从磁盘扫描已存在项目并恢复内存状态。

    项目数据（knowledge.db / snapshots）持久化在 projects/ 目录，
    但 ProjectState 是进程内存单例，服务重启即丢失。此函数让重启后
    旧项目仍可通过 Web 访问（论文/材料/证据等数据按 project_id 读取）。
    topic 不持久化（ProjectSession 快照不含），恢复项目显示 topic 为空，
    重新运行前需在 UI 确认主题。
    """
    projects_dir = _CONFIG.paths.projects
    if not projects_dir.exists():
        return
    for pdir in sorted(projects_dir.iterdir()):
        if not pdir.is_dir() or pdir.name in _PROJECTS:
            continue
        if not (pdir / "knowledge.db").exists():
            continue
        pid = pdir.name
        # created_at 从目录名解析：proj_YYYYMMDD_HHMMSS_xxxx
        created_at = datetime.utcnow().isoformat()
        try:
            _, ts = pid.split("_", 1)
            created_at = datetime.strptime(ts[:13], "%Y%m%d_%H%M%S").isoformat()
        except Exception:
            pass
        # 从 session 快照推断运行状态
        status, summary = "created", ""
        try:
            session = ProjectSession.load(pid, _CONFIG.paths)
            cur = session.current_stage().value
            done = [s for s in LifecycleStage.ordered()
                    if session.status_of(s).value in ("completed", "blocked", "failed")]
            if done or cur != LifecycleStage.RESEARCH.value:
                status = "completed"
                summary = f"服务重启后自动恢复（最近阶段: {cur}）"
            else:
                summary = "服务重启后自动恢复（数据可查看，重新运行需确认主题）"
        except Exception:
            status, summary = "created", "服务重启后自动恢复"
        _PROJECTS[pid] = ProjectState(
            project_id=pid,
            topic="",
            created_at=created_at,
            status=status,
            summary=summary,
        )


_scan_existing_projects()


# ===== Pipeline 工作线程 =====


def _set_state_status(state: ProjectState, status: str) -> None:
    """线程安全更新项目状态。"""
    with _LOCK:
        state.status = status


def _make_progress_callback(
    state: ProjectState,
) -> tuple[Callable[[list[dict]], None], Callable[[str, list[str]], None]]:
    """构造节点级实时进度回调对（完成回调 + 开始回调）。

    完成回调：每完成一个节点就把最新节点历史写入 state.node_history。
    开始回调：每开始一个节点就把当前节点 ID 与下一步候选写入 state。

    这是解决“页面长时间无反应”的关键：research 阶段真实运行需 15-25 分钟，
    若只在 pipeline 结束后才写 node_history，前端会一直空白。此回调让
    /status 轮询能实时看到节点进度，并展示「正在执行 / 下一步」避免干等。

    注意：只写 node_history / current_node，不改 state.status。status 由
    线程函数与 on_pending/on_resume（pending_human ↔ running）统一管理，
    避免回调在人工等待期间误覆盖状态。
    """
    def _on_progress(history: list[dict]) -> None:
        with _LOCK:
            state.node_history = history

    def _on_node_started(node_id: str, next_nodes: list[str]) -> None:
        with _LOCK:
            state.current_node = {
                "node_id": node_id,
                "next_nodes": next_nodes,
                "started_at": datetime.utcnow().isoformat(),
            }
            state.next_nodes = list(next_nodes)

    return _on_progress, _on_node_started


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
        _progress_cbs = _make_progress_callback(state)
        result: PipelineResult = pipeline.run_pipeline(
            project_id=project_id,
            topic=topic,
            human_callback=human_cb,
            resume=resume,
            on_progress=_progress_cbs[0],
            on_node_started=_progress_cbs[1],
        )
        state.last_result = result
        state.status = result.status
        state.summary = result.summary
        state.recommendation = result.recommendation
        state.node_history = result.node_history or []
        state.current_node = None
        state.next_nodes = []
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
        _progress_cbs = _make_progress_callback(state)
        result: PipelineResult = pipeline.run_discovery(
            project_id=project_id,
            topic=topic,
            human_callback=human_cb,
            resume=resume,
            on_progress=_progress_cbs[0],
            on_node_started=_progress_cbs[1],
        )
        state.last_result = result
        state.status = result.status
        state.summary = result.summary
        state.recommendation = result.recommendation
        state.node_history = result.node_history or []
        state.current_node = None
        state.next_nodes = []
        # 从 node_history 提取 discovery 产出摘要
        state.discovery = _extract_discovery_summary(
            result.node_history, result.extra.get("hypotheses") or []
        )
    except Exception as e:  # noqa: BLE001
        state.status = "failed"
        state.error = f"{type(e).__name__}: {e}"
        state.summary = f"Discovery 执行异常: {e}"


def _extract_discovery_summary(node_history: list[dict], hypotheses: list[dict] | None = None) -> dict:
    """从节点历史提取 discovery 产出摘要。"""
    summary = {
        "hypotheses": 0, "candidates": 0, "relationships": 0, "novel": 0,
        "nodes": [], "hypothesis_list": [],
    }
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
    # 假设列表（含三维可验证性评分），按综合分降序，供 Web 排序展示
    h_list: list[dict] = []
    for hyp in hypotheses or []:
        if not isinstance(hyp, dict) or not hyp.get("hypothesis"):
            continue
        def _f(key: str) -> float:
            try:
                return float(hyp.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0
        n_, f_, g_ = _f("novelty_score"), _f("feasibility_score"), _f("gap_relevance_score")
        h_list.append({
            "hypothesis": hyp.get("hypothesis", ""),
            "variables": hyp.get("variables", []) or [],
            "target_property": hyp.get("target_property", ""),
            "rationale": hyp.get("rationale", ""),
            "gap_ref": hyp.get("gap_ref", ""),
            "novelty_score": round(n_, 2),
            "feasibility_score": round(f_, 2),
            "gap_relevance_score": round(g_, 2),
            "overall_score": round(0.4 * n_ + 0.3 * f_ + 0.3 * g_, 2),
        })
    h_list.sort(key=lambda x: x["overall_score"], reverse=True)
    summary["hypothesis_list"] = h_list
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

        _progress_cbs = _make_progress_callback(state)
        result: PipelineResult = pipeline.run_topic_discovery(
            project_id=project_id,
            interest=interest,
            human_callback=human_cb,
            auto_research=False,
            resume=resume,
            on_recommendations=_publish_recommendations,
            on_progress=_progress_cbs[0],
            on_node_started=_progress_cbs[1],
        )
        state.last_result = result
        state.status = result.status
        state.summary = result.summary
        state.recommendation = result.recommendation
        state.node_history = result.node_history or []
        state.current_node = None
        state.next_nodes = []
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
    counts = {"papers": 0, "ideas": 0, "claims": 0, "experiments": 0, "evidence": 0,
              "materials": 0, "properties": 0, "synthesis": 0, "gaps": 0}
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        counts["papers"] = len(store.list_papers())
        counts["ideas"] = len(store.list_ideas())
        counts["claims"] = len(store.list_claims())
        counts["experiments"] = len(store.list_experiments())
        counts["evidence"] = store.evidence_stats()["total"]
        mstats = store.material_stats()
        counts["materials"] = mstats["materials"]
        counts["properties"] = mstats["properties"]
        counts["synthesis"] = mstats["synthesis"]
        counts["gaps"] = store.gap_stats()["total"]
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
        "current_node": state.current_node,
        "next_nodes": state.next_nodes,
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
    if not state.topic.strip():
        raise HTTPException(
            status_code=400,
            detail="项目主题为空（服务重启后恢复的项目不持久化 topic），请重新创建项目或补充主题",
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
    if not state.topic.strip():
        raise HTTPException(
            status_code=400,
            detail="项目主题为空（服务重启后恢复的项目不持久化 topic），请重新创建项目或补充主题",
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
                # 检索证据链字段（审计溯源：来源 / doc_id / 证据分 / 命中子问题）
                "source": p.metadata.get("source", ""),
                "source_subquery": p.metadata.get("source_subquery", ""),
                "doc_id": p.metadata.get("doc_id", ""),
                "offset": p.metadata.get("offset", 0),
                "evidence_score": p.metadata.get("evidence_score", 0.0),
                "relevance_score": p.metadata.get("relevance_score", 0.0),
                "relevance_reason": p.metadata.get("relevance_reason", ""),
            }
            for p in papers
        ]
    }


@app.get("/api/projects/{project_id}/materials")
def list_materials(project_id: str) -> dict:
    """获取材料知识库（Task 2：材料-性能-合成三元组）。

    返回按材料聚合的知识：每种材料含其性能指标与合成方法，
    均带来源论文与证据片段（可溯源）。满足赛题「知识抽取结构化」要求。

    数据标准化（core/knowledge/normalize.py）：
    - 性能指标统一映射（ZT/zT/figure_of_merit → 热电优值，带标准符号/单位/类别）
    - 合成方法归类（固相法/熔融法/烧结法/计算模拟…）
    - 材料体系分类（Bi₂Te₃ 基 / PbTe 基 / 钙钛矿 / 方钴矿…）
    """
    from core.knowledge.normalize import (
        categorize_material,
        classify_method,
        normalize_property,
    )

    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        materials = store.list_materials(limit=500)
        stats = store.material_stats()
        # 聚合：材料 → 性能列表 + 合成列表
        properties = store.list_material_properties()
        synthesis = store.list_material_synthesis()
        props_by_mat: dict[str, list[dict]] = {}
        for p in properties:
            norm = normalize_property(p.property_name, p.property_name_cn)
            props_by_mat.setdefault(p.material_id, []).append({
                "property_name": p.property_name,
                "property_name_cn": p.property_name_cn,
                # 标准化字段
                "norm_key": norm["key"],
                "norm_cn": norm["cn"],
                "symbol": norm["symbol"],
                "unit": norm["unit"],
                "category": norm["category"],
                "value": p.value,
                "value_num": p.value_num,
                "condition": p.condition,
                "paper_title": p.paper_title,
                "confidence": p.confidence,
                "source_snippet": p.source_snippet,
            })
        syn_by_mat: dict[str, list[dict]] = {}
        for s in synthesis:
            mth = classify_method(s.method)
            syn_by_mat.setdefault(s.material_id, []).append({
                "method": s.method,
                # 标准化字段
                "method_category": mth["category"],
                "method_label": mth["label"],
                "precursors": s.precursors,
                "temperature": s.temperature,
                "pressure": s.pressure,
                "atmosphere": s.atmosphere,
                "duration": s.duration,
                "steps": s.steps,
                "paper_title": s.paper_title,
                "confidence": s.confidence,
                "source_snippet": s.source_snippet,
            })
        items = []
        prop_cat_counter: dict[str, int] = {}
        method_cat_counter: dict[str, int] = {}
        material_cat_counter: dict[str, int] = {}
        for m in materials:
            mat_cat = categorize_material(m.name, m.formula)
            material_cat_counter[mat_cat] = material_cat_counter.get(mat_cat, 0) + 1
            props = props_by_mat.get(m.material_id, [])
            syns = syn_by_mat.get(m.material_id, [])
            for pr in props:
                prop_cat_counter[pr["category"]] = prop_cat_counter.get(pr["category"], 0) + 1
            for sy in syns:
                method_cat_counter[sy["method_category"]] = method_cat_counter.get(sy["method_category"], 0) + 1
            items.append({
                "material_id": m.material_id,
                "name": m.name,
                "formula": m.formula,
                "category": mat_cat,
                "crystal_structure": m.crystal_structure,
                "space_group": m.space_group,
                "lattice_parameters": m.lattice_parameters,
                "symmetry": m.symmetry,
                "composition": m.composition,
                "paper_id": m.paper_id,
                "paper_title": m.paper_title,
                "source_paper_ids": m.metadata.get("source_paper_ids", []),
                "confidence": m.confidence,
                "source_snippet": m.source_snippet,
                "properties": props,
                "synthesis": syns,
            })
    except Exception as e:  # noqa: BLE001
        return {"materials": [], "stats": {}, "error": f"读取失败: {e}"}
    return {
        "materials": items,
        "stats": stats,
        "aggregation": {
            "property_categories": prop_cat_counter,
            "method_categories": method_cat_counter,
            "material_categories": material_cat_counter,
        },
    }


@app.get("/api/projects/{project_id}/gaps")
def list_research_gaps(project_id: str) -> dict:
    """获取研究缺口清单（Task 3：Research Gap 识别）。

    由 ResearchGapIdentifyAgent 在 cross_validate 之后生成并落库，
    每条 Gap 含类型（矛盾结论/未被探索方向/缺失知识连接）、
    证据链（可溯源 paper_id + snippet）、可操作性、优先级，
    满足赛题「文献溯源完整性」要求，供前端「研究缺口」页展示。
    """
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        gaps = store.list_research_gaps(limit=200)
        stats = store.gap_stats()
    except Exception as e:  # noqa: BLE001
        return {"gaps": [], "stats": {"total": 0, "by_type": {}},
                "error": f"读取失败: {e}"}
    return {
        "gaps": [
            {
                "gap_id": g.gap_id,
                "gap_type": g.gap_type,
                "statement": g.statement,
                "detail": g.detail,
                "evidence": g.evidence,
                "related_materials": g.related_materials,
                "actionability": g.actionability,
                "priority": g.priority,
                "source": g.source,
                "suggested_actions": g.suggested_actions,
                "subquery": g.subquery,
                "created_at": g.created_at.isoformat() if g.created_at else None,
            }
            for g in gaps
        ],
        "stats": stats,
    }


@app.get("/api/projects/{project_id}/conflicts")
def list_research_conflicts(project_id: str) -> dict:
    """获取文献冲突清单（交叉验证落库，供 Claim 冲突可视化与论文溯源）。

    每条冲突：冲突陈述 + 立场证据（support/refute 双方来源，可点击跳转
    论文页溯源）+ 处置建议 + 置信度 + 来源子问题。
    """
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        conflicts = store.list_research_conflicts(limit=200)
        stats = store.conflict_stats()
    except Exception as e:  # noqa: BLE001
        return {"conflicts": [], "stats": {"total": 0, "papers": 0},
                "error": f"读取失败: {e}"}
    return {
        "conflicts": [
            {
                "conflict_id": c.conflict_id,
                "claim": c.claim,
                "sources": c.sources,
                "resolution": c.resolution,
                "confidence": c.confidence,
                "subquery": c.subquery,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in conflicts
        ],
        "stats": stats,
    }


@app.post("/api/projects/{project_id}/materials/re-extract")
def re_extract_materials(project_id: str) -> dict:
    """覆盖度重抽：对「仅名称」材料（无性能无合成）做针对性二次抽取补全。

    仅名称材料是首轮抽取的召回遗漏（材料名出现但未抽到性能/合成）。
    本接口跨论文聚合包含材料名的摘要片段，专门 prompt 补全其性能与合成。
    异步执行（调用 LLM，可能耗时数十秒~数分钟），立即返回任务受理。
    """
    state = _require_project(project_id)
    if state.status == "running":
        raise HTTPException(status_code=409, detail="pipeline 正在运行中")

    def _worker() -> None:
        try:
            store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
            from core.llm import LLMRegistry
            from stages.research.agents import MaterialKnowledgeExtractionAgent
            registry = LLMRegistry.from_config(_CONFIG)
            agent = MaterialKnowledgeExtractionAgent("material_extraction")
            n_added = agent._re_extract_name_only(store, registry)
            mstats = store.material_stats()
            state.error = None
            state.summary = (
                f"覆盖度重抽完成：补全 {n_added} 条知识"
                f"（库内材料 {mstats['materials']} / 性能 {mstats['properties']} / "
                f"合成 {mstats['synthesis']}）"
            )
        except Exception as e:  # noqa: BLE001
            state.error = f"重抽失败: {type(e).__name__}: {e}"
            state.summary = f"覆盖度重抽异常: {e}"

    threading.Thread(target=_worker, daemon=True).start()
    return {
        "project_id": project_id,
        "message": "覆盖度重抽已启动（异步执行），完成后可刷新材料页查看补全结果",
    }


@app.get("/api/projects/{project_id}/evidence")
def list_evidence(project_id: str) -> dict:
    """获取检索证据链（审计轨迹：query → source → 命中 → paper 关联）。

    赛题手册明确要求文献调研可溯源：Sciverse 调用记录天然构成证据链。
    每条记录：触发子问题、数据源、命中证据标题、外部 ID（doc_id/arxiv_id）、
    证据分、片段摘要，以及是否关联到已入库论文。
    """
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        entries = store.list_evidence(limit=500)
        stats = store.evidence_stats()
    except Exception as e:  # noqa: BLE001
        return {
            "entries": [],
            "stats": {"total": 0, "by_source": {}, "linked": 0},
            "error": f"读取失败: {e}",
        }
    return {"entries": entries, "stats": stats}


@app.get("/api/projects/{project_id}/unlinked-papers")
def list_unlinked_papers(project_id: str) -> dict:
    """列出未入库论文候选（检索命中但未关联入库的证据，按 external_id/title 去重）。

    检索阶段每条子问题按固定配额抓取（Sciverse 10 + arXiv 3 + S2 2），
    其中被相关性筛选或去重剔除的候选保留在证据链（paper_id 为空）。
    前端可展示为「未入库论文」，支持用户手动补录入库。

    每篇候选附带未入库原因（reason）：
    - score_rejected: 相关性打分 < 0.5 被 filter 剔除（Sciverse 用语义检索分，
      arXiv/S2 用 LLM 评估分）
    - dedup_merged:   与已入库论文重复（同 external_id / 同 title），去重时合并
    """
    _require_project(project_id)
    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        entries = store.list_unlinked_evidence(limit=500)
        papers_all = store.list_papers()
    except Exception as e:  # noqa: BLE001
        return {"papers": [], "error": f"读取失败: {e}"}

    # 已入库论文的 arxiv_id / title 集合（用于判定去重合并）
    paper_ax: set[str] = set()
    paper_titles: set[str] = set()
    paper_id_by_ax: dict[str, str] = {}
    paper_id_by_title: dict[str, str] = {}
    for p in papers_all:
        ax = (p.arxiv_id or "").strip()
        tl = (p.title or "").strip().lower()
        if ax:
            paper_ax.add(ax)
            paper_id_by_ax.setdefault(ax, p.paper_id)
        if tl:
            paper_titles.add(tl)
            paper_id_by_title.setdefault(tl, p.paper_id)

    # 按 external_id（或 title）去重聚合：同一候选可能被多个子问题命中
    seen: dict[str, dict] = {}
    for e in entries:
        key = (e.get("external_id") or "").strip() or \
            (e.get("title") or "").strip().lower()
        if not key:
            continue
        if key not in seen:
            seen[key] = {
                "external_id": e.get("external_id", ""),
                "title": e.get("title", ""),
                "source": e.get("source", ""),
                "snippet": e.get("snippet", ""),
                "subquery": e.get("subquery", ""),
                "evidence_score": e.get("evidence_score", 0.0),
                "hit_count": 1,
                "log_ids": [e.get("log_id", "")],
                "reason": "",
                "reason_detail": "",
            }
        else:
            seen[key]["hit_count"] += 1
            if e.get("log_id"):
                seen[key]["log_ids"].append(e.get("log_id", ""))
            if not seen[key]["snippet"]:
                seen[key]["snippet"] = e.get("snippet", "")

    # 判定每篇候选的未入库原因
    for p in seen.values():
        ax = (p["external_id"] or "").strip()
        tl = (p["title"] or "").strip().lower()
        if (ax and ax in paper_ax) or (tl and tl in paper_titles):
            dup_pid = paper_id_by_ax.get(ax) or paper_id_by_title.get(tl, "")
            dup_title = next(
                (pp.title for pp in papers_all if pp.paper_id == dup_pid),
                "",
            )
            p["reason"] = "dedup_merged"
            p["reason_detail"] = (
                f"与已入库论文《{dup_title[:60]}》重复，去重时合并"
            )
        else:
            src = p["source"]
            scorer = "Sciverse 语义检索分" if src == "sciverse" else "LLM 相关性评估分"
            p["reason"] = "score_rejected"
            p["reason_detail"] = (
                f"相关性打分 < 0.5（{scorer}），被 filter 阶段剔除"
            )

    papers = sorted(
        seen.values(),
        key=lambda p: (p["evidence_score"] or 0.0),
        reverse=True,
    )
    return {
        "papers": papers,
        "total": len(papers),
        "filter_threshold": 0.5,
        "filter_note": (
            "入库量化标准：filter 阶段相关性打分 ≥ 0.5 才入库。"
            "Sciverse 候选复用语义检索证据分；arXiv/S2 候选由 LLM 按主题相关性评估。"
            "低于 0.5 的候选保留在未入库列表，可人工复核后手动补录。"
        ),
    }


@app.post("/api/projects/{project_id}/papers/import")
def import_unlinked_paper(project_id: str, body: dict) -> dict:
    """手动补录入库：将一条未关联证据候选正式入库为论文，并回填证据链关联。

    body:
      - external_id: 候选的外部 ID（sciverse doc_id / arxiv_id / s2 paperId）
      - title: 候选标题（external_id 缺失时的兜底定位）
      - snippet: 可选覆盖片段（否则用证据链中的 snippet 作为摘要）
    """
    _require_project(project_id)
    external_id = (body.get("external_id") or "").strip()
    title = (body.get("title") or "").strip()
    if not external_id and not title:
        raise HTTPException(status_code=400, detail="external_id 或 title 至少提供一个")

    try:
        store = KnowledgeStore(_CONFIG.paths.project_db(project_id))
        entries = store.list_unlinked_evidence(limit=500)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取证据链失败: {e}")

    # 定位候选：优先 external_id 精确匹配，其次 title 完全一致
    cand = None
    for e in entries:
        eid = (e.get("external_id") or "").strip()
        etitle = (e.get("title") or "").strip().lower()
        if external_id and eid and eid == external_id:
            cand = e
            break
        if not external_id and title and etitle == title.lower():
            cand = e
            break
    if cand is None:
        # 候选不在未关联列表：可能已入库 → 返回已存在的论文信息
        if external_id:
            existing = store.find_paper_by_external_id(external_id)
            if existing is not None:
                return {
                    "paper_id": existing.paper_id,
                    "created": False,
                    "message": f"该候选已入库：{existing.title}",
                }
        raise HTTPException(
            status_code=404,
            detail="未找到该未入库候选（可能已被入库或已不在证据链中）",
        )

    # 重复入库检查：external_id 已存在则直接关联，不新建论文
    existing = None
    if (cand.get("external_id") or "").strip():
        existing = store.find_paper_by_external_id(cand["external_id"].strip())
    if existing is not None:
        linked_count = 0
        for e in entries:
            eid = (e.get("external_id") or "").strip()
            if external_id and eid and eid == external_id and e.get("log_id"):
                store.link_evidence_to_paper(e["log_id"], existing.paper_id)
                linked_count += 1
        return {
            "paper_id": existing.paper_id,
            "created": False,
            "message": f"该候选已存在同名论文，证据链已关联 {linked_count} 条到现有论文",
        }

    # 新建论文（用证据链中的元数据）
    paper_id = KnowledgeStore.new_id()
    snippet = body.get("snippet") or (cand.get("snippet") or "")
    from core.knowledge.schema import Paper
    paper = Paper(
        paper_id=paper_id,
        title=cand.get("title") or title or "Untitled",
        abstract=snippet or None,
        metadata={
            "source": cand.get("source", ""),
            "source_subquery": cand.get("subquery", ""),
            "doc_id": cand.get("external_id", ""),
            "evidence_score": cand.get("evidence_score", 0.0),
            "relevance_score": 0.0,
            "relevance_reason": "手动补录入库",
            "manually_imported": True,
        },
        source_stage="research",
    )
    # 入库论文 + 切分 chunk
    from core.tools.text_split import split_into_chunks
    store.save_paper(paper)
    abstract = snippet or ""
    chunks = split_into_chunks(abstract, max_tokens=500, overlap_tokens=50)
    from core.knowledge.schema import PaperChunk
    chunk_objs = [
        PaperChunk(
            chunk_id=KnowledgeStore.new_id(),
            paper_id=paper_id,
            chunk_index=i,
            text=c.text,
        )
        for i, c in enumerate(chunks)
    ]
    store.save_paper_chunks(chunk_objs)

    # 回填证据链关联：该候选可能被多个子问题命中，全部关联
    linked_count = 0
    for e in entries:
        eid = (e.get("external_id") or "").strip()
        etitle = (e.get("title") or "").strip().lower()
        match = bool(external_id and eid and eid == external_id) or \
            bool(not external_id and title and etitle == title.lower())
        if match and e.get("log_id"):
            store.link_evidence_to_paper(e["log_id"], paper_id)
            linked_count += 1

    return {
        "paper_id": paper_id,
        "created": True,
        "message": f"论文已入库：{paper.title}（关联 {linked_count} 条证据）",
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
        # 争议检测：claim 证据引用的论文与冲突 sources 有交集 → 争议中
        related_conflicts = []
        try:
            related_conflicts = store.conflicts_for_claim(c.evidence_refs)
        except Exception:  # noqa: BLE001
            related_conflicts = []
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
            # 冲突可视化：相关文献冲突（非空 → 该 Claim 处于争议中）
            "conflicts": [
                {
                    "conflict_id": x.conflict_id,
                    "claim": x.claim,
                    "sources": x.sources,
                    "resolution": x.resolution,
                    "confidence": x.confidence,
                    "subquery": x.subquery,
                }
                for x in related_conflicts
            ],
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


@app.get("/static/{file_path:path}")
def static_files(file_path: str) -> FileResponse:
    """静态资源（强制 no-cache，避免前端更新后用户仍看到旧版缓存导致样式错乱）。"""
    target = (STATIC_DIR / file_path).resolve()
    if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(
        target,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("SRA_WEB_PORT", "8001"))
    uvicorn.run("web.api:app", host="0.0.0.0", port=port, reload=False)
