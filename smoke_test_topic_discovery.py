"""Smoke test: topic_discovery pipeline in dry_run mode.

验证 4 节点链路 (trend_fetch → trend_analysis → topic_recommend → topic_select)
在 dry_run 模式下无报错跑通，且产出数据结构正确。
"""
import os
import sys

# 强制 dry_run
os.environ["SRA_DRY_RUN"] = "true"

# 确保项目根目录在 path 中
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from runtime.pipeline import Pipeline, PipelineResult


def fake_human_callback(req):
    """模拟用户选择第 1 个推荐主题。"""
    from core.orchestration.node import HumanResponse
    print(f"\n[Human Node] {req.prompt[:200]}...")
    print("[Human Node] 自动选择: ok (第1个推荐)")
    return HumanResponse(text="ok", action="continue")


def main():
    print("=" * 60)
    print("Smoke Test: topic_discovery pipeline (dry_run)")
    print("=" * 60)

    pipeline = Pipeline()
    print(f"[1] Pipeline 初始化完成, dry_run={pipeline.config.dry_run}")

    project_id = "smoke_test_topic_001"
    interest = "thermoelectric materials"

    print(f"[2] 启动 run_topic_discovery: project={project_id}, interest='{interest}'")

    result: PipelineResult = pipeline.run_topic_discovery(
        project_id=project_id,
        interest=interest,
        human_callback=fake_human_callback,
    )

    print(f"\n[3] 执行结果:")
    print(f"    status     = {result.status}")
    print(f"    summary    = {result.summary}")
    print(f"    extra keys = {list(result.extra.keys())}")

    # 验证状态
    assert result.status == "completed", f"Expected 'completed', got '{result.status}'"
    print("    [OK] status == 'completed'")

    # 验证 extra 数据
    extra = result.extra
    assert "recommendations" in extra, "extra 缺少 'recommendations'"
    assert "selected_topic" in extra, "extra 缺少 'selected_topic'"
    assert "interest" in extra, "extra 缺少 'interest'"
    print(f"    [OK] extra 包含 recommendations / selected_topic / interest")

    # 验证推荐列表
    recs = extra["recommendations"]
    assert isinstance(recs, list) and len(recs) >= 3, f"推荐列表应 >= 3 项, 实际 {len(recs)}"
    print(f"    [OK] 推荐列表长度 = {len(recs)}")

    # 验证每条推荐结构
    for i, rec in enumerate(recs):
        required_fields = ["topic", "rationale", "innovation_point",
                           "recommended_materials", "trend_summary",
                           "difficulty", "novelty",
                           "relevance", "popularity_score", "growth_rate"]
        missing = [f for f in required_fields if f not in rec]
        assert not missing, f"推荐[{i}] 缺少字段: {missing}"
        print(f"    [OK] 推荐[{i+1}]: {rec['topic'][:40]}... "
              f"(难度={rec['difficulty']}, 创新度={rec['novelty']}, "
              f"关联度={rec.get('relevance')}, 热门度={rec.get('popularity_score')})")

    # 验证选中的主题
    selected = extra["selected_topic"]
    assert selected, "selected_topic 不应为空"
    print(f"    [OK] 用户选中主题: {selected[:60]}")

    # 验证节点历史
    history = result.node_history
    print(f"\n[4] 节点执行历史 ({len(history)} 条):")
    for h in history:
        # 打印所有 key 以确认字段名
        print(f"    keys={list(h.keys())}")
        # 尝试常见字段名
        node_name = h.get("node") or h.get("node_name") or h.get("name") or "?"
        status = h.get("status", "?")
        summary = h.get("summary", "")[:80]
        print(f"    {node_name:25s} | {status:10s} | {summary}")

    # 检查节点历史中是否包含 4 个节点的执行记录
    # 用 summary 关键词匹配（因为字段名可能不同）
    history_summaries = " ".join(h.get("summary", "") for h in history)
    expected_keywords = ["趋势数据获取", "趋势分析", "主题推荐", "人工响应"]
    for kw in expected_keywords:
        assert kw in history_summaries, f"节点历史中未找到关键词 '{kw}'"
    print(f"\n    [OK] 4 个节点的执行记录均出现在历史中")

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
