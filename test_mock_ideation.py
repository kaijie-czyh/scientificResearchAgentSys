"""本地 Mock 验证脚本：模拟真实 LLM API 调用，验证 ideation 阶段 Claim 主题对齐性。

核心思路：
1. 构造 MockLLMProvider 实现 LLMProvider 接口，从 prompt 动态提取研究主题，
   生成紧扣主题的结构化响应（BrainstormSchema / IdeaValidationSchema / ClaimDraftListSchema）
2. 用两个截然不同的主题分别跑 ideation 阶段，验证 Claim 是否包含对应主题关键词
3. 若主题 A 的 Claim 含主题 A 关键词、主题 B 的 Claim 含主题 B 关键词，
   则证明 topic 正确传递到 LLM prompt 且 LLM 响应紧扣主题

运行：
    python test_mock_ideation.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional, Type

# 确保项目根在 sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel

from core.config import get_config
from core.knowledge import KnowledgeStore, Paper
from core.llm.base import (
    EmbeddingRequest,
    EmbeddingResponse,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    StructuredOutputRequest,
)
from core.llm.registry import LLMRegistry
from core.orchestration.context import ExecutionContext
from core.orchestration.node import HumanResponse
from core.state.lifecycle import LifecycleStage
from runtime.cli import _load_env, auto_human_callback
from runtime.pipeline import Pipeline

from stages.common import (
    DRY_RUN,
    KNOWLEDGE_STORE,
    LLM_REGISTRY,
    RESEARCH_CROSS_VALIDATION_REPORT,
    RESEARCH_PAPER_IDS,
    RESEARCH_TOPIC,
)
from stages.ideation.agents import (
    BrainstormSchema,
    ClaimDraftItem,
    ClaimDraftListSchema,
    IdeaDraftItem,
    IdeaValidationSchema,
)

_load_env()


# ===========================================================================
# MockLLMProvider：模拟真实 LLM，从 prompt 提取主题并生成紧扣主题的响应
# ===========================================================================


class MockLLMProvider(LLMProvider):
    """模拟 LLM Provider。

    关键设计：从 prompt 中动态提取「研究主题：xxx」，根据 output_schema 类型
    生成紧扣该主题的结构化响应。这样换主题就能换输出，证明 topic 传递链路完整。
    """

    provider_name = "mock"

    def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        return LLMResponse(
            text="[mock] complete 不用于 ideation 阶段",
            model=model,
            provider=self.provider_name,
        )

    def embed(self, request: EmbeddingRequest, model: str) -> EmbeddingResponse:
        return EmbeddingResponse(
            embeddings=[[0.0] * 8 for _ in request.texts],
            model=model,
            provider=self.provider_name,
        )

    def structured_output(
        self, request: StructuredOutputRequest, model: str
    ) -> BaseModel:
        prompt = request.prompt or ""
        topic = self._extract_topic(prompt)
        schema_name = request.output_schema.__name__

        if schema_name == "BrainstormSchema":
            return self._gen_brainstorm(topic, prompt)
        if schema_name == "IdeaValidationSchema":
            return self._gen_validation(topic, prompt)
        if schema_name == "ClaimDraftListSchema":
            return self._gen_claims(topic, prompt)
        # 兜底
        return request.output_schema()

    # ----- 主题提取 -----

    @staticmethod
    def _extract_topic(prompt: str) -> str:
        """从 prompt 中提取「研究主题：xxx」后面的主题文本。"""
        m = re.search(r"研究主题[：:]\s*(.+?)(?:\n|$)", prompt)
        if m:
            return m.group(1).strip()
        return ""

    @staticmethod
    def _topic_keywords(topic: str) -> list[str]:
        """从主题中抽取关键词（用于后续校验 Claim 是否含这些词）。"""
        # 按常见分隔符切分，过滤掉虚词
        parts = re.split(r"[中的与和及以]", topic)
        keywords = []
        for p in parts:
            p = p.strip()
            if len(p) >= 2:
                keywords.append(p)
        # 额外保留完整主题的核心名词组合
        if topic and topic not in keywords:
            keywords.append(topic)
        return keywords

    # ----- BrainstormSchema 响应 -----

    def _gen_brainstorm(self, topic: str, prompt: str) -> BrainstormSchema:
        """生成 3 个紧扣主题的候选思路。"""
        ideas = [
            {
                "text": (
                    f"针对「{topic}」，设计一种基于契约理论的公平激励机制："
                    f"服务器向客户端提供多档合同，客户端根据自身数据质量与训练成本"
                    f"自选合同档位，在满足激励相容（IC）与个体理性（IR）约束下"
                    f"最大化社会福利。该方法区别于 FedAvg 等权重聚合，"
                    f"将贡献度差异体现到奖励分配中。"
                ),
                "constraints": ["需在非 IID 数据分布下验证 IC 约束", "客户端类型（β值）对服务器不可见"],
                "source_paper_ids": [],
            },
            {
                "text": (
                    f"针对「{topic}」，提出一种考虑客户端异构训练成本的 Shapley 值近似方法："
                    f"在计算贡献度时引入客户端的数据量与计算开销作为成本因子，"
                    f"避免高贡献但高成本的客户端因亏损而退出。"
                    f"与等权重聚合相比，该方法能更公平地反映客户端真实贡献。"
                ),
                "constraints": ["Shapley 近似算法需在多项式时间内完成", "成本因子需可验证"],
                "source_paper_ids": [],
            },
            {
                "text": (
                    f"针对「{topic}」，构建一个基于声誉的动态激励聚合机制："
                    f"客户端声誉直接参与全局模型聚合权重，声誉由历史贡献度与"
                    f"当前轮次模型更新质量共同决定。声誉高的客户端获得更大聚合权重"
                    f"与更高奖励，从而将激励与模型精度提升直接挂钩。"
                ),
                "constraints": ["声誉更新需防篡改", "初始轮次无历史数据时用均匀权重冷启动"],
                "source_paper_ids": [],
            },
        ]
        return BrainstormSchema(ideas=[IdeaDraftItem(**i) for i in ideas])

    # ----- IdeaValidationSchema 响应 -----

    def _gen_validation(self, topic: str, prompt: str) -> IdeaValidationSchema:
        """生成高分验证结果（确保思路通过验证进入 claim_draft）。"""
        return IdeaValidationSchema(
            feasibility=0.8,
            novelty=0.75,
            contribution=0.7,
            reason=(
                f"该思路紧扣「{topic}」，方法路径清晰（契约理论/Shapley值/声誉机制均有"
                f"成熟理论基础），与 FedAvg 等权重聚合有明显差异，对解决联邦学习"
                f"中贡献与奖励不匹配问题有实际贡献。"
            ),
        )

    # ----- ClaimDraftListSchema 响应 -----

    def _gen_claims(self, topic: str, prompt: str) -> ClaimDraftListSchema:
        """生成 2 个紧扣主题的可验证 Claim。"""
        claims = [
            {
                "statement": (
                    f"在「{topic}」场景下，所提基于契约理论的公平激励机制相比 FedAvg "
                    f"等权重聚合，在相同服务器预算约束下能使奖励分配的基尼系数降低至少 20%，"
                    f"同时保持全局模型精度下降不超过 1%。"
                ),
            },
            {
                "statement": (
                    f"针对「{topic}」，当客户端训练成本异构时，考虑成本因子的 Shapley 值"
                    f"激励方法相比纯数据量加权方法，能将客户端参与留存率提升至少 15%，"
                    f"且高贡献客户端不会因亏损退出。"
                ),
            },
        ]
        return ClaimDraftListSchema(claims=[ClaimDraftItem(**c) for c in claims])


# ===========================================================================
# 测试辅助：构造 research 阶段产出（papers + cross_validation_report）
# ===========================================================================


def setup_research_outputs(
    ctx: ExecutionContext, store: KnowledgeStore, topic: str
) -> None:
    """在 ctx 中注入 research 阶段产出，供 ideation 的 brainstorm 读取。"""
    ctx.set(RESEARCH_TOPIC, topic)

    # 构造 2 篇与主题相关的 Paper
    paper_ids: list[str] = []
    for i, title in enumerate([
        "Fair Incentive Mechanism Design in Federated Learning: A Contract Theory Approach",
        "Shapley Value based Contribution Allocation for Federated Learning with Heterogeneous Costs",
    ]):
        pid = f"mock_paper_{i}"
        paper = Paper(
            paper_id=pid,
            title=title,
            authors=["Mock Author"],
            year=2024,
            abstract=f"This paper addresses {topic} with novel mechanism design.",
            arxiv_id=f"2401.{10000 + i}",
        )
        try:
            store.save_paper(paper)
        except Exception:
            pass
        paper_ids.append(pid)

    ctx.set(RESEARCH_PAPER_IDS, paper_ids)

    # 构造交叉验证报告（含 gaps/conflicts/consensus）
    cv_report = {
        "gaps": [
            f"「{topic}」中客户端异构训练成本对激励公平性的影响缺乏系统研究",
            f"「{topic}」中声誉机制直接参与模型聚合的理论保证尚未建立",
        ],
        "conflicts": [
            {
                "claim": "Shapley 值能准确度量联邦学习中客户端的真实贡献",
                "sources": ["mock_paper_0", "mock_paper_1"],
                "resolution": "未解决",
                "confidence": 0.6,
            },
        ],
        "consensus": [
            f"「{topic}」需满足个体理性（IR）约束以防止客户端退出",
            "FedAvg 等权重聚合无法体现客户端贡献差异，导致免费搭车问题",
        ],
        "overall_confidence": 0.75,
    }
    ctx.set(RESEARCH_CROSS_VALIDATION_REPORT, cv_report)


# ===========================================================================
# 核心：运行 ideation 阶段并校验 Claim 主题对齐
# ===========================================================================


def run_ideation_with_mock(topic: str) -> dict:
    """用一个主题跑 ideation 阶段，返回 {claims, topic_keywords}。"""
    config = get_config()
    # 强制 dry_run=True 初始化（避免 start_project 注册真实 provider 干扰），
    # 后面再手动覆盖 DRY_RUN=False + 注入 mock provider
    original_dry_run = config.dry_run
    config.dry_run = True

    pipeline = Pipeline(config=config)
    project_id = f"proj_mock_{abs(hash(topic)) % 100000}"

    # start_project 会创建全新 session + ctx
    session, ctx = pipeline.start_project(project_id, topic)

    # 注入 mock provider：覆盖 minimax（tasks.yaml 全部路由到 minimax）
    registry: LLMRegistry = ctx.get(LLM_REGISTRY)
    mock = MockLLMProvider()
    registry.register_provider("minimax", mock)

    # 关键：覆盖 DRY_RUN=False，让 ideation agents 走真实 LLM 调用分支（即 mock）
    ctx.set(DRY_RUN, False)

    # 注入 research 阶段产出（ideation brainstorm 依赖这些）
    store: KnowledgeStore = ctx.get(KNOWLEDGE_STORE)
    setup_research_outputs(ctx, store, topic)

    # 标记 research 阶段已完成（让 ideation 能正常启动）
    session.start_stage(LifecycleStage.RESEARCH, triggered_by="test")
    session.complete_stage(reason="mock research done", triggered_by="test")

    # 运行 ideation 阶段
    result = pipeline.run_stage(
        session, ctx, LifecycleStage.IDEATION,
        human_callback=auto_human_callback,
    )

    # 恢复 config
    config.dry_run = original_dry_run

    # 从 store 读取生成的 Claim
    claims = store.list_claims()
    claim_statements = [c.statement for c in claims]

    # 提取主题关键词
    topic_keywords = MockLLMProvider._topic_keywords(topic)

    return {
        "topic": topic,
        "status": result.status,
        "claim_statements": claim_statements,
        "topic_keywords": topic_keywords,
        "node_history": result.node_history,
    }


def check_topic_alignment(result: dict) -> tuple[bool, str]:
    """校验 Claim 是否包含主题关键词。

    返回 (通过, 详细信息)。
    """
    topic = result["topic"]
    claims = result["claim_statements"]
    keywords = result["topic_keywords"]

    if not claims:
        return False, f"主题「{topic}」未生成任何 Claim"

    # 校验每个 Claim 是否至少包含 1 个主题关键词
    details: list[str] = []
    all_pass = True
    for i, claim in enumerate(claims):
        hit_keywords = [kw for kw in keywords if kw in claim]
        if not hit_keywords:
            all_pass = False
            details.append(
                f"  Claim #{i + 1} 未命中任何主题关键词：{claim[:80]}..."
            )
        else:
            details.append(
                f"  Claim #{i + 1} 命中关键词 {hit_keywords}：{claim[:60]}..."
            )

    return all_pass, "\n".join(details)


# ===========================================================================
# 主测试：两个截然不同的主题，验证 Claim 主题对齐
# ===========================================================================


def main() -> int:
    print("=" * 72)
    print("Mock LLM 验证：ideation 阶段 Claim 主题对齐性")
    print("=" * 72)

    test_topics = [
        "联邦学习中的公平激励机制设计",
        "热电材料的ZT值优化",
    ]

    all_pass = True

    for topic in test_topics:
        print(f"\n{'─' * 60}")
        print(f"测试主题：{topic}")
        print(f"{'─' * 60}")

        result = run_ideation_with_mock(topic)

        print(f"阶段状态：{result['status']}")
        print(f"主题关键词：{result['topic_keywords']}")
        print(f"生成 Claim 数：{len(result['claim_statements'])}")

        for i, c in enumerate(result['claim_statements']):
            print(f"\n  Claim #{i + 1}：")
            print(f"  {c}")

        # 校验主题对齐
        passed, detail = check_topic_alignment(result)
        print(f"\n主题对齐校验：{'PASS' if passed else 'FAIL'}")
        print(detail)

        if not passed:
            all_pass = False

        # 额外校验：不同主题的 Claim 不应雷同
        # （串主题 bug 的典型表现：换主题后 Claim 内容不变）

    # 跨主题校验：两个主题的 Claim 文本不应高度相似
    print(f"\n{'─' * 60}")
    print("跨主题去重校验（串主题 bug 检测）")
    print(f"{'─' * 60}")

    results = [run_ideation_with_mock(t) for t in test_topics]
    claims_a = set(results[0]["claim_statements"])
    claims_b = set(results[1]["claim_statements"])
    overlap = claims_a & claims_b

    if overlap:
        all_pass = False
        print(f"FAIL：两个不同主题的 Claim 存在重复（{len(overlap)} 条），疑似串主题")
        for c in overlap:
            print(f"  重复 Claim：{c[:80]}...")
    else:
        print("PASS：两个主题的 Claim 无重复，主题传递链路正常")

    # 汇总
    print(f"\n{'=' * 72}")
    if all_pass:
        print("总体结论：PASS — ideation 阶段 Claim 紧扣研究主题，主题传递链路完整")
    else:
        print("总体结论：FAIL — 存在主题对齐问题，需排查")
    print("=" * 72)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
