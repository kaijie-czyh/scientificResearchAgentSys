"""材料构效关系搜索工具（路线 A：构效关系发现）。

核心思想：LLM 深度参与搜索过程，而非仅生成搜索代码。
- LLM 生成候选构效关系假设作为搜索种群种子
- LLM 评估中间结果的科学合理性（物理合法性、与文献一致性）
- LLM 引导搜索空间剪枝（排除物理不合理的区域）
- 文献抽取的 (结构, 性能) 数据点作为代理模型的训练样本

搜索策略：MCTS 启发式（选择→扩展→评估→回传）+ 文献数据代理模型。
代理模型用加权最近邻插值（纯 Python，无重依赖），诚实反映文献证据。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SearchVariable:
    """搜索变量定义。"""

    name: str
    low: float
    high: float
    unit: str = ""
    var_type: str = "continuous"  # continuous / discrete / categorical
    categories: list[str] = field(default_factory=list)

    def sample(self) -> float | str:
        """在定义域内采样一个值。"""
        if self.var_type == "categorical" and self.categories:
            return random.choice(self.categories)
        if self.var_type == "discrete":
            return float(random.randint(int(self.low), int(self.high)))
        return random.uniform(self.low, self.high)

    def contains(self, value) -> bool:
        """值是否在定义域内。"""
        if self.var_type == "categorical":
            return value in self.categories
        try:
            v = float(value)
            return self.low <= v <= self.high
        except (TypeError, ValueError):
            return False


@dataclass
class LiteraturePoint:
    """从文献抽取的 (结构, 性能) 数据点。"""

    config: dict  # {var_name: value}
    target: float  # 目标性能值
    paper_id: str = ""
    chunk_id: str = ""
    note: str = ""


@dataclass
class SearchCandidate:
    """搜索候选（一个材料配置 + 代理模型预测 + LLM 评估）。"""

    config: dict  # {var_name: value}
    predicted_target: float = 0.0  # 代理模型预测的性能
    plausibility: float = 0.0  # LLM 评估的科学合理性 0~1
    mechanism: str = ""  # LLM 给出的物理机制解释
    novelty: str = ""  # LLM 给出的新颖性说明
    surrogate_confidence: float = 0.0  # 代理模型置信度（与最近文献点距离）


class SurrogateModel:
    """文献数据代理模型：加权最近邻插值。

    用文献抽取的 (config, target) 数据点构建一个轻量代理模型：
    - 对新配置，找最近的 k 个文献点，按距离反比加权平均预测性能
    - 距离越远，置信度越低（鼓励搜索有文献支撑的区域，兼顾外推）

    设计依据：不引入 sklearn/numpy 重依赖；诚实反映「文献证据密度」。
    """

    def __init__(self, points: list[LiteraturePoint], k: int = 3):
        self.points = points
        self.k = max(1, min(k, len(points))) if points else 0

    def is_available(self) -> bool:
        return len(self.points) > 0

    @staticmethod
    def _distance(c1: dict, c2: dict) -> float:
        """归一化欧氏距离（仅比较共有的数值变量）。"""
        common = set(c1.keys()) & set(c2.keys())
        if not common:
            return 1.0
        sq_sum = 0.0
        for key in common:
            try:
                v1 = float(c1[key])
                v2 = float(c2[key])
                sq_sum += (v1 - v2) ** 2
            except (TypeError, ValueError):
                # 类别变量：相同为 0，不同为 1
                sq_sum += 0.0 if c1[key] == c2[key] else 1.0
        return math.sqrt(sq_sum / len(common))

    def predict(self, config: dict) -> tuple[float, float]:
        """预测配置的性能与置信度。

        Returns:
            (predicted_target, confidence)
            - predicted_target: 加权最近邻预测值（无数据点时返回 0.0）
            - confidence: 基于最近距离的置信度 0~1（越近越高）
        """
        if not self.points:
            return 0.0, 0.0

        dists = [(self._distance(config, p.config), p) for p in self.points]
        dists.sort(key=lambda x: x[0])

        k = self.k
        nearest = dists[:k]
        # 距离为 0 时直接返回该点目标值
        if nearest and nearest[0][0] == 0.0:
            return nearest[0][1].target, 1.0

        total_w = 0.0
        weighted_target = 0.0
        for d, p in nearest:
            w = 1.0 / (d + 1e-6)
            weighted_target += w * p.target
            total_w += w
        predicted = weighted_target / total_w if total_w > 0 else 0.0
        # 置信度：最近距离越小越高
        min_d = nearest[0][0]
        confidence = max(0.0, 1.0 / (1.0 + min_d))
        return predicted, confidence


class MCTSSearcher:
    """MCTS 启发式搜索器（LLM 引导）。

    流程（每轮迭代）：
    1. 选择：从已探索候选中按 UCB1 选一个高潜力配置
    2. 扩展：LLM 在该配置邻域生成新的候选配置（物理合法的扰动）
    3. 评估：代理模型预测性能 + LLM 评估科学合理性
    4. 回传：把候选加入候选池，记录访问次数与累计奖励

    注意：本类只负责搜索循环的状态管理与代理模型调用，
    LLM 的候选生成与合理性评估由调用方（Agent）注入回调，避免工具层耦合 LLM。
    """

    def __init__(
        self,
        variables: list[SearchVariable],
        surrogate: SurrogateModel,
        max_iterations: int = 8,
        exploration_weight: float = 1.414,
    ):
        self.variables = variables
        self.surrogate = surrogate
        self.max_iterations = max(1, max_iterations)
        self.exploration_weight = exploration_weight
        # 候选池：{config_key: SearchCandidate}
        self._pool: dict[str, SearchCandidate] = {}
        self._visit_counts: dict[str, int] = {}
        self._total_visits = 0

    @staticmethod
    def _config_key(config: dict) -> str:
        return "|".join(f"{k}={v}" for k, v in sorted(config.items()))

    def _random_config(self) -> dict:
        return {v.name: v.sample() for v in self.variables}

    def _ucb1(self, key: str) -> float:
        """UCB1 选择分数：exploit + explore。"""
        if key not in self._pool:
            return float("inf")
        visits = self._visit_counts.get(key, 0)
        if visits == 0:
            return float("inf")
        exploit = self._pool[key].plausibility * 0.5 + min(
            1.0, max(0.0, self._pool[key].predicted_target / 10.0)
        ) * 0.5
        explore = self.exploration_weight * math.sqrt(
            math.log(self._total_visits + 1) / visits
        )
        return exploit + explore

    def evaluate_with_surrogate(self, config: dict) -> tuple[float, float]:
        """用代理模型评估配置（不调 LLM）。"""
        if self.surrogate.is_available():
            return self.surrogate.predict(config)
        # 无文献数据时，返回中性预测
        return 0.5, 0.1

    def add_candidate(self, candidate: SearchCandidate) -> None:
        """把 LLM 评估后的候选加入池。"""
        key = self._config_key(candidate.config)
        self._pool[key] = candidate
        self._visit_counts[key] = self._visit_counts.get(key, 0) + 1
        self._total_visits += 1

    def select_parent(self) -> Optional[dict]:
        """选择下一个要扩展的父配置（UCB1 最优）。"""
        if not self._pool:
            return None
        best_key = max(self._pool.keys(), key=self._ucb1)
        return dict(self._pool[best_key].config)

    def get_candidates(self) -> list[SearchCandidate]:
        """返回候选池所有候选。"""
        return list(self._pool.values())

    def best_candidates(self, top_n: int = 5) -> list[SearchCandidate]:
        """按 (plausibility + predicted_target 归一化) 排序返回 top-N。"""
        if not self._pool:
            return []
        scored = list(self._pool.values())
        # 预测值归一化到 0~1（用池内最大值）
        max_pred = max((c.predicted_target for c in scored), default=1.0) or 1.0
        scored.sort(
            key=lambda c: c.plausibility * 0.6
            + min(1.0, c.predicted_target / max_pred) * 0.3
            + c.surrogate_confidence * 0.1,
            reverse=True,
        )
        return scored[:top_n]


def perturb_config(
    config: dict, variables: list[SearchVariable], scale: float = 0.2
) -> dict:
    """对配置做物理合法的扰动（在变量定义域内）。

    用于 MCTS 扩展阶段：在父配置邻域生成新候选。
    """
    new_config = {}
    var_map = {v.name: v for v in variables}
    for name, value in config.items():
        v = var_map.get(name)
        if v is None:
            new_config[name] = value
            continue
        if v.var_type == "categorical":
            # 类别变量：以概率 scale 切换到另一类别
            if random.random() < scale and len(v.categories) > 1:
                others = [c for c in v.categories if c != value]
                new_config[name] = random.choice(others)
            else:
                new_config[name] = value
        else:
            try:
                base = float(value)
                span = v.high - v.low
                delta = random.uniform(-scale, scale) * span
                new_val = max(v.low, min(v.high, base + delta))
                if v.var_type == "discrete":
                    new_val = float(round(new_val))
                new_config[name] = new_val
            except (TypeError, ValueError):
                new_config[name] = v.sample()
    return new_config


def build_search_variables(space_def: dict) -> list[SearchVariable]:
    """从搜索空间定义 dict 构造 SearchVariable 列表。"""
    variables = []
    for vd in space_def.get("variables", []):
        variables.append(
            SearchVariable(
                name=vd.get("name", "var"),
                low=float(vd.get("low", vd.get("range", [0, 1])[0])),
                high=float(vd.get("high", vd.get("range", [0, 1])[1])),
                unit=vd.get("unit", ""),
                var_type=vd.get("type", "continuous"),
                categories=vd.get("categories", []),
            )
        )
    return variables


def build_literature_points(points_def: list[dict]) -> list[LiteraturePoint]:
    """从文献数据点定义列表构造 LiteraturePoint。"""
    points = []
    for pd in points_def:
        config = pd.get("config", {})
        target = pd.get("target", 0.0)
        try:
            target = float(target)
        except (TypeError, ValueError):
            target = 0.0
        points.append(
            LiteraturePoint(
                config=config,
                target=target,
                paper_id=pd.get("paper_id", ""),
                chunk_id=pd.get("chunk_id", ""),
                note=pd.get("note", ""),
            )
        )
    return points
