"""topic_discovery 阶段 Agent / Human 节点实现。

节点拓扑（方向推荐）：
    TrendFetchAgent（获取论文数据 + 提取关键词频率）
    → TrendAnalysisAgent（计算增长率，分类新兴/稳定/饱和方向）
    → TopicRecommendAgent（LLM 生成推荐主题 + 解释）
    → TopicSelectHuman（用户选择推荐主题，写入 RESEARCH_TOPIC）

执行模式：
- dry_run=True  ：用占位趋势数据返回，不调用 LLM、不访问 arXiv API
- dry_run=False ：真实调用 arXiv API 获取论文，真实调用 MiniMax M3 生成推荐
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from core.llm import LLMRegistry
from core.orchestration.context import ExecutionContext
from core.orchestration.node import (
    AgentNode,
    HumanNode,
    HumanResponse,
    NodeInput,
    NodeOutput,
    NodeResult,
    NodeStatus,
)
from core.tools.trend_analyzer import (
    TrendData,
    compute_growth_rates,
    fetch_keyword_trends,
    placeholder_trends,
)

from stages.common import (
    DRY_RUN,
    LLM_REGISTRY,
    RESEARCH_TOPIC,
    TOPIC_DISCOVERY_ANALYSIS,
    TOPIC_DISCOVERY_INTEREST,
    TOPIC_DISCOVERY_RECOMMENDATIONS,
    TOPIC_DISCOVERY_SELECTED_TOPIC,
    TOPIC_DISCOVERY_TRENDS,
)

from stages.topic_discovery.io_schema import (
    TrendAnalysisInput,
    TrendAnalysisOutput,
    TrendFetchInput,
    TrendFetchOutput,
    TopicRecommendInput,
    TopicRecommendOutput,
    TopicSelectOutput,
)

logger = logging.getLogger(__name__)


# ===== 结构化输出 Schema（供 LLM structured_output 使用）=====

class TopicRecommendationItem(BaseModel):
    """单条推荐主题 schema。"""

    topic: str = Field(description="推荐的研究主题名称（简洁明确）")
    rationale: str = Field(description="为什么值得研究（引用趋势数据与领域分析）")
    innovation_point: str = Field(description="创新切入点（具体可操作的研究方向）")
    recommended_materials: list[str] = Field(
        default_factory=list,
        description="推荐探索的材料体系（如 Bi2Te3, CsPbBr3）",
    )
    trend_summary: str = Field(description="该方向的趋势摘要（增长率、论文数等）")
    difficulty: str = Field(description="实现难度：easy / medium / hard")
    novelty: str = Field(description="创新程度：low / medium / high")
    # ===== 新增维度（热门 → 难度/创新性/关联性 结构化评估）=====
    relevance: str = Field(description="与用户研究兴趣的关联度：low / medium / high")
    popularity_score: int = Field(description="热门度量化值 0-100（综合增长率与论文总量归一化）")
    growth_rate: float = Field(
        default=0.0, description="该方向对应关键词的年度增长率（如 0.35 表示 35%）"
    )


class TopicRecommendationSchema(BaseModel):
    """主题推荐输出 schema。"""

    recommendations: list[TopicRecommendationItem] = Field(
        description="3-5 个推荐研究主题，按推荐优先级排序"
    )


class HotDirectionItem(BaseModel):
    """第一步：热门方向锁定。"""

    keyword: str = Field(description="热门关键词/方向名")
    popularity_score: int = Field(description="热门度 0-100")
    trend_summary: str = Field(description="一句话趋势摘要（增长率、论文数）")


class HotDirectionSchema(BaseModel):
    """热门方向列表输出 schema（第一步）。"""

    directions: list[HotDirectionItem] = Field(
        description="按热门度降序的 5-8 个热门方向"
    )


# ===== TrendFetchAgent =====

class TrendFetchAgent(AgentNode):
    """趋势数据获取 Agent。

    根据用户研究兴趣，调用 arXiv API 获取论文，
    从标题/摘要中提取关键词并按年份统计频率。

    产出 TOPIC_DISCOVERY_TRENDS，供后续趋势分析与主题推荐使用。
    """

    node_type = "topic_trend_fetch"
    task_type = "topic_trend_analysis"
    input_schema = TrendFetchInput
    output_schema = TrendFetchOutput
    output_keys = {
        "trend_data": TOPIC_DISCOVERY_TRENDS,
    }

    DEFAULT_MAX_RESULTS = 200

    def _build_input(self, ctx: ExecutionContext) -> TrendFetchInput:
        interest = ctx.get(TOPIC_DISCOVERY_INTEREST, "")
        return TrendFetchInput(interest=interest)

    def _execute(self, input_obj: TrendFetchInput, ctx: ExecutionContext) -> NodeResult:
        dry_run: bool = ctx.get(DRY_RUN, True)

        if dry_run:
            trend_data = placeholder_trends(input_obj.interest).to_dict()
        else:
            try:
                trend_data = fetch_keyword_trends(
                    query=input_obj.interest,
                    max_results=self.DEFAULT_MAX_RESULTS,
                ).to_dict()
            except Exception as e:
                logger.warning("TrendFetch 真实调用失败，回退占位: %s", e)
                trend_data = placeholder_trends(input_obj.interest).to_dict()

        # 确保查询关键词存入 context（供下游节点使用）
        ctx.set(TOPIC_DISCOVERY_INTEREST, input_obj.interest)

        output = TrendFetchOutput(trend_data=trend_data)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"趋势数据获取完成：{trend_data.get('total_fetched', 0)} 篇论文，"
                f"{len(trend_data.get('keyword_frequencies', {}))} 个关键词"
            ),
        )


# ===== TrendAnalysisAgent =====

class TrendAnalysisAgent(AgentNode):
    """趋势分析 Agent。

    对关键词年度频率数据计算增长率，分类为：
    - emerging（新兴方向）：最近一年增长率 > 50%
    - stable（稳定方向）：增长率 10%-50%
    - saturated（饱和方向）：增长率 < 10% 或负增长

    纯统计计算，不调用 LLM。
    """

    node_type = "topic_trend_analysis"
    task_type = "topic_trend_analysis"
    input_schema = TrendAnalysisInput
    output_schema = TrendAnalysisOutput
    output_keys = {
        "analysis": TOPIC_DISCOVERY_ANALYSIS,
    }

    def _build_input(self, ctx: ExecutionContext) -> TrendAnalysisInput:
        return TrendAnalysisInput(
            trend_data=ctx.get(TOPIC_DISCOVERY_TRENDS, {})
        )

    def _execute(
        self, input_obj: TrendAnalysisInput, ctx: ExecutionContext
    ) -> NodeResult:
        keyword_frequencies = input_obj.trend_data.get("keyword_frequencies", {})

        if not keyword_frequencies:
            analysis = {
                "emerging": [],
                "stable": [],
                "saturated": [],
                "all_keywords": [],
            }
        else:
            analysis = compute_growth_rates(keyword_frequencies)

        output = TrendAnalysisOutput(analysis=analysis)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=(
                f"趋势分析完成：{len(analysis['emerging'])} 个新兴方向，"
                f"{len(analysis['stable'])} 个稳定方向，"
                f"{len(analysis['saturated'])} 个饱和方向"
            ),
        )


# ===== TopicRecommendAgent =====

class TopicRecommendAgent(AgentNode):
    """主题推荐 Agent。

    根据趋势分析结果，调用 LLM 生成 3-5 个推荐研究主题。
    每个主题包含：名称、理由、创新切入点、推荐材料体系、趋势摘要、难度、创新程度。

    LLM 深度参与：
    - 解读趋势数据的科学含义
    - 结合材料科学领域知识推荐具体材料体系
    - 评估创新性与可行性
    """

    node_type = "topic_recommend"
    task_type = "topic_recommend"
    input_schema = TopicRecommendInput
    output_schema = TopicRecommendOutput
    output_keys = {
        "recommendations": TOPIC_DISCOVERY_RECOMMENDATIONS,
    }

    def _build_input(self, ctx: ExecutionContext) -> TopicRecommendInput:
        return TopicRecommendInput(
            interest=ctx.get(TOPIC_DISCOVERY_INTEREST, ""),
            trend_data=ctx.get(TOPIC_DISCOVERY_TRENDS, {}),
            analysis=ctx.get(TOPIC_DISCOVERY_ANALYSIS, {}),
        )

    def _execute(
        self, input_obj: TopicRecommendInput, ctx: ExecutionContext
    ) -> NodeResult:
        registry: Optional[LLMRegistry] = ctx.get(LLM_REGISTRY)
        dry_run: bool = ctx.get(DRY_RUN, True)

        if not dry_run and registry is not None:
            try:
                recommendations = self._real_recommend(input_obj, registry)
            except Exception as e:
                logger.warning("TopicRecommend 真实调用失败，回退占位: %s", e)
                recommendations = self._placeholder(input_obj)
        else:
            recommendations = self._placeholder(input_obj)

        output = TopicRecommendOutput(recommendations=recommendations)
        return NodeResult(
            status=NodeStatus.SUCCESS,
            output=output,
            summary=f"主题推荐完成：生成 {len(recommendations)} 个推荐主题",
        )

    def _real_recommend(
        self, input_obj: TopicRecommendInput, registry: LLMRegistry
    ) -> list[dict]:
        """真实调用 LLM 生成推荐（两步推理）。

        第一步：从趋势数据锁定热门方向并量化热门度（popularity_score 0-100）。
        第二步：基于热门方向逐条产出推荐，评估难度/创新性/关联性。
        """
        # 构造趋势数据摘要
        analysis = input_obj.analysis
        emerging = analysis.get("emerging", [])[:10]
        stable = analysis.get("stable", [])[:10]
        saturated = analysis.get("saturated", [])[:5]

        def fmt_trend_list(items: list[dict]) -> str:
            lines = []
            for item in items:
                kw = item.get("keyword", "")
                gr = item.get("growth_rate", 0)
                tc = item.get("total_count", 0)
                trend = item.get("trend", [])
                trend_str = ", ".join(f"{t['year']}:{t['count']}" for t in trend)
                lines.append(f"  {kw} (增长率:{gr:.0%}, 总数:{tc}, 趋势: {trend_str})")
            return "\n".join(lines)

        trend_block = (
            f"【新兴方向（增长率>50%）】\n{fmt_trend_list(emerging)}\n\n"
            f"【稳定方向（增长率10%-50%）】\n{fmt_trend_list(stable)}\n\n"
            f"【饱和方向（增长率<10%，避免投入）】\n{fmt_trend_list(saturated)}"
        )

        # 样本论文
        sample_papers = input_obj.trend_data.get("sample_papers", [])[:10]
        paper_block = "\n".join(
            f"  - {p.get('title', 'N/A')} ({p.get('year', '?')})"
            for p in sample_papers
        )

        # ===== 第一步：锁定热门方向并量化热门度 =====
        step1_system = (
            "你是科研趋势分析专家。你的任务是【先锁定热门研究方向】：\n"
            "1. 结合用户研究兴趣与趋势数据，找出最值得投入的 5-8 个热门方向；\n"
            "2. 描述方向时不要停留在泛泛的标签词（如“机器学习”“纳米”“优化”），\n"
            "   要落到**具体的材料体系、科学问题或关键技术手段**上，例如\n"
            "   “钙钛矿太阳能电池的缺陷钝化”“锂硫电池多硫化物穿梭效应的抑制”，\n"
            "   让每个方向一听就知道它在解决什么具体问题；\n"
            "3. 为每个方向量化热门度 popularity_score（0-100）：\n"
            "   - 增长率高且论文总量大 → 高分（80-100，热门且活跃）\n"
            "   - 增长率高但总量小 → 中高分（60-79，新兴但需注意风险）\n"
            "   - 增长率平稳 → 中分（40-59）\n"
            "   - 饱和/衰退 → 低分（<40，尽量避免）\n"
            "4. 按热门度从高到低排序。\n"
            "只输出方向清单，不要展开推荐主题。"
        )
        step1_prompt = (
            f"用户研究兴趣：{input_obj.interest}\n\n"
            f"趋势分析结果：\n{trend_block}\n\n"
            f"近期论文样本：\n{paper_block}"
        )
        hot = registry.structured_output(
            task_type=self.task_type,
            output_schema=HotDirectionSchema,
            system=step1_system,
            prompt=step1_prompt,
            temperature_override=0.7,
        )

        # ===== 第二步：基于热门方向逐条产出完整推荐 =====
        hot_block = "\n".join(
            f"  - {d.keyword}（popularity={d.popularity_score}，{d.trend_summary}）"
            for d in hot.directions
        )
        # 供 LLM 参考的多样化命名单模式（体现措辞的多样性，而非固定模板）
        _name_patterns = (
            "主题命名的多样化参考（不要照抄其中任何一个，仅体会其措辞差异）：\n"
            "  · “XX缺陷钝化实现钙钛矿电池效率新突破”——机制链 + 应用目标\n"
            "  · “高熵 XX 的组分设计空间与性能边界”——材料体系 + 科学问题\n"
            "  · “XX 中载流子-声子协同调控的多尺度建模”——机理 + 方法\n"
            "  · “面向 XX 场景的 YY 结构工程与可扩展制备”——应用 + 工艺\n"
            "  · “小样本数据下实现 XX 的主动学习筛选框架”——方法 + 对象\n"
            "  · “XX 的界面/缺陷工程：从原子尺度到器件尺度”——尺度 + 视角"
        )
        step2_system = (
            "你是材料科学研究顾问。在上一步锁定的热门方向基础上，"
            "产出 3-5 个推荐研究主题。\n\n"
            "【主题命名要求（最重要）】\n"
            "1. 每条主题的名称必须**整体语感像一位材料科学家手写的论文题目**，"
            "用具体术语（材料名、机理、方法名）与动作性短语来组织，\n"
            "   不要用“XX的核心动机与痛点”“未来的探索方向”“XX的机遇与挑战”这类评论性套话；\n"
            "2. 5 条推荐之间**句式结构必须明显不同**——禁止共用同一个动宾模板；\n"
            "   比较下面两组，前者是合格的差异化，后者是失败的雷同：\n"
            "   合格：A. “微量 Sm 掺杂对 SnTe 热电性能的双峰调控机制”\n"
            "        B. “基于相场模拟的钙钛矿晶粒粗化动力学与稳定性”\n"
            "        C. “面向柔性电子的超薄 Bi2Te3 膜的晶格应变工程”\n"
            "   失败：A. “XX的性能突破研究” / B. “XX的优化与筛选” / C. “XX的机制研究”；\n"
            "3. 每条主题从**不同的切入视角**出发，可参考但不限于：\n"
            "   材料体系创新（组分/掺杂/高熵）、工艺与制备方法、性能维度（热电\光学\力学\\n"
            "   各一性能指标）、应用场景（柔性/可穿戴/高温/低温）、多尺度理解（原子-介观-宏观）、\n"
            "   数据驱动（主动学习/生成模型/符号回归）；\n"
            "4. 若两个主题落在同一材料体系，必须用不同的科学问题或手段加以区分，\n"
            "   宁可刀状细分也不要选题近似；\n"
            "5. 主题名控制在 15-30 字，信息密度高，去掉“基于”“关于”等冗余介词。\n\n"
            "【其余字段要求】\n"
            "1. rationale 引用趋势数据（增长率、论文数等），并说明该主题**为什么是一个值得做的空缺**；\n"
            "2. innovation_point 给出具体可操作的切入点（具体材料/具体方法/具体指标）；\n"
            "3. 给出推荐材料体系（如 Bi2Te3, CsPbBr3），尽量各不相同；\n"
            "4. 评估实现难度 difficulty（easy/medium/hard）、创新度 novelty（low/medium/high）、\n"
            "5. 评估与用户研究兴趣的关联度 relevance（low/medium/high）；\n"
            "5. popularity_score 与上一步热门度保持一致（0-100）；\n"
            "6. growth_rate 填该方向对应关键词的年度增长率（浮点，如 0.35）。\n"
            "避免推荐饱和方向（增长率<10%）。\n\n"
            f"{_name_patterns}"
        )
        step2_prompt = (
            f"用户研究兴趣：{input_obj.interest}\n\n"
            f"上一步锁定的热门方向（按热门度降序）：\n{hot_block}\n\n"
            f"趋势分析结果：\n{trend_block}\n\n"
            f"近期论文样本：\n{paper_block}"
        )
        result = registry.structured_output(
            task_type=self.task_type,
            output_schema=TopicRecommendationSchema,
            system=step2_system,
            prompt=step2_prompt,
            temperature_override=0.8,
        )

        recs = [item.model_dump() for item in result.recommendations]
        # 服务端按热门度降序排序（保证 ctx 顺序 = 展示顺序 = 前端选中索引一致）
        recs.sort(key=lambda r: r.get("popularity_score", 0), reverse=True)
        return recs

    @staticmethod
    def _placeholder(input_obj: TopicRecommendInput) -> list[dict]:
        """占位推荐数据（dry_run 模式用）。"""
        return [
            {
                "topic": f"基于高熵合金化的{input_obj.interest}性能突破研究",
                "rationale": "该方向关键词增长率 120%，目前仅 3 篇相关论文，竞争度低",
                "innovation_point": "使用机器学习辅助筛选高熵合金元素组合",
                "recommended_materials": ["Bi2Te3", "Sb2Te3", "高熵合金化体系"],
                "trend_summary": "high-entropy 增长 120%，alloy 增长 80%",
                "difficulty": "medium",
                "novelty": "high",
                "relevance": "high",
                "popularity_score": 88,
                "growth_rate": 1.2,
            },
            {
                "topic": f"机器学习驱动的{input_obj.interest}材料筛选与优化",
                "rationale": "machine learning 关键词增长率 150%，是当前最热门交叉方向",
                "innovation_point": "结合主动学习与贝叶斯优化加速材料筛选",
                "recommended_materials": ["perovskite", "oxide", "halide"],
                "trend_summary": "machine learning 增长 150%，screening 增长 90%",
                "difficulty": "medium",
                "novelty": "high",
                "relevance": "high",
                "popularity_score": 75,
                "growth_rate": 1.5,
            },
            {
                "topic": f"纳米结构工程提升{input_obj.interest}性能的机制研究",
                "rationale": "nanostructure 增长稳定 30%，是持续活跃的稳定方向",
                "innovation_point": "结合第一性原理计算与实验验证纳米界面的声子散射机制",
                "recommended_materials": ["Bi2Te3", "nanocomposite", "superlattice"],
                "trend_summary": "nanostructure 增长 30%，phonon 增长 25%",
                "difficulty": "hard",
                "novelty": "medium",
                "relevance": "medium",
                "popularity_score": 55,
                "growth_rate": 0.3,
            },
        ]


# ===== TopicSelectHuman =====

class TopicSelectHuman(HumanNode):
    """用户选择推荐主题。

    展示 LLM 推荐的研究主题列表，用户选择一个主题后
    写入 RESEARCH_TOPIC，接入原有 research 流程。

    用户也可以选择自定义主题（输入自由文本）。
    """

    node_type = "topic_select"
    input_schema = NodeInput
    output_schema = TopicSelectOutput
    output_keys = {
        "selected_topic": TOPIC_DISCOVERY_SELECTED_TOPIC,
        "confirmed": RESEARCH_TOPIC,  # 同时写入 RESEARCH_TOPIC
    }

    def _build_input(self, ctx: ExecutionContext) -> NodeInput:
        return NodeInput()

    def _render_prompt(self, ctx: ExecutionContext) -> str:
        recommendations = ctx.get(TOPIC_DISCOVERY_RECOMMENDATIONS, [])
        if not recommendations:
            return (
                "暂无推荐主题。请直接输入你想研究的主题，"
                "系统将进入文献调研流程。"
            )

        lines = ["系统根据领域趋势分析，推荐以下研究主题：\n"]
        for i, rec in enumerate(recommendations):
            lines.append(
                f"  [{i+1}] {rec.get('topic', 'N/A')}\n"
                f"      理由：{rec.get('rationale', 'N/A')}\n"
                f"      创新点：{rec.get('innovation_point', 'N/A')}\n"
                f"      推荐材料：{', '.join(rec.get('recommended_materials', []))}\n"
                f"      难度：{rec.get('difficulty', 'N/A')} | "
                f"创新度：{rec.get('novelty', 'N/A')}\n"
            )
        lines.append(
            "\n请选择：\n"
            "  - 输入数字（如 1）选择对应推荐主题\n"
            "  - 或输入自定义主题（直接输入文本）\n"
            "  - 输入 'ok' 选择第 1 个推荐"
        )
        return "\n".join(lines)

    def _build_output_from_response(
        self, response: HumanResponse, ctx: ExecutionContext
    ) -> Optional[NodeOutput]:
        recommendations = ctx.get(TOPIC_DISCOVERY_RECOMMENDATIONS, [])
        text = (response.text or "").strip()

        if not text or text.lower() in ("ok", "确认", "y", "yes"):
            # 默认选择第 1 个推荐
            if recommendations:
                selected = recommendations[0].get("topic", "")
            else:
                selected = ""
            return TopicSelectOutput(selected_topic=selected, confirmed=True)

        # 数字选择
        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(recommendations):
                selected = recommendations[idx].get("topic", "")
                return TopicSelectOutput(selected_topic=selected, confirmed=True)

        # 自定义主题
        return TopicSelectOutput(selected_topic=text, confirmed=True)
