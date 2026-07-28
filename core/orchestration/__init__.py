"""Agent 编排层。

提供 DAG 编排框架，每个生命周期是一个子图。

核心抽象：
- Node: 节点基类（AgentNode / HumanNode / ToolNode / CheckpointNode）
- ExecutionContext: 节点间上下文传递
- Graph: DAG 图，定义节点与边
- GraphRunner: 图执行引擎（拓扑序执行，支持人工节点阻塞）
- NodeRegistry: 节点类型注册表

设计原则：
- 节点 IO 用 Pydantic 模型显式声明（验证导向）
- HumanNode 阻塞等待用户输入
- 失败可回退到最近的 CheckpointNode
- 节点无状态，所有状态走 ExecutionContext
"""
from core.orchestration.node import (
    Node,
    NodeInput,
    NodeOutput,
    NodeResult,
    NodeStatus,
    AgentNode,
    HumanNode,
    HumanResponse,
    ToolNode,
    CheckpointNode,
    NodeError,
)
from core.orchestration.context import ExecutionContext, ContextKey
from core.orchestration.graph import (
    Graph,
    GraphRunner,
    GraphExecutionError,
    GraphValidationError,
)
from core.orchestration.registry import NodeRegistry, register_node_type

__all__ = [
    "Node",
    "NodeInput",
    "NodeOutput",
    "NodeResult",
    "NodeStatus",
    "AgentNode",
    "HumanNode",
    "HumanResponse",
    "ToolNode",
    "CheckpointNode",
    "NodeError",
    "ExecutionContext",
    "ContextKey",
    "Graph",
    "GraphRunner",
    "GraphExecutionError",
    "GraphValidationError",
    "NodeRegistry",
    "register_node_type",
]
