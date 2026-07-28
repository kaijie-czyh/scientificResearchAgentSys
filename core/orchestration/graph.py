"""编排图与执行引擎。

Graph: DAG 图，定义节点与边
GraphRunner: 执行引擎，拓扑序执行，处理人工节点阻塞与失败回退

设计原则：
- 图结构静态声明（构造时确定拓扑），执行时动态决策
- 人工节点阻塞：GraphRunner 暂停，暴露 pending 状态，等待外部 continue
- 失败回退：失败时回退到最近的 CheckpointNode
- 子图支持：每个生命周期是一个子图，子图可作为节点嵌入父图
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.orchestration.context import ExecutionContext
from core.orchestration.node import (
    HumanResponse,
    Node,
    NodeResult,
    NodeStatus,
)


class GraphValidationError(Exception):
    """图结构校验错误。"""


class GraphExecutionError(Exception):
    """图执行错误。"""


@dataclass
class Graph:
    """DAG 图。

    nodes: 节点字典 {node_id: Node}
    edges: 边列表 [(source_id, target_id)]
    entry_node: 入口节点 ID
    exit_node: 出口节点 ID
    """

    name: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    entry_node: Optional[str] = None
    exit_node: Optional[str] = None
    # 当前阶段标识（生命周期 stage 名）
    stage: str = ""

    def add_node(self, node: Node) -> "Graph":
        if node.node_id in self.nodes:
            raise GraphValidationError(f"节点已存在: {node.node_id}")
        self.nodes[node.node_id] = node
        if self.entry_node is None:
            self.entry_node = node.node_id
        self.exit_node = node.node_id
        return self

    def add_edge(self, source_id: str, target_id: str) -> "Graph":
        if source_id not in self.nodes:
            raise GraphValidationError(f"源节点不存在: {source_id}")
        if target_id not in self.nodes:
            raise GraphValidationError(f"目标节点不存在: {target_id}")
        self.edges.append((source_id, target_id))
        return self

    def validate(self) -> None:
        """校验图结构。"""
        if not self.nodes:
            raise GraphValidationError("图为空")
        if self.entry_node is None or self.entry_node not in self.nodes:
            raise GraphValidationError(f"入口节点无效: {self.entry_node}")
        if self.exit_node is None or self.exit_node not in self.nodes:
            raise GraphValidationError(f"出口节点无效: {self.exit_node}")
        # 检查无环（DAG）
        self._check_acyclic()
        # 检查从入口可达所有节点
        self._check_reachable()

    def _check_acyclic(self) -> None:
        """拓扑排序检测环。"""
        in_degree: dict[str, int] = {nid: 0 for nid in self.nodes}
        adj: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for src, dst in self.edges:
            adj[src].append(dst)
            in_degree[dst] += 1

        queue = [nid for nid, d in in_degree.items() if d == 0]
        visited = 0
        while queue:
            n = queue.pop()
            visited += 1
            for m in adj[n]:
                in_degree[m] -= 1
                if in_degree[m] == 0:
                    queue.append(m)

        if visited != len(self.nodes):
            raise GraphValidationError("图存在环，无法拓扑排序")

    def _check_reachable(self) -> None:
        """从入口节点 BFS，检查可达性。"""
        adj: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for src, dst in self.edges:
            adj[src].append(dst)
        visited: set[str] = set()
        queue = [self.entry_node]
        while queue:
            n = queue.pop(0)
            if n in visited:
                continue
            visited.add(n)
            queue.extend(adj.get(n, []))
        unreachable = set(self.nodes.keys()) - visited
        if unreachable:
            raise GraphValidationError(
                f"以下节点从入口不可达: {unreachable}"
            )

    def successors(self, node_id: str) -> list[str]:
        return [dst for src, dst in self.edges if src == node_id]

    def predecessors(self, node_id: str) -> list[str]:
        return [src for src, dst in self.edges if dst == node_id]


@dataclass
class GraphRunnerState:
    """执行引擎状态。"""

    current_node_id: Optional[str] = None
    pending_human_node_id: Optional[str] = None  # 等待人工响应的节点
    last_checkpoint_node_id: Optional[str] = None  # 最近的检查点
    completed_nodes: set[str] = field(default_factory=set)
    failed_nodes: set[str] = field(default_factory=set)
    aborted: bool = False


class GraphRunner:
    """图执行引擎。

    使用范式：
        runner = GraphRunner(graph, ctx)
        runner.start()
        while runner.is_pending_human():
            req = runner.pending_human_request()
            # 呈现给用户，获取响应
            response = ...
            runner.resume_after_human(response)
        result = runner.final_result()
    """

    def __init__(self, graph: Graph, ctx: ExecutionContext):
        graph.validate()
        self._graph = graph
        self._ctx = ctx
        self._state = GraphRunnerState()
        self._last_result: Optional[NodeResult] = None

    # ===== 执行控制 =====

    def start(self) -> None:
        """从入口节点开始执行。"""
        self._state = GraphRunnerState()
        self._state.current_node_id = self._graph.entry_node
        self._step()

    def resume_after_human(self, response: HumanResponse) -> None:
        """人工响应后继续执行。"""
        if self._state.pending_human_node_id is None:
            raise GraphExecutionError("当前无等待人工响应的节点")
        node = self._graph.nodes[self._state.pending_human_node_id]
        # 必须是 HumanNode 才能接收人工响应
        from core.orchestration.node import HumanNode

        if not isinstance(node, HumanNode):
            raise GraphExecutionError(
                f"节点 {node.node_id} 不是 HumanNode，无法接收人工响应"
            )

        result = node.continue_after_human(response, self._ctx)
        self._last_result = result
        self._state.pending_human_node_id = None

        if result.status == NodeStatus.SUCCESS:
            self._state.completed_nodes.add(node.node_id)
            self._record_result(node, result)
            self._advance()
        elif result.status == NodeStatus.FAILED:
            self._state.failed_nodes.add(node.node_id)
            self._handle_failure(node, result)
        elif result.status == NodeStatus.BLOCKED:
            # 用户请求回滚
            self._rollback_to_checkpoint()

    def abort(self) -> None:
        """中止执行。"""
        self._state.aborted = True

    # ===== 状态查询 =====

    def is_pending_human(self) -> bool:
        return self._state.pending_human_node_id is not None

    def pending_human_request(self):
        """获取当前等待的人工请求。"""
        if self._state.pending_human_node_id is None:
            return None
        if self._last_result is None or self._last_result.human_request is None:
            return None
        return self._last_result.human_request

    def is_completed(self) -> bool:
        return (
            self._state.current_node_id is None
            and not self.is_pending_human()
            and not self._state.aborted
        )

    def is_aborted(self) -> bool:
        return self._state.aborted

    def final_result(self) -> Optional[NodeResult]:
        return self._last_result

    def state(self) -> GraphRunnerState:
        return self._state

    # ===== 内部实现 =====

    def _step(self) -> None:
        """执行当前节点，根据结果决定下一步。"""
        while self._state.current_node_id is not None:
            node_id = self._state.current_node_id
            node = self._graph.nodes[node_id]
            self._ctx.current_node_id = node_id

            # 记录检查点
            from core.orchestration.node import CheckpointNode

            if isinstance(node, CheckpointNode):
                self._state.last_checkpoint_node_id = node_id

            result = node.run(self._ctx)
            self._last_result = result

            if result.status == NodeStatus.SUCCESS:
                self._state.completed_nodes.add(node_id)
                self._record_result(node, result)
                self._advance()
            elif result.status == NodeStatus.PENDING_HUMAN:
                self._state.pending_human_node_id = node_id
                self._record_result(node, result)
                return  # 阻塞，等待外部 resume
            elif result.status == NodeStatus.SKIPPED:
                self._record_result(node, result)
                self._advance()
            elif result.status == NodeStatus.FAILED:
                self._state.failed_nodes.add(node_id)
                self._record_result(node, result)
                self._handle_failure(node, result)
                return
            else:
                raise GraphExecutionError(
                    f"未知节点状态: {result.status} (节点 {node_id})"
                )

    def _advance(self) -> None:
        """推进到下一个节点。"""
        current = self._state.current_node_id
        if current is None:
            return
        successors = self._graph.successors(current)
        if not successors:
            # 到达出口
            self._state.current_node_id = None
            return
        if len(successors) == 1:
            self._state.current_node_id = successors[0]
        else:
            # 多后继：简单起见，取第一个未完成的
            # 复杂分支决策应由专门的 DecisionNode 处理
            next_nodes = [s for s in successors if s not in self._state.completed_nodes]
            if not next_nodes:
                self._state.current_node_id = None
            else:
                self._state.current_node_id = next_nodes[0]

    def _handle_failure(self, node: Node, result: NodeResult) -> None:
        """处理节点失败。尝试回退到最近的检查点，否则停止。"""
        if self._state.last_checkpoint_node_id is not None:
            # 从 context 取检查点快照（由 CheckpointNode 写入）
            from core.orchestration.context import ContextKey

            snapshot = self._ctx.get(
                ContextKey[dict](f"checkpoint.{self._state.last_checkpoint_node_id}")
            )
            if snapshot is not None:
                self._ctx.restore(snapshot)
                self._state.current_node_id = self._state.last_checkpoint_node_id
                self._state.failed_nodes.clear()
                return
        # 无检查点，停止
        self._state.current_node_id = None

    def _rollback_to_checkpoint(self) -> None:
        """用户请求回滚。"""
        if self._state.last_checkpoint_node_id is None:
            self._state.aborted = True
            return
        from core.orchestration.context import ContextKey

        snapshot = self._ctx.get(
            ContextKey[dict](f"checkpoint.{self._state.last_checkpoint_node_id}")
        )
        if snapshot is None:
            self._state.aborted = True
            return
        self._ctx.restore(snapshot)
        self._state.current_node_id = self._state.last_checkpoint_node_id
        self._state.failed_nodes.clear()
        self._state.pending_human_node_id = None

    def _record_result(self, node: Node, result: NodeResult) -> None:
        self._ctx.record_node_result(
            node_id=node.node_id,
            node_type=node.node_type,
            status=result.status.value,
            summary=result.summary,
        )
