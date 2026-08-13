"""符号回归（Symbolic Regression）—— 路线 A 第二搜索/优化算法。

与 MCTS 互补：MCTS 在配置空间做启发式搜索；
符号回归从文献数据点 (config → target) 直接拟合**解析表达式**，
输出形如  ZT = a * x / (b + x²)  的可解释公式，供发现候选与报告引用。

实现：遗传编程（Genetic Programming），纯 Python，无 sklearn/numpy 重依赖。
- 表达式树：二元算子 (+ - * /) + 一元算子 (exp log inv) + 变量 + 常数
- 种群初始化：ramped half-and-half
- 进化：锦标赛选择 + 子树交叉 + 局部变异 + 精英保留
- 适应度：R²（决定系数），惩罚过深与无变量表达式
- 数值保护：exp/log/1/x 域检查，除零保护

模块化设计：仅依赖标准库，便于替换与测试。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

_EPS = 1e-12


def _safe_div(a: float, b: float) -> float:
    if abs(b) < _EPS:
        return 0.0
    return a / b


def _safe_exp(x: float) -> float:
    x = max(-30.0, min(30.0, x))
    return math.exp(x)


def _safe_log(x: float) -> float:
    if x <= _EPS:
        return 0.0
    return math.log(max(x, _EPS))


def _safe_inv(x: float) -> float:
    if abs(x) < _EPS:
        return 0.0
    return 1.0 / x


BINARY_OPS = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: _safe_div(a, b),
    "^": lambda a, b: _safe_pow(a, b),
}

UNARY_OPS = {
    "exp": _safe_exp,
    "log": _safe_log,
    "inv": _safe_inv,
    "neg": lambda x: -x,
}

CONST_RANGE = (-2.0, 2.0)


def _safe_pow(a: float, b: float) -> float:
    # 仅支持整数次幂（受限指数），避免复数/NaN
    try:
        bi = int(round(b))
        if abs(b - bi) > 1e-6 or bi < -2 or bi > 3:
            return a  # 退化为 a
        return float(a ** bi)
    except (ValueError, OverflowError, ZeroDivisionError):
        return 0.0


class Node:
    """表达式树节点。

    - 叶子：op=None；name 非空 = 变量，否则 value 为常数
    - 一元内部：op in UNARY_OPS，1 个子节点
    - 二元内部：op in BINARY_OPS，2 个子节点
    """

    __slots__ = ("op", "name", "value", "children")

    def __init__(self, op=None, name="", value=0.0, children=None):
        self.op = op
        self.name = name
        self.value = value
        self.children = children or []

    @property
    def arity(self) -> int:
        if self.op is None:
            return 0
        return 1 if self.op in UNARY_OPS else 2

    def is_leaf(self) -> bool:
        return self.op is None

    def eval(self, env: dict[str, float]) -> float:
        if self.op is None:
            if self.name:
                return float(env.get(self.name, 0.0))
            return float(self.value)
        if self.arity == 1:
            return UNARY_OPS[self.op](self.children[0].eval(env))
        return BINARY_OPS[self.op](
            self.children[0].eval(env), self.children[1].eval(env)
        )

    def to_latex(self) -> str:
        if self.op is None:
            return self.name or f"{self.value:g}"
        if self.arity == 1:
            inner = self.children[0].to_latex()
            if self.op == "exp":
                return f"\\exp({inner})"
            if self.op == "log":
                return f"\\ln({inner})"
            if self.op == "inv":
                return f"1/({inner})"
            if self.op == "neg":
                return f"-({inner})"
            return f"{self.op}({inner})"
        a = self.children[0].to_latex()
        b = self.children[1].to_latex()
        if self.op == "/":
            return f"\\frac{{{a}}}{{{b}}}"
        if self.op == "*":
            return f"({a}) \\cdot ({b})"
        if self.op == "^":
            return f"({a})^{{{b}}}"
        return f"({a}) {self.op} ({b})"

    def to_str(self) -> str:
        if self.op is None:
            return self.name or f"{self.value:g}"
        if self.arity == 1:
            inner = self.children[0].to_str()
            if self.op == "exp":
                return f"exp({inner})"
            if self.op == "log":
                return f"log({inner})"
            if self.op == "inv":
                return f"1/({inner})"
            if self.op == "neg":
                return f"-({inner})"
        a = self.children[0].to_str()
        b = self.children[1].to_str()
        return f"({a}{self.op}{b})"

    def size(self) -> int:
        return 1 + sum(c.size() for c in self.children)

    def depth(self) -> int:
        if not self.children:
            return 1
        return 1 + max(c.depth() for c in self.children)

    def count_vars(self, var_set: set[str]) -> int:
        if self.is_leaf() and self.name in var_set:
            return 1
        return sum(c.count_vars(var_set) for c in self.children)


def _clone(node: Node) -> Node:
    if node.is_leaf():
        return Node(name=node.name, value=node.value)
    return Node(op=node.op, children=[_clone(c) for c in node.children])


def _random_leaf(variables: list[str]) -> Node:
    # 恒定范围内随机叶子
    if variables and random.random() < 0.55:
        return Node(name=random.choice(variables))
    return Node(value=random.uniform(*CONST_RANGE))


def _random_tree(
    variables: list[str],
    max_depth: int,
    use_pow: bool = True,
) -> Node:
    if max_depth <= 1 or random.random() < 0.3:
        return _random_leaf(variables)
    if random.random() < 0.35 and max_depth >= 2:
        op = random.choice(list(UNARY_OPS.keys()))
        child = _random_tree(variables, max_depth - 1, use_pow)
        return Node(op=op, children=[child])
    ops = list(BINARY_OPS.keys()) if use_pow else ["+", "-", "*"]
    op = random.choice(ops)
    left = _random_tree(variables, max_depth - 1, use_pow)
    right = _random_tree(variables, max_depth - 1, use_pow)
    return Node(op=op, children=[left, right])


def _collect_nodes(root: Node) -> list[Node]:
    out = []
    stack = [root]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(n.children)
    return out


def _replace_in_tree(root: Node, target: Node, new_node: Node) -> bool:
    """把 root 中的 target 替换为 new_node 克隆；若 root 即 target 返回 False。

    Returns True if replaced, False if root is target (caller 处理整树替换).
    """
    if root is target:
        return False
    for i, child in enumerate(root.children):
        if child is target:
            root.children[i] = _clone(new_node)
            return True
        if _replace_in_tree(child, target, new_node):
            return True
    return False


def _crossover_tree(p1: Node, p2: Node) -> Node:
    if p1.is_leaf():
        return _clone(p2)
    if p2.is_leaf():
        return _clone(p1)
    out = _clone(p1)
    target = random.choice(_collect_nodes(out))
    donor = random.choice(_collect_nodes(p2))
    if out is target:
        return _clone(donor)
    _replace_in_tree(out, target, donor)
    return out


def _mutate_tree(node: Node, variables: list[str]) -> Node:
    out = _clone(node)
    nodes = _collect_nodes(out)
    if not nodes:
        return out
    target = random.choice(nodes)
    r = random.random()
    if target.is_leaf():
        if r < 0.6:
            if variables:
                target.name = random.choice(variables)
            else:
                target.value = random.uniform(*CONST_RANGE)
        else:
            target.value = random.uniform(*CONST_RANGE)
    elif target.arity == 1:
        target.op = random.choice(list(UNARY_OPS.keys()))
    else:
        if r < 0.5:
            target.op = random.choice(list(BINARY_OPS.keys()))
        else:
            # 子树整体替换为新随机树
            replacement = _random_tree(variables, max_depth=2)
            if out is target:
                out = replacement
            else:
                _replace_in_tree(out, target, replacement)
    return out


def _tournament(
    pop: list[Node],
    k: int,
    X: list[dict],
    y: list[float],
    y_denom: float,
) -> Node:
    best, best_f = None, float("-inf")
    for _ in range(k):
        cand = random.choice(pop)
        f = _fitness(cand, X, y, y_denom)
        if f > best_f:
            best_f, best = f, cand
    return _clone(best)


def _fitness(node: Node, X: list[dict], y: list[float], y_denom: float) -> float:
    try:
        preds = [node.eval(env) for env in X]
    except Exception:
        return float("-inf")
    for p in preds:
        if math.isnan(p) or math.isinf(p):
            return float("-inf")
    ss_res = sum((pi - yi) ** 2 for pi, yi in zip(preds, y))
    r2 = 1.0 - ss_res / max(y_denom, 1e-12)
    penalty = 0.0
    if node.size() > 60:
        penalty += 0.15
    if not X or node.count_vars(set(X[0].keys())) < 1:
        penalty += 0.6
    return r2 - penalty


@dataclass
class SymbolicFitResult:
    """符号回归拟合结果。"""

    expr_latex: str = ""          # 表达式（LaTeX）
    expr_str: str = ""            # 表达式（可读串）
    r2: float = 0.0               # 决定系数 R²
    mae: float = float("inf")     # 平均绝对误差
    n_points: int = 0             # 有效数据点数
    variable_names: list[str] = field(default_factory=list)
    fitted: bool = False         # 是否成功拟合
    note: str = ""                # 备注

    def to_dict(self) -> dict:
        return {
            "expr_latex": self.expr_latex,
            "expr_str": self.expr_str,
            "r2": self.r2,
            "mae": self.mae,
            "n_points": self.n_points,
            "variable_names": self.variable_names,
            "fitted": self.fitted,
            "note": self.note,
        }


class SymbolicRegressor:
    """遗传编程符号回归器。"""

    def __init__(
        self,
        population_size: int = 60,
        generations: int = 30,
        max_depth: int = 4,
        tournament_size: int = 3,
        mutation_rate: float = 0.25,
        crossover_rate: float = 0.7,
        seed: Optional[int] = None,
    ):
        self.population_size = population_size
        self.generations = generations
        self.max_depth = max_depth
        self.tournament_size = tournament_size
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        if seed is not None:
            random.seed(seed)

    def fit(self, points: list[dict]) -> SymbolicFitResult:
        """拟合。

        points: [{config: {var: value}, target: float, ...}]
        """
        res = SymbolicFitResult()
        valid = [
            p for p in points
            if isinstance(p.get("config"), dict) and "target" in p
        ]
        if len(valid) < 3:
            res.note = "有效数据点不足 3 个，跳过符号回归"
            return res

        # 数值变量名（排除非数值字符串字段）
        first_config = valid[0]["config"]
        var_names = sorted(
            {
                k
                for p in valid
                for k, v in p["config"].items()
                if not isinstance(v, str) or _is_numeric(v)
            }
        )
        if not var_names:
            res.note = "无非数值变量，符号回归无法进行"
            return res
        res.variable_names = var_names

        X = [
            {name: _to_float(p["config"].get(name, 0.0)) for name in var_names}
            for p in valid
        ]
        y = [_to_float(p["target"]) for p in valid]
        y_mean = sum(y) / len(y)
        y_denom = sum((yi - y_mean) ** 2 for yi in y) or 1e-12
        # 防止极端性能尺度（如 log10）
        y_max = max(abs(yi) for yi in y) or 1.0
        if y_max > 1e6 or y_max < 1e-6:
            y_scale = y_max
            X = X  # 保持
            y_used = [yi / y_scale for yi in y]
            scale_back = y_scale
        else:
            y_used = y
            scale_back = 1.0

        def fitness(node: Node) -> float:
            return _fitness(node, X, y_used, y_denom)

        # 初始化
        population = [
            _random_tree(var_names, self.max_depth) for _ in range(self.population_size)
        ]

        best = None
        best_f = float("-inf")
        for _ in range(self.generations):
            scored = sorted(
                population,
                key=lambda t: fitness(t),
                reverse=True,
            )
            if best_f < fitness(scored[0]):
                best_f = fitness(scored[0])
                best = _clone(scored[0])
            if len(scored) > 1:
                import math as m
                # 提前收敛：若 R² 近似 1.0 且最优稳定，可提前 break
                if best_f > 0.999:
                    break

            next_pop = []
            while len(next_pop) < self.population_size:
                r = random.random()
                if r < self.crossover_rate:
                    p1 = _tournament(population, self.tournament_size, X, y_used, y_denom)
                    p2 = _tournament(population, self.tournament_size, X, y_used, y_denom)
                    child = _crossover_tree(p1, p2)
                    if child.size() > 80:
                        child = _random_tree(var_names, max_depth=3)
                else:
                    parent = _tournament(population, self.tournament_size, X, y_used, y_denom)
                    if random.random() < self.mutation_rate:
                        child = _mutate_tree(parent, var_names)
                    else:
                        child = parent
                next_pop.append(child)
            # 精英保留
            next_pop[0] = _clone(scored[0])
            population = next_pop

        if best is None:
            res.note = "未找到有效表达式"
            return res
        preds = [best.eval(env) * scale_back for env in X]
        ss_res = sum((pi - yi) ** 2 for pi, yi in zip(preds, y))
        r2 = 1.0 - ss_res / max(y_denom, 1e-12)
        mae = sum(abs(pi - yi) for pi, yi in zip(preds, y)) / len(y)
        res.expr_latex = best.to_latex()
        res.expr_str = best.to_str()
        res.r2 = round(max(r2, 0.0), 4) if r2 > 0 else round(r2, 4)
        res.mae = round(mae, 4)
        res.n_points = len(valid)
        res.fitted = True
        res.note = "遗传编程符号回归完成"
        return res


def _is_numeric(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def run_symbolic_regression(
    points: list[dict],
    population_size: int = 60,
    generations: int = 30,
    seed: Optional[int] = None,
) -> SymbolicFitResult:
    """对文献数据点运行符号回归。"""
    reg = SymbolicRegressor(
        population_size=population_size,
        generations=generations,
        seed=seed,
    )
    return reg.fit(points)


__all__ = [
    "SymbolicRegressor",
    "SymbolicFitResult",
    "run_symbolic_regression",
    "Node",
]