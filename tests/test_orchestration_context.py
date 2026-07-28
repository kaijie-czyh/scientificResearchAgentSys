"""ExecutionContext 测试。"""
from __future__ import annotations

from typing import List

from core.orchestration.context import ContextKey, ExecutionContext


# ===== ContextKey =====


def test_context_key_construction_preserves_name():
    """ContextKey 构造后 name 应保留。"""
    key = ContextKey[str]("test.key")
    assert key.name == "test.key"


def test_context_key_str_returns_name():
    """str(ContextKey) 应返回 name。"""
    key = ContextKey[int]("counter")
    assert str(key) == "counter"


def test_context_key_generic_type_param_does_not_affect_runtime():
    """ContextKey[T] 的类型参数仅用于类型检查，运行时无影响。"""
    k1 = ContextKey[str]("same_name")
    k2 = ContextKey[int]("same_name")
    # 同名应视为同一键
    assert k1.name == k2.name


# ===== get / set / has / delete / keys =====


def test_get_returns_default_for_missing_key():
    """get 未设置的键应返回 default。"""
    ctx = ExecutionContext(project_id="p1")
    key = ContextKey[str]("missing")

    assert ctx.get(key, default="fallback") == "fallback"
    # 默认 default=None
    assert ctx.get(key) is None


def test_set_and_get_round_trip():
    """set 后 get 应返回相同值。"""
    ctx = ExecutionContext(project_id="p1")
    key = ContextKey[List[str]]("papers")

    ctx.set(key, ["p1", "p2"])
    assert ctx.get(key) == ["p1", "p2"]


def test_set_overwrites_existing_value():
    """set 已存在的键应覆盖旧值。"""
    ctx = ExecutionContext(project_id="p1")
    key = ContextKey[int]("count")

    ctx.set(key, 1)
    ctx.set(key, 99)
    assert ctx.get(key) == 99


def test_has_returns_true_only_for_set_keys():
    """has 应只在键被 set 后返回 True。"""
    ctx = ExecutionContext(project_id="p1")
    key = ContextKey[str]("x")

    assert not ctx.has(key)
    ctx.set(key, "value")
    assert ctx.has(key)


def test_delete_removes_key():
    """delete 应移除键。"""
    ctx = ExecutionContext(project_id="p1")
    key = ContextKey[str]("x")
    ctx.set(key, "v")

    ctx.delete(key)

    assert not ctx.has(key)
    assert ctx.get(key) is None


def test_delete_missing_key_is_noop():
    """delete 不存在的键应为 no-op，不抛异常。"""
    ctx = ExecutionContext(project_id="p1")
    ctx.delete(ContextKey[str]("nonexistent"))  # 不抛异常


def test_keys_returns_all_set_key_names():
    """keys 应返回所有已设置键的 name 列表。"""
    ctx = ExecutionContext(project_id="p1")
    ctx.set(ContextKey[str]("a"), 1)
    ctx.set(ContextKey[str]("b"), 2)
    ctx.set(ContextKey[str]("c"), 3)

    keys = ctx.keys()
    assert set(keys) == {"a", "b", "c"}


def test_keys_returns_empty_for_fresh_context():
    """新 context 的 keys 应为空。"""
    ctx = ExecutionContext(project_id="p1")
    assert ctx.keys() == []


# ===== record_node_result / history =====


def test_record_node_result_appends_to_history():
    """record_node_result 应追加一条历史记录。"""
    ctx = ExecutionContext(project_id="p1")

    ctx.record_node_result(
        node_id="n1",
        node_type="agent",
        status="success",
        summary="节点1完成",
    )

    history = ctx.history()
    assert len(history) == 1
    assert history[0]["node_id"] == "n1"
    assert history[0]["node_type"] == "agent"
    assert history[0]["status"] == "success"
    assert history[0]["summary"] == "节点1完成"
    assert "timestamp" in history[0]


def test_history_returns_copy_not_reference():
    """history 应返回副本，外部修改不影响内部状态。"""
    ctx = ExecutionContext(project_id="p1")
    ctx.record_node_result(
        node_id="n1", node_type="agent", status="success", summary="x"
    )

    h1 = ctx.history()
    h1.append({"fake": "entry"})

    h2 = ctx.history()
    assert len(h2) == 1  # 外部追加不影响内部


def test_record_multiple_results_preserves_order():
    """多次 record_node_result 应保持顺序。"""
    ctx = ExecutionContext(project_id="p1")
    for i in range(5):
        ctx.record_node_result(
            node_id=f"n{i}", node_type="agent", status="success", summary=str(i)
        )

    history = ctx.history()
    assert [h["node_id"] for h in history] == ["n0", "n1", "n2", "n3", "n4"]


# ===== snapshot / restore =====


def test_snapshot_captures_data_and_history():
    """snapshot 应深拷贝当前 data 与 history。"""
    ctx = ExecutionContext(project_id="p1")
    ctx.set(ContextKey[List[str]]("data"), ["a", "b"])
    ctx.record_node_result(
        node_id="n1", node_type="agent", status="success", summary="x"
    )

    snap = ctx.snapshot()

    assert snap["project_id"] == "p1"
    assert snap["data"] == {"data": ["a", "b"]}
    assert len(snap["history"]) == 1


def test_snapshot_is_deep_copy():
    """snapshot 应是深拷贝：修改原 context 不影响快照。"""
    ctx = ExecutionContext(project_id="p1")
    key = ContextKey[List[str]]("list")
    ctx.set(key, ["a"])
    snap = ctx.snapshot()

    # 修改原 context
    ctx.get(key).append("b")

    assert snap["data"]["list"] == ["a"]  # 快照不变


def test_restore_recovers_data_and_history():
    """restore 应把 context 恢复到快照时点。"""
    ctx = ExecutionContext(project_id="p1")
    ctx.set(ContextKey[str]("x"), "v1")
    ctx.record_node_result(
        node_id="n1", node_type="agent", status="success", summary="x"
    )
    snap = ctx.snapshot()

    # 修改 context
    ctx.set(ContextKey[str]("x"), "v2")
    ctx.record_node_result(
        node_id="n2", node_type="agent", status="success", summary="y"
    )

    # 恢复
    ctx.restore(snap)

    assert ctx.get(ContextKey[str]("x")) == "v1"
    assert len(ctx.history()) == 1
    assert ctx.history()[0]["node_id"] == "n1"


def test_restore_overwrites_current_state_completely():
    """restore 应完全覆盖当前状态。"""
    ctx = ExecutionContext(project_id="p1", current_stage="research")
    ctx.set(ContextKey[str]("a"), "1")
    ctx.set(ContextKey[str]("b"), "2")

    snap = ctx.snapshot()
    # 清空 context
    ctx.set(ContextKey[str]("a"), None)
    ctx.delete(ContextKey[str]("b"))
    ctx.set(ContextKey[str]("c"), "3")

    ctx.restore(snap)

    assert ctx.get(ContextKey[str]("a")) == "1"
    assert ctx.get(ContextKey[str]("b")) == "2"
    assert not ctx.has(ContextKey[str]("c"))


# ===== view =====


def test_view_returns_summary_dict():
    """view 应返回含 project_id / current_stage / data_keys / history_length 的 dict。"""
    ctx = ExecutionContext(project_id="p1", current_stage="research")
    ctx.set(ContextKey[str]("k1"), "v1")
    ctx.set(ContextKey[str]("k2"), "v2")
    ctx.record_node_result(
        node_id="n1", node_type="agent", status="success", summary="x"
    )

    view = ctx.view()

    assert view["project_id"] == "p1"
    assert view["current_stage"] == "research"
    assert set(view["data_keys"]) == {"k1", "k2"}
    assert view["history_length"] == 1


# ===== current_stage / current_node_id 字段 =====


def test_context_default_current_stage_is_empty():
    """ExecutionContext 默认 current_stage 为空串。"""
    ctx = ExecutionContext(project_id="p1")
    assert ctx.current_stage == ""
    assert ctx.current_node_id == ""


def test_context_fields_can_be_updated_directly():
    """current_stage / current_node_id 可直接赋值更新。"""
    ctx = ExecutionContext(project_id="p1")
    ctx.current_stage = "ideation"
    ctx.current_node_id = "node_42"

    assert ctx.current_stage == "ideation"
    assert ctx.current_node_id == "node_42"
