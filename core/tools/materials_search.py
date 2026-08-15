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
        """用代理模型评估配置（不调 LLM）。

        无文献数据点时返回 (0.0, 0.0)：**不编造中性预测值**。
        0.5 之类的"默认分"会被下游误当成科学预测，因此显式置零，
        由调用方根据 surrogate_confidence==0 判定"无文献支撑"。
        """
        if self.surrogate.is_available():
            return self.surrogate.predict(config)
        return 0.0, 0.0

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
        """按 (plausibility + predicted_target 归一化) 排序返回 top-N。

        无文献支撑的候选（surrogate_confidence≈0）被显式降权 0.4：
        没有文献锚点的配置不允许仅凭 LLM 叙事进入发现前列，
        它们仍保留在候选池中但被标注为 unsupported，供验证阶段降级处理。
        """
        if not self._pool:
            return []
        scored = list(self._pool.values())
        # 预测值归一化到 0~1（用池内最大值）
        max_pred = max((c.predicted_target for c in scored), default=1.0) or 1.0
        def _rank(c: SearchCandidate) -> float:
            base = (
                c.plausibility * 0.6
                + min(1.0, c.predicted_target / max_pred) * 0.3
                + c.surrogate_confidence * 0.1
            )
            if c.surrogate_confidence <= 0.05:
                base -= 0.4  # 无文献锚点：惩罚
            return base
        scored.sort(key=_rank, reverse=True)
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


# ===== 目标性能物理边界表（热力学/物理客观定律硬筛） =====
# 候选的 predicted_target 必须落在对应性能的物理合理区间内，
# 否则视为违反客观物理规律的候选，在验证阶段直接剪枝。
# 区间来源：教科书典型值 + 极端文献值的外包络（有意放宽，只拦"物理不可能"）。
PHYSICAL_TARGET_BOUNDS: dict[str, tuple[float, float]] = {
    # 热电性能
    "zt": (1e-3, 4.5),                    # ZT 无量纲；室温 Bi2Te3 ~1，高温 SnSe 报道 2.6-3.1
    "figure of merit": (1e-3, 4.5),
    "seebeck": (-1500.0, 2000.0),         # μV/K
    "seebeck coefficient": (-1500.0, 2000.0),
    "thermal conductivity": (0.005, 2500.0),  # W/m·K（下限接近理论最小，上限含金刚石）
    "electrical conductivity": (1.0, 1e7),    # S/m
    "power factor": (1e-3, 200.0),        # mW/m·K²
    # 电子结构
    "band gap": (0.0, 12.0),              # eV
    "formation energy": (-8.0, 3.0),      # eV/atom
    # 力学/热学
    "young's modulus": (0.001, 1400.0),   # GPa
    "melting point": (0.0, 4000.0),       # K
    "boiling point": (-200.0, 6000.0),    # °C
    "specific heat": (1.0, 3000.0),       # J/kg·K（杜隆-珀蒂极限约束）
    "density": (0.05, 25.0),              # g/cm³
    # 输运/流体
    "viscosity": (0.001, 1e6),            # cSt/mPa·s
    "kinematic viscosity": (0.001, 1e6),
    "dielectric strength": (0.1, 1500.0),  # kV/mm（真空~∞，工程液体上限）
    "gwp": (0.0, 30000.0),                # GWP100（CO2 当量）
    "gwp100": (0.0, 30000.0),
    "odp": (0.0, 15.0),                   # 臭氧消耗潜值
    # 光学
    "refractive index": (1.0, 4.5),
    "absorption coefficient": (1e-1, 1e7),  # cm⁻¹
    # 催化/电化学
    "overpotential": (0.0, 2000.0),       # mV
    "exchange current density": (1e-12, 1.0),  # A/cm²
    "faradaic efficiency": (1.0, 100.0),  # %
    # 通用适任性/评分类目标（无物理单位，仅约束 0~1）
    "suitability score": (0.0, 1.0),
    "score": (0.0, 1.0),
    "performance score": (0.0, 1.0),
}

# 边界检查豁免的定性目标（无法用数值区间约束）
_UNBOUNDED_TARGETS = {"property", "performance", "target", "quality", ""}


def _normalize_target_key(target_property: str) -> str:
    """目标性能名归一化：小写 + 去多余空白，用于边界表匹配。"""
    return " ".join((target_property or "").strip().lower().split())


def check_target_plausibility(
    target_property: str,
    predicted_value: float,
) -> tuple[bool, str]:
    """检查预测的目标性能值是否落在物理合理区间内。

    Returns:
        (passed, reason)
        - passed=True：值在边界内（或目标无边界定义，交由 LLM/文献判断）
        - passed=False：违反物理边界，reason 给出剪枝依据
    """
    key = _normalize_target_key(target_property)
    # 别名映射：热电 ZT 的常见写法
    if key in {"zt value", "zt值", "无量纲热电优值"}:
        key = "zt"
    bounds = PHYSICAL_TARGET_BOUNDS.get(key)
    if bounds is None:
        # 未定义边界的目标不做硬筛（避免误杀），交由文献交叉验证判断
        return True, ""
    lo, hi = bounds
    try:
        v = float(predicted_value)
    except (TypeError, ValueError):
        return False, f"预测值 {predicted_value!r} 不是有效数值，无法通过物理边界检查"
    if not (math.isfinite(v)):
        return False, "预测值为 NaN/Inf，物理上无意义"
    if v < lo:
        return False, f"预测值 {v:.4g} 低于 {key} 物理下限 {lo:g}（违反客观物理规律）"
    if v > hi:
        return False, f"预测值 {v:.4g} 超过 {key} 物理上限 {hi:g}（违反客观物理规律）"
    return True, ""


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


@dataclass
class CalibrationReport:
    """代理模型-数据库校准报告。

    将代理模型（基于文献数据点的加权 KNN）的预测值与
    Materials Project / OQMD / NOMAD 的 DFT 计算值对比，
    量化代理模型的系统偏差，使搜索空间有数据库证据支持。
    """

    calibrated: bool = False           # 是否完成校准（至少 1 个材料匹配到数据库值）
    n_checked: int = 0                 # 尝试查询的材料数
    n_matched: int = 0                 # 成功匹配到数据库值的材料数
    sources_used: list[str] = field(default_factory=list)  # 命中的数据源
    mae: float = 0.0                   # 平均绝对误差（代理预测 vs 数据库 DFT）
    bias: float = 0.0                  # 系统偏差（预测均值 - 数据库均值）
    per_material: list[dict] = field(default_factory=list)  # 逐材料对比详情
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "calibrated": self.calibrated,
            "n_checked": self.n_checked,
            "n_matched": self.n_matched,
            "sources_used": self.sources_used,
            "mae": round(self.mae, 4),
            "bias": round(self.bias, 4),
            "per_material": self.per_material,
            "note": self.note,
        }


def _extract_formula_from_config(config: dict) -> str:
    """从配置字典中提取材料化学式。

    搜索常见的键名：material, formula, composition, compound 等。
    """
    for key in ("material", "formula", "composition", "compound", "material_formula"):
        val = config.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def calibrate_surrogate_with_databases(
    surrogate: SurrogateModel,
    lit_points: list[LiteraturePoint],
) -> CalibrationReport:
    """用材料数据库 DFT 值校准代理模型。

    对文献数据点中出现的材料，查询 MP / OQMD / NOMAD 的 DFT 计算值
    （带隙 / 形成能），与代理模型对该材料配置的预测值对比，
    计算 MAE 和系统偏差，输出校准报告。

    所有数据库查询均优雅降级（网络不可用或未配置 API key 时跳过）。
    """
    import logging
    logger = logging.getLogger(__name__)

    report = CalibrationReport()

    # 延迟导入避免循环依赖
    try:
        from core.tools.materials_project import query_material_by_formula as mp_query
    except Exception:
        mp_query = None
    try:
        from core.tools.oqmd_nomad import query_oqmd_by_formula
    except Exception:
        query_oqmd_by_formula = None
    try:
        from core.tools.materials_db_gap import query_nomad_by_formula
    except Exception:
        query_nomad_by_formula = None

    seen_formulas: set[str] = set()
    comparisons: list[dict] = []
    db_values: list[float] = []
    pred_values: list[float] = []
    sources_hit: set[str] = set()

    for lp in lit_points:
        formula = _extract_formula_from_config(lp.config)
        if not formula or formula in seen_formulas:
            continue
        seen_formulas.add(formula)
        report.n_checked += 1

        # 查询三个数据库，取第一个命中的 DFT 值
        db_value: Optional[float] = None
        db_source = ""

        # 1. Materials Project（带隙）
        if mp_query is not None:
            try:
                mp_results = mp_query(formula)
                if mp_results:
                    bg = mp_results[0].get("band_gap")
                    if bg is not None:
                        db_value = float(bg)
                        db_source = "Materials Project"
                        sources_hit.add("Materials Project")
            except Exception as e:
                logger.debug("MP 校准查询失败 (%s): %s", formula, e)

        # 2. OQMD（形成能 / 带隙）
        if db_value is None and query_oqmd_by_formula is not None:
            try:
                oqmd_resp = query_oqmd_by_formula(formula)
                if oqmd_resp and oqmd_resp.matched and oqmd_resp.entries:
                    entry = oqmd_resp.entries[0]
                    if entry.formation_energy is not None:
                        db_value = float(entry.formation_energy)
                        db_source = "OQMD"
                        sources_hit.add("OQMD")
                    elif entry.band_gap is not None:
                        db_value = float(entry.band_gap)
                        db_source = "OQMD"
                        sources_hit.add("OQMD")
            except Exception as e:
                logger.debug("OQMD 校准查询失败 (%s): %s", formula, e)

        # 3. NOMAD（仅做命中密度统计，无直接数值属性可取）
        if db_value is None and query_nomad_by_formula is not None:
            try:
                nomad_resp = query_nomad_by_formula(formula)
                if nomad_resp and nomad_resp.get("matched"):
                    sources_hit.add("NOMAD")
                    # NOMAD API 不直接返回带隙/形成能，仅标记命中
            except Exception as e:
                logger.debug("NOMAD 校准查询失败 (%s): %s", formula, e)

        if db_value is None:
            continue

        # 代理模型对该配置的预测值
        pred_target, confidence = surrogate.predict(lp.config) if surrogate.is_available() else (0.0, 0.0)
        report.n_matched += 1
        db_values.append(db_value)
        pred_values.append(pred_target)

        comparisons.append({
            "formula": formula,
            "db_source": db_source,
            "db_value": round(db_value, 4),
            "surrogate_prediction": round(pred_target, 4),
            "deviation": round(pred_target - db_value, 4),
            "surrogate_confidence": round(confidence, 4),
        })

    if not comparisons:
        report.note = "无材料匹配到数据库 DFT 值（可能网络不可用或材料不在库中）"
        return report

    # 计算校准指标
    n = len(comparisons)
    report.mae = sum(abs(p - d) for p, d in zip(pred_values, db_values)) / n
    pred_mean = sum(pred_values) / n
    db_mean = sum(db_values) / n
    report.bias = pred_mean - db_mean
    report.sources_used = sorted(sources_hit)
    report.per_material = comparisons
    report.calibrated = True
    report.note = f"代理模型与 {n} 个材料的数据库 DFT 值对比完成"
    return report
