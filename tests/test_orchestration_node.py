"""Node / AgentNode / HumanNode / CheckpointNode 测试。"""
from __future__ import annotations

from typing import Optional

import pytest

from core.orchestration.context import ContextKey, ExecutionContext
from core.orchestration.node import (
    AgentNode,
    CheckpointNode,
    HumanNode,
    HumanRequest,
    HumanResponse,
    NodeInput,
    NodeOutput,
    NodeResult,
    NodeStatus,
)


# ===== 测试用 IO schema =====


class _AddOutput(NodeOutput):
    """_AddAgent 的输出 schema。"""

    result: int = 0


class _ConfirmOutput(NodeOutput):
    """_ConfirmHuman 的输出 schema。"""

    confirmed_text: str = ""


# ===== 测试用具体节点类 =====


class _AddAgent(AgentNode):
    """测试用 AgentNode：把 input.value + offset 写入 context。"""

    node_type = "test_add_agent"
    input_schema = NodeInput
    output_schema = _AddOutput
    output_keys = {"result": ContextKey[int]("test.result")}

    def __init__(self, node_id: str, offset: int = 0):
        super().__init__(node_id)
        self._offset = offset

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _execute(self, input_obj: NodeInput, ctx: ExecutionContext) -> NodeResult:
        base = ctx.get(ContextKey[int]("test.base"), default=0)
        result = base + self._offset
        output = _AddOutput(result=result)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"加 {self._offset} 得 {result}",
        )


class _FailingAgent(AgentNode):
    """测试用 AgentNode：总是抛异常。"""

    node_type = "test_failing_agent"
    input_schema = NodeInput
    output_schema = NodeOutput
    output_keys = {}

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _execute(self, input_obj: NodeInput, ctx: ExecutionContext) -> NodeResult:
        raise RuntimeError("故意失败")


class _ConfirmHuman(HumanNode):
    """测试用 HumanNode：把响应文本写入 context。"""

    node_type = "test_confirm_human"
    input_schema = NodeInput
    output_schema = _ConfirmOutput
    output_keys = {"confirmed_text": ContextKey[str]("test.confirmed")}

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        return _ConfirmOutput(confirmed_text=response.text or "")


# ===== Node.run 流程 =====


def test_agent_node_run_writes_output_to_context():
    """AgentNode.run 成功时应把 output 写入 context。"""
    ctx = ExecutionContext(project_id="p1")
    ctx.set(ContextKey[int]("test.base"), 10)
    node = _AddAgent("add_5", offset=5)

    result = node.run(ctx)

    assert result.status == NodeStatus.SUCCESS
    assert ctx.get(ContextKey[int]("test.result")) == 15


def test_agent_node_run_summary_is_recorded():
    """AgentNode.run 应在 result.summary 中携带执行摘要。"""
    ctx = ExecutionContext(project_id="p1")
    ctx.set(ContextKey[int]("test.base"), 0)
    node = _AddAgent("add_1", offset=1)

    result = node.run(ctx)

    assert result.summary


def test_failing_node_returns_failed_result_with_error():
    """抛异常的节点应返回 FAILED 状态与 error 信息。"""
    ctx = ExecutionContext(project_id="p1")
    node = _FailingAgent("fail")

    result = node.run(ctx)

    assert result.status == NodeStatus.FAILED
    assert result.error is not None
    assert "RuntimeError" in result.error


def test_node_run_does_not_write_output_on_failure():
    """失败的节点不应把 output 写入 context。"""
    ctx = ExecutionContext(project_id="p1")
    node = _FailingAgent("fail")

    result = node.run(ctx)

    assert result.output is None


# ===== HumanNode =====


def test_human_node_execute_returns_pending_human():
    """HumanNode._execute 应返回 PENDING_HUMAN 状态与 HumanRequest。"""
    ctx = ExecutionContext(project_id="p1")
    node = _ConfirmHuman("confirm", prompt="请确认")

    result = node.run(ctx)

    assert result.status == NodeStatus.PENDING_HUMAN
    assert result.human_request is not None
    assert isinstance(result.human_request, HumanRequest)
    assert "请确认" in result.human_request.prompt


def test_human_node_does_not_write_output_on_pending():
    """PENDING_HUMAN 时不应写 output 到 context。"""
    ctx = ExecutionContext(project_id="p1")
    node = _ConfirmHuman("confirm", prompt="x")

    node.run(ctx)

    assert not ctx.has(ContextKey[str]("test.confirmed"))


def test_human_node_continue_after_human_writes_output():
    """continue_after_human 应把响应文本写入 context 并返回 SUCCESS。"""
    ctx = ExecutionContext(project_id="p1")
    node = _ConfirmHuman("confirm", prompt="x")
    # 先 run 触发 PENDING_HUMAN
    node.run(ctx)

    result = node.continue_after_human(
        HumanResponse(text="user confirmed"), ctx
    )

    assert result.status == NodeStatus.SUCCESS
    assert ctx.get(ContextKey[str]("test.confirmed")) == "user confirmed"


def test_human_node_continue_after_abort_returns_failed():
    """action=abort 时 continue_after_human 应返回 FAILED。"""
    ctx = ExecutionContext(project_id="p1")
    node = _ConfirmHuman("confirm", prompt="x")

    result = node.continue_after_human(
        HumanResponse(action="abort"), ctx
    )

    assert result.status == NodeStatus.FAILED
    assert "中止" in result.error


def test_human_node_continue_after_rollback_returns_blocked():
    """action=rollback 时 continue_after_human 应返回 BLOCKED。"""
    ctx = ExecutionContext(project_id="p1")
    node = _ConfirmHuman("confirm", prompt="x")

    result = node.continue_after_human(
        HumanResponse(action="rollback"), ctx
    )

    assert result.status == NodeStatus.BLOCKED


def test_human_node_init_options_preserved():
    """HumanNode 初始化时的 options / allow_free_text 应保留在请求中。"""
    ctx = ExecutionContext(project_id="p1")
    node = _ConfirmHuman(
        "confirm",
        prompt="选择",
        options=["A", "B"],
        allow_free_text=False,
    )

    result = node.run(ctx)

    assert result.human_request.options == ["A", "B"]
    assert result.human_request.allow_free_text is False


# ===== CheckpointNode =====


def test_checkpoint_node_saves_snapshot_to_context():
    """CheckpointNode.run 应把当前 context 快照写入 checkpoint.{node_id} 键。"""
    ctx = ExecutionContext(project_id="p1", current_stage="research")
    ctx.set(ContextKey[str]("x"), "v1")
    node = CheckpointNode("cp1")

    result = node.run(ctx)

    assert result.status == NodeStatus.SUCCESS
    snap_key = ContextKey[dict]("checkpoint.cp1")
    snap = ctx.get(snap_key)
    assert snap is not None
    assert snap["project_id"] == "p1"
    assert snap["data"] == {"x": "v1"}


def test_checkpoint_node_distinct_keys_per_node_id():
    """不同 node_id 的 CheckpointNode 应写入不同的 context 键。"""
    ctx = ExecutionContext(project_id="p1")
    ctx.set(ContextKey[str]("v"), "1")
    CheckpointNode("cp_a").run(ctx)

    ctx.set(ContextKey[str]("v"), "2")
    CheckpointNode("cp_b").run(ctx)

    snap_a = ctx.get(ContextKey[dict]("checkpoint.cp_a"))
    snap_b = ctx.get(ContextKey[dict]("checkpoint.cp_b"))
    assert snap_a["data"]["v"] == "1"
    assert snap_b["data"]["v"] == "2"


# ===== NodeStatus 枚举 =====


def test_node_status_has_5_members():
    """NodeStatus 应有 5 个成员。"""
    members = list(NodeStatus)
    assert len(members) == 5
    assert NodeStatus.SUCCESS in members
    assert NodeStatus.FAILED in members
    assert NodeStatus.SKIPPED in members
    assert NodeStatus.PENDING_HUMAN in members
    assert NodeStatus.BLOCKED in members


# ===== NodeResult / HumanRequest / HumanResponse 数据类 =====


def test_node_result_default_timestamp():
    """NodeResult 默认 timestamp 应为 datetime 实例。"""
    from datetime import datetime

    result = NodeResult(status=NodeStatus.SUCCESS)
    assert isinstance(result.timestamp, datetime)


def test_human_request_default_options_is_none():
    """HumanRequest 默认 options 应为 None。"""
    req = HumanRequest(prompt="x")
    assert req.options is None
    assert req.allow_free_text is True
    assert req.context == {}


def test_human_response_default_action_is_continue():
    """HumanResponse 默认 action 应为 'continue'。"""
    resp = HumanResponse(text="ok")
    assert resp.action == "continue"
    assert resp.text == "ok"
    assert resp.selected_option is None
