"""topic_discovery 阶段（方向推荐）。

在用户给定主题之前，主动分析领域趋势、发现新兴方向、推荐研究主题。
作为 research 阶段的可选前置入口，不修改原有流程。

节点拓扑：
    TrendFetchAgent（获取论文数据 + 提取关键词频率）
    → TrendAnalysisAgent（计算增长率，分类新兴/稳定/饱和方向）
    → TopicRecommendAgent（LLM 生成推荐主题 + 解释）
    → TopicSelectHuman（用户选择推荐主题，写入 RESEARCH_TOPIC）
"""
