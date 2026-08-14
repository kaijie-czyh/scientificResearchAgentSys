"""Graph 拓扑校验 + GraphRunner 执行测试。"""
from __future__ import annotations

from typing import Optional

import pytest

from core.orchestration.context import ContextKey, ExecutionContext
from core.orchestration.graph import (
    Graph,
    GraphExecutionError,
    GraphRunner,
    GraphValidationError,
)
from core.orchestration.node import (
    AgentNode,
    CheckpointNode,
    HumanNode,
    HumanResponse,
    NodeInput,
    NodeOutput,
    NodeResult,
    NodeStatus,
)


# ===== 测试用 IO schema =====


class _HumanTextOutput(NodeOutput):
    """_NoopHuman 的输出 schema。"""

    text: str = ""


# ===== 测试用节点 =====


class _NoopAgent(AgentNode):
    """空 Agent：直接返回 SUCCESS。"""

    node_type = "test_noop_agent"
    input_schema = NodeInput
    output_schema = NodeOutput
    output_keys = {}

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _execute(self, input_obj: NodeInput, ctx: ExecutionContext) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, summary="noop done")


class _NoopHuman(HumanNode):
    """空 HumanNode：用父类默认实现，写入响应文本。"""

    node_type = "test_noop_human"
    input_schema = NodeInput
    output_schema = _HumanTextOutput
    output_keys = {"text": ContextKey[str]("test.human_text")}

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        return _HumanTextOutput(text=response.text or "")


# ===== Graph.add_node / add_edge =====


def test_add_node_sets_entry_and_exit_on_first_node():
    """add_node 第一个节点应同时成为 entry 与 exit。"""
    graph = Graph(name="g")
    graph.add_node(_NoopAgent("n1"))

    assert graph.entry_node == "n1"
    assert graph.exit_node == "n1"


def test_add_node_updates_exit_on_subsequent_nodes():
    """后续 add_node 应更新 exit_node。"""
    graph = Graph(name="g")
    graph.add_node(_NoopAgent("n1"))
    graph.add_node(_NoopAgent("n2"))

    assert graph.entry_node == "n1"
    assert graph.exit_node == "n2"


def test_add_node_rejects_duplicate_id():
    """add_node 重复 ID 应抛 GraphValidationError。"""
    graph = Graph(name="g")
    graph.add_node(_NoopAgent("n1"))
    with pytest.raises(GraphValidationError):
        graph.add_node(_NoopAgent("n1"))


def test_add_edge_rejects_unknown_source():
    """add_edge 源节点不存在应抛 GraphValidationError。"""
    graph = Graph(name="g")
    graph.add_node(_NoopAgent("n1"))
    with pytest.raises(GraphValidationError):
        graph.add_edge("unknown", "n1")


def test_add_edge_rejects_unknown_target():
    """add_edge 目标节点不存在应抛 GraphValidationError。"""
    graph = Graph(name="g")
    graph.add_node(_NoopAgent("n1"))
    with pytest.raises(GraphValidationError):
        graph.add_edge("n1", "unknown")


def test_add_edge_returns_graph_for_chaining():
    """add_edge 应返回 self 以支持链式调用。"""
    graph = Graph(name="g")
    graph.add_node(_NoopAgent("n1"))
    graph.add_node(_NoopAgent("n2"))

    result = graph.add_edge("n1", "n2")

    assert result is graph


# ===== Graph.validate =====


def test_validate_rejects_empty_graph():
    """空图应抛 GraphValidationError。"""
    graph = Graph(name="empty")
    with pytest.raises(GraphValidationError):
        graph.validate()


def test_validate_rejects_cycle():
    """含环的图应抛 GraphValidationError。"""
    graph = Graph(name="cyclic")
    graph.add_node(_NoopAgent("a"))
    graph.add_node(_NoopAgent("b"))
    graph.add_edge("a", "b")
    graph.add_edge("b", "a")  # 形成环

    with pytest.raises(GraphValidationError):
        graph.validate()


def test_validate_rejects_unreachable_node():
    """从入口不可达的节点应抛 GraphValidationError。"""
    graph = Graph(name="unreachable")
    graph.add_node(_NoopAgent("entry"))
    graph.add_node(_NoopAgent("reachable"))
    graph.add_node(_NoopAgent("orphan"))  # 不可达
    graph.add_edge("entry", "reachable")

    with pytest.raises(GraphValidationError):
        graph.validate()


def test_validate_passes_for_simple_linear_graph():
    """线性 DAG 应通过校验。"""
    graph = Graph(name="linear")
    graph.add_node(_NoopAgent("a"))
    graph.add_node(_NoopAgent("b"))
    graph.add_node(_NoopAgent("c"))
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")

    # 不抛异常即通过
    graph.validate()


def test_validate_passes_for_diamond_dag():
    """菱形 DAG 应通过校验。"""
    graph = Graph(name="diamond")
    graph.add_node(_NoopAgent("entry"))
    graph.add_node(_NoopAgent("top"))
    graph.add_node(_NoopAgent("bottom"))
    graph.add_node(_NoopAgent("exit"))
    graph.add_edge("entry", "top")
    graph.add_edge("entry", "bottom")
    graph.add_edge("top", "exit")
    graph.add_edge("bottom", "exit")

    graph.validate()


# ===== successors / predecessors =====


def test_successors_returns_downstream_nodes():
    """successors 应返回所有直接后继。"""
    graph = Graph(name="g")
    graph.add_node(_NoopAgent("a"))
    graph.add_node(_NoopAgent("b"))
    graph.add_node(_NoopAgent("c"))
    graph.add_edge("a", "b")
    graph.add_edge("a", "c")

    assert set(graph.successors("a")) == {"b", "c"}
    assert graph.successors("b") == []


def test_predecessors_returns_upstream_nodes():
    """predecessors 应返回所有直接前驱。"""
    graph = Graph(name="g")
    graph.add_node(_NoopAgent("a"))
    graph.add_node(_NoopAgent("b"))
    graph.add_node(_NoopAgent("c"))
    graph.add_edge("a", "c")
    graph.add_edge("b", "c")

    assert set(graph.predecessors("c")) == {"a", "b"}
    assert graph.predecessors("a") == []


# ===== GraphRunner 基本执行 =====


def _build_linear_graph() -> Graph:
    """构造 a → b → c 的线性图。"""
    graph = Graph(name="linear")
    graph.add_node(_NoopAgent("a"))
    graph.add_node(_NoopAgent("b"))
    graph.add_node(_NoopAgent("c"))
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    return graph


def test_runner_start_executes_all_nodes_in_linear_graph():
    """线性图中 start 应执行所有节点直到 current_node_id 为 None。"""
    graph = _build_linear_graph()
    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)

    runner.start()

    assert runner.is_completed()
    assert not runner.is_pending_human()
    assert not runner.is_aborted()


def test_runner_records_history_during_execution():
    """执行过程中应在 context.history 中记录每个节点。"""
    graph = _build_linear_graph()
    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)

    runner.start()

    history = ctx.history()
    assert len(history) == 3
    assert [h["node_id"] for h in history] == ["a", "b", "c"]


def test_runner_final_result_returns_last_result():
    """final_result 应返回最后执行的 NodeResult。"""
    graph = _build_linear_graph()
    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)

    runner.start()

    result = runner.final_result()
    assert result is not None
    assert result.status == NodeStatus.SUCCESS


def test_runner_state_shows_completed_nodes():
    """state 应显示已完成的节点集合。"""
    graph = _build_linear_graph()
    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)

    runner.start()

    state = runner.state()
    assert state.completed_nodes == {"a", "b", "c"}
    assert state.current_node_id is None


# ===== GraphRunner 人工节点阻塞 =====


def test_runner_blocks_at_human_node():
    """含 HumanNode 的图：start 后应停在 human 节点，is_pending_human() 为 True。"""
    # 构造 a → human → b
    graph = Graph(name="with_human")
    graph.add_node(_NoopAgent("a"))
    graph.add_node(_NoopHuman("human", prompt="请确认"))
    graph.add_node(_NoopAgent("b"))
    graph.add_edge("a", "human")
    graph.add_edge("human", "b")

    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)

    runner.start()

    # 应停在 human 节点
    assert runner.is_pending_human()
    assert not runner.is_completed()
    # pending_human_request 应返回 HumanRequest
    req = runner.pending_human_request()
    assert req is not None
    assert "请确认" in req.prompt


def test_runner_resume_after_human_completes_graph():
    """resume_after_human 后图应继续执行直到完成。"""
    graph = Graph(name="with_human")
    graph.add_node(_NoopAgent("a"))
    graph.add_node(_NoopHuman("human", prompt="请确认"))
    graph.add_node(_NoopAgent("b"))
    graph.add_edge("a", "human")
    graph.add_edge("human", "b")

    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)

    runner.start()
    assert runner.is_pending_human()

    runner.resume_after_human(HumanResponse(text="ok"))

    assert runner.is_completed()
    assert not runner.is_pending_human()
    # context 中应有 human 节点写入的响应文本
    assert ctx.get(ContextKey[str]("test.human_text")) == "ok"
    # fork PR #2 后 human 节点会同时记录 pending_human + success 两次历史，故总计 4 条
    assert len(ctx.history()) == 4
    # 确认 human 节点在历史里出现两次（pending → success）
    human_history = [h for h in ctx.history() if h["node_id"] == "human"]
    assert len(human_history) == 2
    assert human_history[0]["status"] == "pending_human"
    assert human_history[1]["status"] == "success"


def test_runner_resume_without_pending_human_raises():
    """无 pending human 时 resume_after_human 应抛 GraphExecutionError。"""
    graph = _build_linear_graph()
    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)
    runner.start()  # 直接完成

    with pytest.raises(GraphExecutionError):
        runner.resume_after_human(HumanResponse(text="x"))


def test_runner_human_node_at_exit_completes_after_resume():
    """HumanNode 作为 exit 时 resume 后也应完成。"""
    graph = Graph(name="human_exit")
    graph.add_node(_NoopAgent("a"))
    graph.add_node(_NoopHuman("human", prompt="最后确认"))
    graph.add_edge("a", "human")

    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)

    runner.start()
    assert runner.is_pending_human()

    runner.resume_after_human(HumanResponse(text="final"))
    assert runner.is_completed()


# ===== GraphRunner abort =====


def test_runner_abort_sets_aborted_state():
    """abort 后 is_aborted 应为 True。"""
    graph = _build_linear_graph()
    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)

    runner.start()
    runner.abort()

    assert runner.is_aborted()


# ===== GraphRunner 构造时校验 =====


def test_runner_constructor_validates_graph():
    """GraphRunner 构造时应校验图，非法图应抛 GraphValidationError。"""
    graph = Graph(name="empty")  # 空图
    ctx = ExecutionContext(project_id="p1")

    with pytest.raises(GraphValidationError):
        GraphRunner(graph, ctx)


# ===== CheckpointNode 集成 =====


def test_runner_records_last_checkpoint_node_id():
    """执行含 CheckpointNode 的图时，state.last_checkpoint_node_id 应被记录。"""
    graph = Graph(name="with_cp")
    graph.add_node(_NoopAgent("a"))
    graph.add_node(CheckpointNode("cp1"))
    graph.add_node(_NoopAgent("b"))
    graph.add_edge("a", "cp1")
    graph.add_edge("cp1", "b")

    ctx = ExecutionContext(project_id="p1")
    runner = GraphRunner(graph, ctx)

    runner.start()

    assert runner.state().last_checkpoint_node_id == "cp1"
