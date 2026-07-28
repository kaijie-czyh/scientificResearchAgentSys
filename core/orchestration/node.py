"""编排节点定义。

节点类型：
- AgentNode: 调用 LLM/工具完成任务的节点
- HumanNode: 阻塞等待用户输入的节点
- ToolNode: 调用外部工具的节点
- CheckpointNode: 检查点（用于回滚）

每个节点：
- 显式声明 input_schema / output_schema（Pydantic 模型）
- execute 方法接收 ExecutionContext，返回 NodeResult
- 输入从 context 读取，输出写入 context
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Type

from pydantic import BaseModel

from core.orchestration.context import ContextKey, ExecutionContext


class NodeError(Exception):
    """节点执行错误。"""


class NodeStatus(str, Enum):
    """节点执行状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    PENDING_HUMAN = "pending_human"  # 等待人工输入
    BLOCKED = "blocked"


# ===== 节点 IO 抽象 =====

class NodeInput(BaseModel):
    """节点输入基类。

    子类化以声明具体节点的输入 schema。
    节点的输入通过 ContextKey 从 context 读取，组装为此模型。
    """

    model_config = {"arbitrary_types_allowed": True}


class NodeOutput(BaseModel):
    """节点输出基类。

    子类化以声明具体节点的输出 schema。
    节点的输出会被序列化并按 output_keys 写入 context。
    """

    model_config = {"arbitrary_types_allowed": True}


@dataclass
class NodeResult:
    """节点执行结果。"""

    status: NodeStatus
    output: Optional[NodeOutput] = None
    error: Optional[str] = None
    summary: str = ""  # 一句话摘要，用于历史记录
    # 若 status=PENDING_HUMAN，附带人工请求
    human_request: Optional["HumanRequest"] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class HumanRequest:
    """人工介入请求。

    HumanNode 执行后产生此请求，由 UI 层消费并向用户呈现。
    用户响应后，由 GraphRunner 注入回 HumanNode.continue_after_human。
    """

    prompt: str  # 向用户呈现的提示
    options: Optional[list[str]] = None  # 选项（可选）
    allow_free_text: bool = True  # 是否允许自由文本输入
    context: dict[str, Any] = field(default_factory=dict)  # 附加上下文


@dataclass
class HumanResponse:
    """人工响应。"""

    text: Optional[str] = None
    selected_option: Optional[str] = None
    # 用户可能选择"中止"或"回滚"
    action: str = "continue"  # continue / abort / rollback


# ===== 节点基类 =====

class Node(abc.ABC):
    """节点抽象基类。

    子类必须：
    - 声明 input_schema / output_schema
    - 实现 _build_input（从 context 组装输入）
    - 实现 _apply_output（把输出写回 context）
    - 实现 _execute（业务逻辑）
    """

    node_type: str = "abstract"
    input_schema: Type[NodeInput]
    output_schema: Type[NodeOutput]
    # 输出键：声明输出写入 context 的哪些键
    output_keys: dict[str, ContextKey] = {}

    def __init__(self, node_id: str):
        self.node_id = node_id

    def run(self, ctx: ExecutionContext) -> NodeResult:
        """执行入口。处理 IO 编排与异常捕获。"""
        try:
            # 1. 从 context 组装输入
            input_obj = self._build_input(ctx)
            # 2. 子类业务逻辑
            result = self._execute(input_obj, ctx)
            # 3. 把输出写回 context
            if result.status == NodeStatus.SUCCESS and result.output is not None:
                self._apply_output(result.output, ctx)
            return result
        except NodeError as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=str(e),
                summary=f"节点 {self.node_id} 失败: {e}",
            )
        except Exception as e:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"{type(e).__name__}: {e}",
                summary=f"节点 {self.node_id} 异常: {type(e).__name__}",
            )

    @abc.abstractmethod
    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        """从 context 组装输入。"""

    @abc.abstractmethod
    def _execute(
        self, input_obj: NodeInput, ctx: ExecutionContext
    ) -> NodeResult:
        """业务逻辑。"""

    def _apply_output(self, output: NodeOutput, ctx: ExecutionContext) -> None:
        """把输出字段按 output_keys 写回 context。

        默认实现：按字段名匹配 output_keys。
        子类可覆盖以实现更复杂的写入逻辑。
        """
        output_data = output.model_dump()
        for field_name, ctx_key in self.output_keys.items():
            if field_name in output_data:
                ctx.set(ctx_key, output_data[field_name])


# ===== 具体节点类型 =====

class AgentNode(Node):
    """调用 LLM 完成任务的节点。

    子类应：
    - 在 _execute 中调用 LLMRegistry.complete / structured_output
    - 把 LLM 返回组装为 output_schema 实例
    """

    node_type = "agent"
    task_type: str = ""  # 子类声明，用于 LLM 路由


class HumanNode(Node):
    """人工介入节点。

    执行时返回 PENDING_HUMAN 状态与 HumanRequest。
    GraphRunner 暂停执行，等待用户响应。
    用户响应后调用 continue_after_human 继续。
    """

    node_type = "human"

    def __init__(
        self,
        node_id: str,
        prompt: str = "",
        options: Optional[list[str]] = None,
        allow_free_text: bool = True,
    ):
        super().__init__(node_id)
        self._prompt = prompt
        self._options = options
        self._allow_free_text = allow_free_text

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        # HumanNode 通常不依赖输入，直接返回空 input
        return NodeInput()

    def _execute(
        self, input_obj: NodeInput, ctx: ExecutionContext
    ) -> NodeResult:
        # 渲染 prompt（可从 context 取变量）
        rendered = self._render_prompt(ctx)
        return NodeResult(
            status=NodeStatus.PENDING_HUMAN,
            summary=f"等待人工输入: {rendered[:80]}",
            human_request=HumanRequest(
                prompt=rendered,
                options=self._options,
                allow_free_text=self._allow_free_text,
                context={"node_id": self.node_id},
            ),
        )

    def _render_prompt(self, ctx: ExecutionContext) -> str:
        """子类可覆盖以从 context 取变量渲染 prompt。"""
        return self._prompt

    def continue_after_human(
        self,
        response: HumanResponse,
        ctx: ExecutionContext,
    ) -> NodeResult:
        """用户响应后继续执行。

        子类可覆盖以处理响应并产生输出。
        默认实现：把响应文本写入 context，标记成功。
        """
        if response.action == "abort":
            return NodeResult(
                status=NodeStatus.FAILED,
                error="用户中止",
                summary=f"节点 {self.node_id} 被用户中止",
            )
        if response.action == "rollback":
            return NodeResult(
                status=NodeStatus.BLOCKED,
                summary=f"节点 {self.node_id} 用户请求回滚",
            )
        # 默认：把响应写入 context（若声明了输出键）
        output = self._build_output_from_response(response, ctx)
        if output is not None:
            self._apply_output(output, ctx)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"节点 {self.node_id} 收到人工响应",
        )

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        """从人工响应构造输出。子类可覆盖。"""
        return None


class ToolNode(Node):
    """调用外部工具的节点。

    工具示例：论文下载、PDF 解析、实验运行、文件读写。
    """

    node_type = "tool"


class CheckpointNode(Node):
    """检查点节点。

    执行时对 context 做快照，便于回滚。
    """

    node_type = "checkpoint"

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _execute(
        self, input_obj: NodeInput, ctx: ExecutionContext
    ) -> NodeResult:
        snapshot = ctx.snapshot()
        ctx.set(
            ContextKey[dict](f"checkpoint.{self.node_id}"),
            snapshot,
        )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            summary=f"检查点 {self.node_id} 已保存",
        )
