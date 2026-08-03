"""端到端 Pipeline 编排器。

串联 5 个生命周期阶段子图，处理：
- 系统依赖注入（LLMRegistry / KnowledgeStore / ArtifactManager / ProvenanceValidator / dry_run）
- 状态机流转（ProjectSession.start_stage / advance / complete_stage）
- 阶段间数据流（通过 ExecutionContext 域键自动传递，无需显式搬运）
- 实验成败判断（experiment 阶段末尾的 EXPERIMENT_OUTCOME 决定是否进入 writing）
- 人工节点阻塞（通过 human_callback 回调机制暴露给 UI 层）

核心设计决策（"带着脑子推进"）：
1. 实验失败是科研常态——experiment 阶段产出 EXPERIMENT_OUTCOME，若 success=False 则
   不进入 writing，而是返回"实验未验证核心 Claim"的结果，建议回滚到 ideation 或重试。
   这避免了"为写论文而写论文"的反模式。
2. dry_run 默认 True——所有节点用占位数据返回，不调用 MiniMax API。
   用户配置好 .env 且确认后，设 SRA_DRY_RUN=false 启用真实调用。
3. 人工节点通过回调处理——run_stage 接收 human_callback，签名 (HumanRequest) -> HumanResponse。
   CLI 模式下用 input() 实现，Web 模式下可换成 WebSocket 推送。

使用范式：
    pipeline = Pipeline()
    result = pipeline.run_pipeline(
        project_id="proj_001",
        topic="联邦学习中的公平激励机制",
        human_callback=cli_human_callback,
    )
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import logging

from core.artifacts import ArtifactContentStore, ArtifactManager, ProvenanceValidator
from core.config import GlobalConfig, get_config
from core.knowledge import KnowledgeStore
from core.llm import LLMRegistry
from core.orchestration.context import ExecutionContext
from core.orchestration.graph import Graph, GraphRunner
from core.orchestration.node import HumanRequest, HumanResponse, NodeResult, NodeStatus
from core.state.lifecycle import LifecycleStage, StageStatus
from core.state.session import ProjectSession

from stages.common import (
    ARTIFACT_MANAGER,
    DISCOVERY_CANDIDATES,
    DISCOVERY_HYPOTHESES,
    DISCOVERY_RELATIONSHIPS,
    DISCOVERY_REPORT_ARTIFACT_ID,
    DISCOVERY_SEARCH_SPACE,
    DRY_RUN,
    EXPERIMENT_OUTCOME,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    PROJECT_DIR,
    PROJECT_ROOT,
    PROVENANCE_VALIDATOR,
    RESEARCH_CROSS_VALIDATION_REPORT,
    RESEARCH_FILTERED_PAPER_METAS,
    RESEARCH_PAPER_IDS,
    RESEARCH_PAPER_METAS,
    RESEARCH_TOPIC,
)


# 人工回调类型：(HumanRequest) -> HumanResponse
HumanCallback = Callable[[HumanRequest], HumanResponse]

logger = logging.getLogger(__name__)


@dataclass
class StageResult:
    """单阶段执行结果。"""

    stage: LifecycleStage
    status: str  # success / pending_human / failed / aborted / experiment_failed
    summary: str = ""
    node_history: list[dict] = field(default_factory=list)
    # 若 status=pending_human，附带当前人工请求
    pending_human_request: Optional[HumanRequest] = None
    # 若 status=experiment_failed，附带实验评估结果
    experiment_outcome: Optional[dict] = None
    # 建议的下一步动作
    recommendation: str = ""


@dataclass
class PipelineResult:
    """全流程执行结果。"""

    project_id: str
    status: str  # completed / pending_human / failed / aborted / experiment_failed / stopped
    completed_stages: list[LifecycleStage] = field(default_factory=list)
    current_stage: Optional[LifecycleStage] = None
    summary: str = ""
    experiment_outcome: Optional[dict] = None
    recommendation: str = ""
    node_history: list[dict] = field(default_factory=list)


class Pipeline:
    """端到端科研论文生成 Pipeline。

    串联 5 个生命周期阶段，处理状态流转、依赖注入、实验成败判断、人工节点。
    """

    def __init__(self, config: Optional[GlobalConfig] = None):
        self.config = config or get_config()
        self.paths = self.config.paths

    # ===== 项目初始化 =====

    def start_project(
        self, project_id: str, topic: str
    ) -> tuple[ProjectSession, ExecutionContext]:
        """启动新项目，初始化 session 与 context，注入系统依赖。

        Args:
            project_id: 项目唯一 ID
            topic: 研究主题

        Returns:
            (session, ctx) — 项目会话与执行上下文
        """
        # 创建项目会话
        session = ProjectSession.create(project_id, self.paths)

        # 创建执行上下文
        ctx = ExecutionContext(project_id=project_id)

        # 初始化并注入系统依赖
        store = KnowledgeStore(self.paths.project_db(project_id))
        content_store = ArtifactContentStore(self.paths.project_artifacts(project_id))
        artifact_manager = ArtifactManager(store, content_store)
        provenance_validator = ProvenanceValidator(store)
        registry = LLMRegistry.from_config(self.config)

        ctx.set(LLM_REGISTRY, registry)
        ctx.set(KNOWLEDGE_STORE, store)
        ctx.set(ARTIFACT_MANAGER, artifact_manager)
        ctx.set(PROVENANCE_VALIDATOR, provenance_validator)
        ctx.set(DRY_RUN, self.config.dry_run)
        ctx.set(PROJECT_ROOT, self.paths.root)
        ctx.set(PROJECT_DIR, self.paths.project_dir(project_id))

        # 设置研究主题
        ctx.set(RESEARCH_TOPIC, topic)

        return session, ctx

    def resume_project(self, project_id: str) -> tuple[ProjectSession, ExecutionContext]:
        """从已有项目恢复（用于中断后续跑）。"""
        session = ProjectSession.load(project_id, self.paths)
        ctx = ExecutionContext(project_id=project_id)
        # 重新注入依赖（依赖不持久化在 context 里，每次恢复时重建）
        store = KnowledgeStore(self.paths.project_db(project_id))
        content_store = ArtifactContentStore(self.paths.project_artifacts(project_id))
        ctx.set(LLM_REGISTRY, LLMRegistry.from_config(self.config))
        ctx.set(KNOWLEDGE_STORE, store)
        ctx.set(ARTIFACT_MANAGER, ArtifactManager(store, content_store))
        ctx.set(PROVENANCE_VALIDATOR, ProvenanceValidator(store))
        ctx.set(DRY_RUN, self.config.dry_run)
        ctx.set(PROJECT_ROOT, self.paths.root)
        ctx.set(PROJECT_DIR, self.paths.project_dir(project_id))
        return session, ctx

    def _restore_research_outputs(
        self, ctx: ExecutionContext, project_id: str, topic: str
    ) -> None:
        """resume 模式下从 KnowledgeStore 恢复 research 阶段产出。

        session 只持久化 stage_states，不持久化 ctx 域数据（paper_ids、
        cross_validation_report 等）。discovery 子图依赖这些产出，需手动恢复。
        """
        store: KnowledgeStore = ctx.get(KNOWLEDGE_STORE)  # type: ignore[assignment]
        if store is None:
            return
        try:
            papers = store.list_papers()
            paper_ids = [p.paper_id for p in papers]
            paper_metas = [
                {
                    "title": p.title,
                    "authors": p.authors,
                    "year": p.year,
                    "abstract": p.abstract or "",
                    "arxiv_id": p.arxiv_id,
                    "doi": p.doi,
                    "venue": p.venue,
                    "source_subquery": (p.metadata or {}).get("source_subquery", ""),
                    "relevance_score": (p.metadata or {}).get("relevance_score", 0.7),
                }
                for p in papers
            ]
            ctx.set(RESEARCH_TOPIC, topic)
            ctx.set(RESEARCH_PAPER_IDS, paper_ids)
            ctx.set(RESEARCH_PAPER_METAS, paper_metas)
            ctx.set(RESEARCH_FILTERED_PAPER_METAS, paper_metas)
            # 优先从 KV 表恢复完整 cross_validation_report（含 Research Gaps）
            cv_report = store.get_kv("cross_validation_report")
            if cv_report and (cv_report.get("gaps") or cv_report.get("consensus")):
                ctx.set(RESEARCH_CROSS_VALIDATION_REPORT, cv_report)
                logger.info(
                    "resume 模式：从 KV 恢复完整 cross_validation_report（gaps=%d, consensus=%d）",
                    len(cv_report.get("gaps", [])),
                    len(cv_report.get("consensus", [])),
                )
            else:
                # KV 无记录时设为简化版（兼容旧项目）
                ctx.set(
                    RESEARCH_CROSS_VALIDATION_REPORT,
                    {
                        "gaps": [],
                        "conflicts": [],
                        "consensus": ["(resume 模式，跳过交叉验证)"],
                        "overall_confidence": 0.5,
                    },
                )
            logger.info(
                "resume 模式：从 KnowledgeStore 恢复 %d 篇论文产出", len(paper_ids)
            )
        except Exception as e:
            logger.warning("resume 模式恢复 research 产出失败: %s", e)

    # ===== 阶段图构建 =====

    def _build_stage_graph(self, stage: LifecycleStage) -> Graph:
        """构建阶段子图。延迟导入避免循环依赖。"""
        if stage == LifecycleStage.RESEARCH:
            from stages.research.graph import build_research_graph
            return build_research_graph()
        if stage == LifecycleStage.IDEATION:
            from stages.ideation.graph import build_ideation_graph
            return build_ideation_graph()
        if stage == LifecycleStage.DESIGN:
            from stages.design.graph import build_design_graph
            return build_design_graph()
        if stage == LifecycleStage.EXPERIMENT:
            from stages.experiment.graph import build_experiment_graph
            return build_experiment_graph()
        if stage == LifecycleStage.WRITING:
            from stages.writing.graph import build_writing_graph
            return build_writing_graph()
        raise ValueError(f"未知阶段: {stage}")

    # ===== 单阶段执行 =====

    def run_stage(
        self,
        session: ProjectSession,
        ctx: ExecutionContext,
        stage: LifecycleStage,
        human_callback: Optional[HumanCallback] = None,
    ) -> StageResult:
        """运行单个阶段。

        Args:
            session: 项目会话
            ctx: 执行上下文
            stage: 要运行的阶段
            human_callback: 人工节点回调。若为 None 且遇到人工节点，
                           返回 status=pending_human 的 StageResult。

        Returns:
            StageResult
        """
        graph = self._build_stage_graph(stage)
        runner = GraphRunner(graph, ctx)

        # 启动阶段（状态机流转）
        current_status = session.status_of(stage)
        if current_status == StageStatus.NOT_STARTED:
            session.start_stage(stage, triggered_by="pipeline")
        elif current_status == StageStatus.BLOCKED:
            # 之前运行中节点失败导致 blocked，重新运行前先解除阻塞
            session.unblock(
                reason=f"重新运行阶段 {stage.value}（从 blocked 恢复）",
                triggered_by="pipeline",
            )

        ctx.current_stage = stage.value
        runner.start()

        # 处理人工节点阻塞
        while runner.is_pending_human():
            if human_callback is None:
                # 无回调，返回 pending 状态供调用方处理
                req = runner.pending_human_request()
                return StageResult(
                    stage=stage,
                    status="pending_human",
                    summary=f"阶段 {stage.value} 等待人工输入",
                    node_history=ctx.history(),
                    pending_human_request=req,
                )
            # 有回调，调用回调获取响应
            req = runner.pending_human_request()
            resp = human_callback(req)
            runner.resume_after_human(resp)

        # 检查执行结果
        if runner.is_aborted():
            return StageResult(
                stage=stage,
                status="aborted",
                summary=f"阶段 {stage.value} 被中止",
                node_history=ctx.history(),
            )

        final = runner.final_result()
        if final is not None and final.status == NodeStatus.FAILED:
            session.mark_blocked(reason=final.error or "节点失败", triggered_by="pipeline")
            return StageResult(
                stage=stage,
                status="failed",
                summary=final.summary or f"阶段 {stage.value} 失败",
                node_history=ctx.history(),
            )

        # 阶段成功完成
        session.complete_stage(
            reason=f"阶段 {stage.value} 完成",
            triggered_by="pipeline",
        )

        return StageResult(
            stage=stage,
            status="success",
            summary=f"阶段 {stage.value} 完成",
            node_history=ctx.history(),
        )

    # ===== 全流程执行 =====

    def run_pipeline(
        self,
        project_id: str,
        topic: str,
        human_callback: Optional[HumanCallback] = None,
        stop_before: Optional[LifecycleStage] = None,
        resume: bool = False,
        force_writing: bool = False,
    ) -> PipelineResult:
        """运行全流程：research → ideation → design → experiment → writing(可选)。

        核心逻辑：
        1. 依次执行 5 个阶段
        2. experiment 阶段后检查 EXPERIMENT_OUTCOME：
           - success=True → 继续进入 writing
           - success=False → 停止流程，返回实验失败结果（不强行写论文）
        3. 人工节点通过 human_callback 处理
        4. force_writing=True 时绕过实验成败判断，强制进入 writing 阶段
           （仅用于 dry_run 下验证 writing 架构；真实模式应让实验成败自然决策）

        Args:
            project_id: 项目 ID
            topic: 研究主题
            human_callback: 人工节点回调
            stop_before: 在某阶段前停止（如 stop_before=WRITING 跳过写作）
            resume: 是否从已有项目恢复
            force_writing: 强制进入 writing 阶段（绕过实验成败判断）

        Returns:
            PipelineResult
        """
        # 初始化或恢复
        if resume:
            session, ctx = self.resume_project(project_id)
            # resume 模式下 ctx 是全新的，需恢复 topic 与 research 阶段产出，
            # 否则下游阶段（ideation/design/...）读不到 paper_ids / cross_validation_report，
            # 导致 brainstorm 拿到空输入、生成与主题无关的占位思路（串主题根因）
            ctx.set(RESEARCH_TOPIC, topic)
            if session.is_stage_done(LifecycleStage.RESEARCH):
                self._restore_research_outputs(ctx, project_id, topic)
        else:
            session, ctx = self.start_project(project_id, topic)

        result = PipelineResult(project_id=project_id, status="running")
        stages = LifecycleStage.ordered()

        for stage in stages:
            # stop_before 检查
            if stop_before is not None and stage == stop_before:
                result.status = "stopped"
                result.summary = f"在 {stage.value} 前停止（按需求跳过）"
                return result

            # 跳过已完成的阶段（resume 模式）
            if session.is_stage_done(stage):
                result.completed_stages.append(stage)
                continue

            # 执行阶段
            stage_result = self.run_stage(session, ctx, stage, human_callback)
            result.current_stage = stage
            result.node_history = stage_result.node_history

            if stage_result.status == "pending_human":
                result.status = "pending_human"
                result.summary = stage_result.summary
                return result

            if stage_result.status == "failed":
                result.status = "failed"
                result.summary = stage_result.summary
                result.recommendation = "检查失败节点，修复后重试该阶段"
                return result

            if stage_result.status == "aborted":
                result.status = "aborted"
                result.summary = stage_result.summary
                return result

            # 阶段成功
            result.completed_stages.append(stage)

            # ===== 关键：experiment 后判断是否进入 writing =====
            if stage == LifecycleStage.EXPERIMENT:
                outcome = ctx.get(EXPERIMENT_OUTCOME, {})
                result.experiment_outcome = outcome
                if force_writing:
                    # force_writing：绕过实验成败判断，强制进入 writing（架构验证用）
                    result.summary = (
                        "force_writing=True：绕过实验成败判断，强制进入 writing 阶段"
                    )
                elif not outcome.get("success", False):
                    # 实验失败是科研常态，不强行写论文
                    result.status = "experiment_failed"
                    result.summary = outcome.get(
                        "summary", "实验未验证核心 Claim"
                    )
                    result.recommendation = outcome.get(
                        "recommendation", "rollback_to_ideation"
                    )
                    return result

            # 前进到下一阶段
            next_stage = stage.next_stage()
            if next_stage is not None:
                # 检查是否要在下一阶段前停止
                if stop_before is not None and next_stage == stop_before:
                    result.status = "stopped"
                    result.summary = f"在 {next_stage.value} 前停止"
                    return result
                # 状态机前进
                if session.status_of(next_stage) == StageStatus.NOT_STARTED:
                    session.advance(
                        to_stage=next_stage,
                        reason=f"从 {stage.value} 前进到 {next_stage.value}",
                        triggered_by="pipeline",
                    )

        # 全部阶段完成
        result.status = "completed"
        result.summary = "全流程完成：调研→思路→方案→实验→写作"
        return result

    # ===== 路线 A：构效关系发现 =====

    def run_discovery(
        self,
        project_id: str,
        topic: str,
        human_callback: Optional[HumanCallback] = None,
        resume: bool = False,
    ) -> PipelineResult:
        """运行构效关系发现（路线 A）。

        流程：research 阶段（文献调研，产出 Research Gap + 论文）
              → discovery 子图（假设种子→搜索空间→LLM 引导搜索→验证→报告）

        discovery 不属于标准 5 阶段生命周期，作为 research 之后的可选扩展，
        专用于材料科学构效关系发现。复用 research 阶段的产出（论文 + 交叉验证报告）。

        Args:
            project_id: 项目 ID
            topic: 研究主题（如「热电材料的构效关系与性能优化」）
            human_callback: 人工节点回调
            resume: 是否从已有项目恢复（复用已完成的 research 阶段）

        Returns:
            PipelineResult，summary 含发现概览，node_history 含 discovery 节点历史
        """
        if resume:
            session, ctx = self.resume_project(project_id)
        else:
            session, ctx = self.start_project(project_id, topic)

        result = PipelineResult(project_id=project_id, status="running")

        # 1. 先跑 research 阶段（若未完成）
        if not session.is_stage_done(LifecycleStage.RESEARCH):
            research_result = self.run_stage(
                session, ctx, LifecycleStage.RESEARCH, human_callback
            )
            result.node_history = research_result.node_history
            if research_result.status == "pending_human":
                result.status = "pending_human"
                result.summary = research_result.summary
                return result
            if research_result.status in ("failed", "aborted"):
                result.status = research_result.status
                result.summary = f"research 阶段{research_result.status}: {research_result.summary}"
                return result
            result.completed_stages.append(LifecycleStage.RESEARCH)
        else:
            result.completed_stages.append(LifecycleStage.RESEARCH)
            # resume 模式：research 已完成但 ctx 是全新的，需从 KnowledgeStore 恢复 research 产出
            # （session 只持久化 stage_states，不持久化 ctx 域数据）
            self._restore_research_outputs(ctx, project_id, topic)

        # 2. 运行 discovery 子图
        from stages.discovery.graph import build_discovery_graph

        graph = build_discovery_graph()
        runner = GraphRunner(graph, ctx)
        ctx.current_stage = "discovery"

        if session.status_of(LifecycleStage.RESEARCH) == StageStatus.NOT_STARTED:
            session.start_stage(LifecycleStage.RESEARCH, triggered_by="pipeline")

        runner.start()

        # 处理人工节点阻塞
        while runner.is_pending_human():
            if human_callback is None:
                req = runner.pending_human_request()
                result.status = "pending_human"
                result.summary = "discovery 阶段等待人工输入"
                return result
            req = runner.pending_human_request()
            resp = human_callback(req)
            runner.resume_after_human(resp)

        if runner.is_aborted():
            result.status = "aborted"
            result.summary = "discovery 阶段被中止"
            result.node_history = ctx.history()
            return result

        final = runner.final_result()
        if final is not None and final.status == NodeStatus.FAILED:
            result.status = "failed"
            result.summary = final.summary or "discovery 阶段失败"
            result.node_history = ctx.history()
            return result

        # 3. 收集 discovery 产出
        hypotheses = ctx.get(DISCOVERY_HYPOTHESES, []) or []
        candidates = ctx.get(DISCOVERY_CANDIDATES, []) or []
        relationships = ctx.get(DISCOVERY_RELATIONSHIPS, []) or []
        report_id = ctx.get(DISCOVERY_REPORT_ARTIFACT_ID, "") or ""
        n_novel = sum(1 for r in relationships if r.get("novelty") == "novel")

        result.status = "completed"
        result.current_stage = None
        result.summary = (
            f"构效关系发现完成：{len(hypotheses)} 个假设 → "
            f"{len(candidates)} 个搜索候选 → {len(relationships)} 条验证发现"
            f"（{n_novel} 条 novel），报告 Artifact {report_id[:8] if report_id else '无'}"
        )
        result.node_history = ctx.history()
        result.recommendation = "discovery_completed"
        return result
