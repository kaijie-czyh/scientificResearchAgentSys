"""5 个 build_*_graph 函数的拓扑校验测试。

对 research / ideation / design / experiment / writing 五个阶段：
- 返回的 Graph 调用 validate() 不抛异常
- 节点数 >= 3
- entry_node 与 exit_node 不同
- 至少有一个 node_type == "checkpoint" 的节点（StageCheckpoint）
"""
from __future__ import annotations

import pytest

from core.orchestration.graph import Graph
from stages import (
    build_design_graph,
    build_experiment_graph,
    build_ideation_graph,
    build_research_graph,
    build_writing_graph,
)


# ===== 参数化：5 个 build_*_graph 函数 =====

BUILD_FUNCS = [
    pytest.param(build_research_graph, id="research"),
    pytest.param(build_ideation_graph, id="ideation"),
    pytest.param(build_design_graph, id="design"),
    pytest.param(build_experiment_graph, id="experiment"),
    pytest.param(build_writing_graph, id="writing"),
]


@pytest.mark.parametrize("build_func", BUILD_FUNCS)
def test_build_graph_returns_graph_instance(build_func):
    """build_*_graph 应返回 Graph 实例。"""
    graph = build_func()
    assert isinstance(graph, Graph)


@pytest.mark.parametrize("build_func", BUILD_FUNCS)
def test_build_graph_validates_without_error(build_func):
    """build_*_graph 返回的图应能通过 validate()（不抛异常）。"""
    graph = build_func()
    # 再次调用 validate 不抛异常即通过
    graph.validate()


@pytest.mark.parametrize("build_func", BUILD_FUNCS)
def test_build_graph_has_at_least_3_nodes(build_func):
    """每个图至少有 3 个节点。"""
    graph = build_func()
    assert len(graph.nodes) >= 3


@pytest.mark.parametrize("build_func", BUILD_FUNCS)
def test_build_graph_entry_and_exit_are_different(build_func):
    """entry_node 与 exit_node 应不同。"""
    graph = build_func()
    assert graph.entry_node is not None
    assert graph.exit_node is not None
    assert graph.entry_node != graph.exit_node


@pytest.mark.parametrize("build_func", BUILD_FUNCS)
def test_build_graph_has_at_least_one_checkpoint_node(build_func):
    """每个图至少有一个 node_type == 'checkpoint' 的节点（StageCheckpoint）。"""
    graph = build_func()
    node_types = [node.node_type for node in graph.nodes.values()]
    assert "checkpoint" in node_types, (
        f"图 {graph.name} 应至少包含一个 checkpoint 节点，实际 node_types={node_types}"
    )


@pytest.mark.parametrize("build_func", BUILD_FUNCS)
def test_build_graph_entry_node_exists_in_nodes(build_func):
    """entry_node 应在 nodes 字典中。"""
    graph = build_func()
    assert graph.entry_node in graph.nodes


@pytest.mark.parametrize("build_func", BUILD_FUNCS)
def test_build_graph_exit_node_exists_in_nodes(build_func):
    """exit_node 应在 nodes 字典中。"""
    graph = build_func()
    assert graph.exit_node in graph.nodes


@pytest.mark.parametrize("build_func", BUILD_FUNCS)
def test_build_graph_has_edges(build_func):
    """每个图应至少有 2 条边（保证 DAG 连通）。"""
    graph = build_func()
    assert len(graph.edges) >= 2


@pytest.mark.parametrize("build_func", BUILD_FUNCS)
def test_build_graph_stage_field_matches_expected(build_func):
    """每个图的 stage 字段应是非空字符串。"""
    graph = build_func()
    assert graph.stage, f"图 {graph.name} 的 stage 字段不应为空"
    assert isinstance(graph.stage, str)


# ===== 单独验证各图的具体拓扑（保证不被参数化掩盖） =====


def test_research_graph_has_5_nodes():
    """research 图应有 5 个节点（topic_refine, cp, topic_confirm, paper_fetch, paper_ingest）。"""
    graph = build_research_graph()
    assert len(graph.nodes) == 5
    assert graph.entry_node == "topic_refine"
    assert graph.exit_node == "paper_ingest"


def test_ideation_graph_has_5_nodes():
    """ideation 图应有 5 个节点。"""
    graph = build_ideation_graph()
    assert len(graph.nodes) == 5
    assert graph.entry_node == "brainstorm"
    assert graph.exit_node == "claim_draft"


def test_design_graph_has_5_nodes():
    """design 图应有 5 个节点。"""
    graph = build_design_graph()
    assert len(graph.nodes) == 5
    assert graph.entry_node == "method_formalize"
    assert graph.exit_node == "method_artifact"


def test_experiment_graph_has_5_nodes():
    """experiment 图应有 5 个节点。"""
    graph = build_experiment_graph()
    assert len(graph.nodes) == 5
    assert graph.entry_node == "experiment_config"
    assert graph.exit_node == "claim_verify"


def test_writing_graph_has_6_nodes():
    """writing 图应有 6 个节点。"""
    graph = build_writing_graph()
    assert len(graph.nodes) == 6
    assert graph.entry_node == "provenance_check"
    assert graph.exit_node == "revise"


# ===== 各图 checkpoint 节点 ID 验证 =====


def test_research_graph_has_checkpoint_named_cp_before_confirm():
    """research 图应包含名为 cp_before_confirm 的 checkpoint。"""
    graph = build_research_graph()
    assert "cp_before_confirm" in graph.nodes
    assert graph.nodes["cp_before_confirm"].node_type == "checkpoint"


def test_ideation_graph_has_checkpoint_named_cp_before_validate():
    """ideation 图应包含名为 cp_before_validate 的 checkpoint。"""
    graph = build_ideation_graph()
    assert "cp_before_validate" in graph.nodes
    assert graph.nodes["cp_before_validate"].node_type == "checkpoint"


def test_design_graph_has_checkpoint_named_cp_before_review():
    """design 图应包含名为 cp_before_review 的 checkpoint。"""
    graph = build_design_graph()
    assert "cp_before_review" in graph.nodes
    assert graph.nodes["cp_before_review"].node_type == "checkpoint"


def test_experiment_graph_has_checkpoint_named_cp_before_anomaly():
    """experiment 图应包含名为 cp_before_anomaly 的 checkpoint。"""
    graph = build_experiment_graph()
    assert "cp_before_anomaly" in graph.nodes
    assert graph.nodes["cp_before_anomaly"].node_type == "checkpoint"


def test_writing_graph_has_checkpoint_named_cp_before_draft():
    """writing 图应包含名为 cp_before_draft 的 checkpoint。"""
    graph = build_writing_graph()
    assert "cp_before_draft" in graph.nodes
    assert graph.nodes["cp_before_draft"].node_type == "checkpoint"


# ===== 各图至少包含一个人工节点 =====


def test_research_graph_has_human_node():
    """research 图应包含 HumanNode（topic_confirm）。"""
    from core.orchestration.node import HumanNode

    graph = build_research_graph()
    human_nodes = [n for n in graph.nodes.values() if isinstance(n, HumanNode)]
    assert len(human_nodes) >= 1


def test_ideation_graph_has_human_node():
    """ideation 图应包含 HumanNode（idea_discuss）。"""
    from core.orchestration.node import HumanNode

    graph = build_ideation_graph()
    human_nodes = [n for n in graph.nodes.values() if isinstance(n, HumanNode)]
    assert len(human_nodes) >= 1


def test_design_graph_has_human_node():
    """design 图应包含 HumanNode（method_review）。"""
    from core.orchestration.node import HumanNode

    graph = build_design_graph()
    human_nodes = [n for n in graph.nodes.values() if isinstance(n, HumanNode)]
    assert len(human_nodes) >= 1


def test_writing_graph_has_human_node():
    """writing 图应包含 HumanNode（revise）。"""
    from core.orchestration.node import HumanNode

    graph = build_writing_graph()
    human_nodes = [n for n in graph.nodes.values() if isinstance(n, HumanNode)]
    assert len(human_nodes) >= 1
