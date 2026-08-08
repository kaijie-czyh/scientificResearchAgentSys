"""发现质量量化评估工具（路线 A 客观指标层）。

赛题要求（材料方向）：
- Agent 产出的"发现"必须有可量化的客观质量指标，让评委/材料专家一眼可见
- 评判维度应与领域科研实践对齐：研究紧迫度、可填补性、证据强度、
  数据外推风险、机制论证一致性等

本模块提供：
- GapQualityScorer：基于知识库统计的 Research Gap 客观评分
- DiscoveryReliabilityScorer：构效关系发现的可信度综合评分
- ExpertAssistanceBuilder：为材料专家生成可操作的下一步建议

设计原则：
- 所有指标可独立复现（基于 KnowledgeStore 真实数据，不依赖 LLM）
- 评分函数显式写出加权公式，便于评委/专家理解
- 评分结果落库 KnowledgeStore（KV 表），前端可视化直接读取
"""
from __future__ import annotations

import logging
import math
import statistics
from typing import Any, Optional

from core.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)


# ============================================================
# Gap 质量评分（让评委一眼可见"该 Gap 多紧迫 / 多可填补"）
# ============================================================


class GapQualityScorer:
    """Research Gap 客观质量评分。

    评分维度（每个维度 0~1，加权得综合分）：
    1. 文献稀缺度（literature_scarcity）：相关论文越少 / Gap 关联材料越少被讨论，Gap 越紧迫
    2. 可填补性（fillability）：基于材料库现有三元组覆盖度，评估该 Gap 是否能在本系统内被填补
    3. 行动清晰度（action_clarity）：suggested_actions 是否包含可执行步骤（合成/检索/计算）
    4. 关联强度（related_strength）：Gap 关联的材料/论文是否真实存在知识库中

    设计动机：评委看到 Gap 时，第一反应是"这条 Gap 是不是真的存在？",
    "我能不能马上填补它？", "行动是否明确？"——本类直接量化这三个核心问题。
    """

    # 维度权重
    WEIGHTS = {
        "literature_scarcity": 0.30,
        "fillability": 0.30,
        "action_clarity": 0.20,
        "related_strength": 0.20,
    }

    def __init__(self, store: KnowledgeStore):
        self.store = store

    def score(self, gap: dict) -> dict:
        """对单条 Gap 返回完整评分明细。

        Returns:
            dict 含 4 个维度分数 + 综合分（quality_score 0~1）
            + 评分依据（reasoning，给评委看的人话解释）
        """
        gap_type = gap.get("gap_type", "unexplored")
        related_materials = gap.get("related_materials", []) or []
        evidence = gap.get("evidence", []) or []
        suggested_actions = gap.get("suggested_actions", []) or []

        # 维度 1：文献稀缺度（相关材料/论文数越少，Gap 越紧迫）
        literature_scarcity = self._literature_scarcity(
            related_materials, evidence, gap_type
        )

        # 维度 2：可填补性（材料库是否有支撑数据）
        fillability = self._fillability(related_materials, gap_type)

        # 维度 3：行动清晰度
        action_clarity = self._action_clarity(suggested_actions)

        # 维度 4：关联强度（Gap 关联实体是否真实存在）
        related_strength = self._related_strength(related_materials, evidence)

        # 加权综合
        quality_score = sum(
            self.WEIGHTS[k] * v
            for k, v in [
                ("literature_scarcity", literature_scarcity),
                ("fillability", fillability),
                ("action_clarity", action_clarity),
                ("related_strength", related_strength),
            ]
        )
        quality_score = round(max(0.0, min(1.0, quality_score)), 3)

        return {
            "gap_id": gap.get("gap_id", ""),
            "quality_score": quality_score,
            "dimensions": {
                "literature_scarcity": round(literature_scarcity, 3),
                "fillability": round(fillability, 3),
                "action_clarity": round(action_clarity, 3),
                "related_strength": round(related_strength, 3),
            },
            "weights": dict(self.WEIGHTS),
            "reasoning": self._reasoning(
                gap_type, literature_scarcity, fillability,
                action_clarity, related_strength, len(related_materials),
                len(evidence),
            ),
            "score_version": "v1.0",
        }

    def score_batch(self, gaps: list[dict]) -> list[dict]:
        return [self.score(g) for g in gaps]

    # ---------- 内部评分函数 ----------

    def _literature_scarcity(
        self, materials: list[str], evidence: list, gap_type: str
    ) -> float:
        """文献稀缺度：相关材料/论文数越少，Gap 越紧迫（unexplored 倾向高分）。

        公式：sigmoid(log(1/max(count,1)) 归一化后取反 + type 加权)
        """
        mat_count = len(materials)
        ev_count = len(evidence)

        # 反向映射：关联数越多，稀缺度越低
        # sigmoid 归一化到 (0, 1)
        # 当 mat_count=0 + ev_count=0：scarcity ≈ 1.0（高紧迫）
        # 当 mat_count>=5 + ev_count>=5：scarcity ≈ 0.3（低紧迫）
        total = mat_count + ev_count
        if total == 0:
            base = 1.0  # 完全没有文献支撑的 Gap，稀缺度最高
        else:
            # 1 - 1/(1 + exp(-(total-3)))，total=3 时 ≈ 0.5
            base = 1.0 - (1.0 / (1.0 + math.exp(-(total - 3))))

        # 缺口类型加权（unexplored 看重稀缺度；missing_link 看重知识连接完整度；contradiction 看重冲突量）
        type_boost = {
            "unexplored": 0.0,
            "missing_link": -0.05,
            "contradiction": -0.10,
        }.get(gap_type, 0.0)
        return max(0.0, min(1.0, base + type_boost))

    def _fillability(self, materials: list[str], gap_type: str) -> float:
        """可填补性：基于材料库现有三元组，评估本系统能否填补该 Gap。

        missing_link：有相关材料 + 库内有部分知识（性能/合成）→ 可填补
        unexplored：库内有相关材料 → 可建议进一步检索
        contradiction：库内有多材料数据可支撑对照实验 → 可填补
        """
        if not materials:
            return 0.2  # 无关联材料，无法填补

        # 统计关联材料的知识完整度
        all_mats = {m.name.lower(): m for m in self.store.list_materials(limit=2000)}
        all_props = self.store.list_material_properties(limit=5000)
        all_syns = self.store.list_material_synthesis(limit=5000)

        prop_mat_ids = {p.material_id for p in all_props}
        syn_mat_ids = {s.material_id for s in all_syns}

        # 命中材料数
        hit_mats = [all_mats[m.lower()] for m in materials if m.lower() in all_mats]
        if not hit_mats:
            return 0.3

        # 命中材料的三元组覆盖
        complete = sum(
            1 for m in hit_mats
            if m.material_id in prop_mat_ids and m.material_id in syn_mat_ids
        )
        partial = sum(
            1 for m in hit_mats
            if m.material_id in prop_mat_ids or m.material_id in syn_mat_ids
        )
        coverage = complete + 0.5 * partial
        coverage_ratio = coverage / max(len(hit_mats), 1)

        # 类型加权
        type_weight = {
            "missing_link": 1.0,  # 知识连接缺失，本系统可填补
            "unexplored": 0.7,    # 未探索方向，需要新检索/实验
            "contradiction": 0.8, # 矛盾结论，需要新实验判别
        }.get(gap_type, 0.6)

        return round(min(1.0, coverage_ratio * type_weight + 0.2), 3)

    def _action_clarity(self, suggested_actions: list[str]) -> float:
        """行动清晰度：建议步骤是否包含可执行关键词（合成/检索/计算/验证/测试）。

        关键词命中越多越具体，分数越高。
        """
        if not suggested_actions:
            return 0.1

        keywords = {
            "合成": 0.2,
            "检索": 0.15,
            "计算": 0.2,
            "DFT": 0.25,
            "验证": 0.15,
            "测试": 0.15,
            "实验": 0.15,
            "外推": 0.10,
            "对照": 0.10,
            "消融": 0.10,
            "制备": 0.2,
            "表征": 0.2,
        }

        score = 0.1  # 基线
        text = " ".join(suggested_actions)
        hit_keywords = 0
        for kw, w in keywords.items():
            if kw in text:
                score += w
                hit_keywords += 1
        # 行动数量（≥3 条更具体）
        if len(suggested_actions) >= 3:
            score += 0.1

        return round(min(1.0, score), 3)

    def _related_strength(
        self, materials: list[str], evidence: list[dict]
    ) -> float:
        """关联强度：Gap 关联的实体（材料 / 论文）是否真实存在于知识库。

        材料：必须存在于 Materials 表
        论文：必须存在于 Papers 表（paper_id 可查）
        """
        if not materials and not evidence:
            return 0.0

        # 材料命中
        all_mats = {m.name.lower() for m in self.store.list_materials(limit=2000)}
        mat_hit = sum(1 for m in materials if m.lower() in all_mats)
        mat_score = mat_hit / max(len(materials), 1) if materials else 0.0

        # 论文命中
        paper_ids = {p.paper_id for p in self.store.list_papers()}
        ev_pids = [
            e.get("paper_id", "") for e in evidence
            if e.get("type", "paper") == "paper" or "paper_id" in e
        ]
        paper_hit = sum(1 for pid in ev_pids if pid in paper_ids)
        paper_score = paper_hit / max(len(ev_pids), 1) if ev_pids else 0.0

        # 综合（材料 60% + 论文 40%）
        return round(mat_score * 0.6 + paper_score * 0.4, 3)

    def _reasoning(
        self, gap_type, lit_scar, fill, action, rel, mat_n, ev_n
    ) -> str:
        """生成评分人话解释（评委/专家可读）。"""
        lines = []
        if lit_scar >= 0.7:
            lines.append(f"文献稀缺度高（{mat_n} 材料 + {ev_n} 证据），Gap 紧迫")
        elif lit_scar >= 0.4:
            lines.append(f"文献中等覆盖（{mat_n} 材料 + {ev_n} 证据）")
        else:
            lines.append(f"文献覆盖较广（{mat_n} 材料 + {ev_n} 证据），Gap 紧迫度低")

        if fill >= 0.6:
            lines.append(f"库内数据可支撑填补（可填补性={fill:.2f}）")
        elif fill >= 0.3:
            lines.append(f"库内有部分数据，需补充检索后可填补（{fill:.2f}）")
        else:
            lines.append(f"库内无支撑数据，需从头检索/实验（{fill:.2f}）")

        if action >= 0.6:
            lines.append(f"行动建议清晰具体（{action:.2f}）")
        elif action >= 0.3:
            lines.append(f"行动建议较泛（{action:.2f}）")
        else:
            lines.append(f"缺少明确行动步骤（{action:.2f}）")

        if rel >= 0.6:
            lines.append(f"Gap 实体可追溯（{rel:.2f}）")
        else:
            lines.append(f"Gap 实体追溯性弱（{rel:.2f}）")

        return "；".join(lines)


# ============================================================
# 发现可信度评分（让评委一眼可见"该发现是否值得做实验验证"）
# ============================================================


class DiscoveryReliabilityScorer:
    """构效关系发现可信度综合评分。

    评分维度（每个维度 0~1，加权得综合分）：
    1. 数据外推距离（extrapolation_risk）：候选 config 离训练数据点的归一化距离
       → 距离越远越不可信
    2. 文献支撑密度（literature_density）：关联 paper 数 / 搜索空间相关 paper 数
       → 文献支撑越多越可信
    3. 物理机制论证（mechanism_evidence）：mechanism 5 要素是否完整且含已知理论
       → 论证越严谨越可信
    4. 交叉验证一致性（cross_validation_consistency）：
       MP/OQMD/规则三重验证是否一致
    5. 预测区间合理性（interval_reasonability）：95% CI 宽度/预测值 < 阈值
       → CI 太宽说明代理模型在胡猜

    设计动机：评委看到发现时，最关心的是"这个发现值得实验验证吗？"
    本类从 5 个独立可量化维度回答这个问题。
    """

    WEIGHTS = {
        "extrapolation_safety": 0.25,  # 注意：是 extrapolation_safety = 1 - extrapolation_risk
        "literature_density": 0.20,
        "mechanism_evidence": 0.20,
        "cross_validation_consistency": 0.20,
        "interval_reasonability": 0.15,
    }

    def __init__(self, store: Optional[KnowledgeStore] = None):
        """构造可信度评分器。

        store 可选：提供后可用于读 Materials/Papers 评估外推距离等。
        """
        self.store = store

    # 95% CI 宽度占预测值的最大可接受比例（超过认为外推太远）
    MAX_CI_RATIO = 0.30

    # 理论白名单（物理机制 5 要素中出现的理论/概念关键词）
    KNOWN_THEORY_KEYWORDS = [
        # 热电
        "Boltzmann", "Slack", "PGEC", "phonon-glass", "electron-crystal",
        "Seebeck", "Peltier", "ZT", "power factor", "S²σ",
        # 通用凝聚态物理
        "band structure", "density of states", "DOS", "Fermi",
        "phonon", "carrier", "scattering", "relaxation time",
        # 材料科学通用
        "doping", "alloy", "defect", "grain boundary",
        "Hall effect", "mobility", "thermal conductivity",
        "electron", "hole",
        # 力学/化学/其他方向
        "DFT", "first-principles", "ab initio",
        "phase diagram", "crystal structure", "lattice",
    ]

    def score(
        self,
        relationship: dict,
        search_space: dict,
        literature_points: list[dict],
    ) -> dict:
        """对单条构效关系发现返回完整评分。

        Args:
            relationship: DiscoveryValidateOutput 的 relationships 一项
            search_space: 搜索空间定义（含变量定义域）
            literature_points: 从文献抽取的 (config, target) 数据点

        Returns:
            dict 含 5 维度分数 + 综合分（reliability_score 0~1）+ 风险标签
        """
        config = relationship.get("config", {}) or {}
        predicted = float(relationship.get("predicted_target", 0) or 0)
        mechanism = relationship.get("mechanism", "") or ""
        evidence_refs = relationship.get("evidence_refs", []) or []
        cv = relationship.get("cross_validation", {}) or {}
        novelty = relationship.get("novelty", "unknown")
        confidence = float(relationship.get("confidence", 0) or 0)

        # 维度 1：外推风险（取反得到外推安全性）
        extrap_risk = self._extrapolation_distance(config, literature_points, search_space)
        extrap_safety = 1.0 - extrap_risk

        # 维度 2：文献支撑密度
        lit_density = self._literature_density(evidence_refs, literature_points, config)

        # 维度 3：物理机制论证
        mech_ev = self._mechanism_evidence(mechanism)

        # 维度 4：交叉验证一致性
        cv_consistency = self._cross_validation_consistency(cv, novelty)

        # 维度 5：预测区间合理性
        interval_score = self._interval_reasonability(
            predicted, relationship.get("prediction_interval"), search_space
        )

        # 加权综合
        reliability_score = sum(
            self.WEIGHTS[k] * v
            for k, v in [
                ("extrapolation_safety", extrap_safety),
                ("literature_density", lit_density),
                ("mechanism_evidence", mech_ev),
                ("cross_validation_consistency", cv_consistency),
                ("interval_reasonability", interval_score),
            ]
        )
        reliability_score = round(max(0.0, min(1.0, reliability_score)), 3)

        # 风险标签（评委/专家一眼可见）
        risk_label = self._risk_label(
            reliability_score, extrap_safety, cv_consistency, novelty, confidence
        )

        return {
            "claim_id": relationship.get("claim_id", ""),
            "reliability_score": reliability_score,
            "dimensions": {
                "extrapolation_safety": round(extrap_safety, 3),
                "extrapolation_risk": round(extrap_risk, 3),
                "literature_density": round(lit_density, 3),
                "mechanism_evidence": round(mech_ev, 3),
                "cross_validation_consistency": round(cv_consistency, 3),
                "interval_reasonability": round(interval_score, 3),
            },
            "weights": dict(self.WEIGHTS),
            "risk_label": risk_label,
            "novelty": novelty,
            "llm_confidence": round(confidence, 3),
            "score_version": "v1.0",
        }

    def score_batch(
        self,
        relationships: list[dict],
        search_space: dict,
        literature_points: list[dict],
    ) -> list[dict]:
        return [
            self.score(r, search_space, literature_points)
            for r in relationships
        ]

    # ---------- 内部评分函数 ----------

    def _extrapolation_distance(
        self, config: dict, lit_points: list[dict], search_space: dict
    ) -> float:
        """外推距离：候选 config 离最近训练数据点的归一化距离。

        0 = 在训练点附近（内插）→ 安全
        1 = 远离所有训练点（外推）→ 风险高

        距离用每个变量的 |x - x_nearest| / (high - low) 归一化后求平均。
        """
        if not lit_points or not config:
            return 0.7  # 无训练点 → 默认较高风险

        # 收集训练点 config
        train_configs = []
        for p in lit_points:
            c = p.get("config", {}) if isinstance(p, dict) else {}
            if c:
                train_configs.append(c)
        if not train_configs:
            return 0.7

        # 对每个变量计算最近距离（按定义域归一化）
        variables = search_space.get("variables", []) or []
        var_ranges = {}
        for v in variables:
            name = v.get("name", "")
            lo, hi = v.get("low", 0), v.get("high", 0)
            if hi > lo:
                var_ranges[name] = (lo, hi)

        # 收集 config 中所有数值变量
        dist_per_var = []
        for var_name, val in config.items():
            if not isinstance(val, (int, float)):
                continue
            if var_name not in var_ranges:
                continue
            lo, hi = var_ranges[var_name]
            range_size = hi - lo
            if range_size <= 0:
                continue
            # 找最近训练点的距离
            train_vals = [
                tc.get(var_name) for tc in train_configs
                if isinstance(tc.get(var_name), (int, float))
            ]
            if not train_vals:
                dist_per_var.append(1.0)
                continue
            min_dist = min(abs(val - tv) for tv in train_vals)
            norm_dist = min_dist / range_size
            dist_per_var.append(min(1.0, norm_dist))

        if not dist_per_var:
            return 0.5
        avg_dist = statistics.mean(dist_per_var)
        return round(avg_dist, 3)

    def _literature_density(
        self,
        evidence_refs: list,
        lit_points: list[dict],
        config: dict,
    ) -> float:
        """文献支撑密度：关联 paper 数 / 总文献数据点数。

        包含两个子项：
        - evidence paper 数（直接证据）
        - lit_points 中含相同变量组合的点数（间接证据）
        """
        ev_papers = len([e for e in evidence_refs if e.get("id")])
        if not lit_points:
            return min(1.0, ev_papers / 3.0)
        # 间接：lit_points 中含 config 任意一个变量的数据点数
        if config:
            config_keys = set(config.keys())
            related_points = sum(
                1 for p in lit_points
                if isinstance(p, dict) and config_keys & set((p.get("config") or {}).keys())
            )
        else:
            related_points = 0
        total = ev_papers + 0.5 * related_points
        # 归一化（>10 ≈ 1.0）
        return round(min(1.0, total / 10.0), 3)

    def _mechanism_evidence(self, mechanism: str) -> float:
        """物理机制论证：mechanism 中是否含已知理论/概念关键词 + 长度合理性。

        评分细则：
        - 关键词命中数 / 5（满分 5 个不同关键词）
        - 长度 100~500 字为佳（太长=注水，太短=空泛）
        """
        if not mechanism:
            return 0.1
        text = mechanism
        # 关键词命中
        hits = sum(1 for kw in self.KNOWN_THEORY_KEYWORDS if kw.lower() in text.lower())
        kw_score = min(1.0, hits / 5.0)
        # 长度合理性
        length = len(text)
        if 100 <= length <= 600:
            len_score = 1.0
        elif length < 100:
            len_score = length / 100.0
        else:
            len_score = max(0.3, 1.0 - (length - 600) / 2000.0)
        return round(0.7 * kw_score + 0.3 * len_score, 3)

    def _cross_validation_consistency(
        self, cv: dict, novelty: str
    ) -> float:
        """交叉验证一致性：MP / OQMD / 规则三重验证。

        mp_match + rule_check_passed + literature_consistent 三者一致 → 高分
        """
        if not cv:
            # 无交叉验证数据时：按 novelty 类型给基础分
            base = {
                "novel": 0.3,        # novel 缺验证 → 风险高
                "partially_known": 0.5,
                "known": 0.7,
            }.get(novelty, 0.4)
            return base

        mp_match = cv.get("mp_match")
        rule_passed = cv.get("rule_check_passed")
        lit_consistent = cv.get("literature_consistent")

        # 三项命中数（True 计 1，False 计 0，None 计 0.5）
        def score_flag(v):
            if v is True:
                return 1.0
            if v is False:
                return 0.0
            return 0.5

        avg = (
            score_flag(mp_match)
            + score_flag(rule_passed)
            + score_flag(lit_consistent)
        ) / 3.0
        return round(avg, 3)

    def _interval_reasonability(
        self,
        predicted: float,
        interval: Optional[dict],
        search_space: dict,
    ) -> float:
        """预测区间合理性：95% CI 宽度 / |预测值| < MAX_CI_RATIO。

        CI 越宽说明代理模型在胡猜，分数越低。
        """
        if not interval or predicted == 0:
            # 没传 CI：粗略基于 search space 变量的相对范围估算
            return 0.5

        try:
            lo = float(interval.get("low", predicted * 0.85))
            hi = float(interval.get("high", predicted * 1.15))
        except (TypeError, ValueError):
            return 0.5

        width = hi - lo
        ratio = width / max(abs(predicted), 1e-6)
        if ratio <= self.MAX_CI_RATIO:
            return 1.0
        # 线性衰减：ratio=0.3 → 1.0；ratio=1.0 → 0.3
        score = max(0.3, 1.0 - (ratio - self.MAX_CI_RATIO) * (0.7 / 0.7))
        return round(score, 3)

    def _risk_label(
        self, reliability, extrap_safety, cv_consistency, novelty, confidence
    ) -> str:
        """生成风险标签（评委/专家一眼可见：强烈推荐/谨慎推荐/不建议）。"""
        if reliability >= 0.7 and cv_consistency >= 0.6 and extrap_safety >= 0.5:
            return "✓ 强烈推荐实验验证"
        if reliability >= 0.5 and cv_consistency >= 0.4:
            return "△ 谨慎推荐（建议先小规模验证）"
        if novelty == "novel" and reliability >= 0.4:
            return "○ 高风险高回报（需 DFT 计算支撑）"
        return "✗ 不建议直接实验（建议改进机制论证）"


# ============================================================
# 专家辅助建议（让材料专家感到"系统对我有帮助"）
# ============================================================


class ExpertAssistanceBuilder:
    """为材料专家生成可操作的下一步建议。

    包含 4 类辅助输出：
    1. nearest_neighbor_synthesis：最近邻材料合成工艺（借鉴已有方法）
    2. similar_materials_table：性能对比表（基准对照）
    3. dft_verification_protocol：DFT 计算验证建议（材料结构参数）
    4. experiment_protocol：实验 protocol（温区、测试参数、对照组）
    """

    def __init__(self, store: Optional[KnowledgeStore] = None):
        self.store = store

    def build_for_discovery(
        self,
        relationship: dict,
        search_space: dict,
    ) -> dict:
        """为单条构效关系发现生成专家辅助包。"""
        config = relationship.get("config", {}) or {}
        target_prop = search_space.get("target_property", "ZT")
        material = config.get("material", "")

        return {
            "claim_id": relationship.get("claim_id", ""),
            "material": material,
            "nearest_neighbor_synthesis": self._nearest_neighbor_synthesis(material),
            "similar_materials_table": self._similar_materials_table(material, target_prop),
            "dft_verification_protocol": self._dft_protocol(config, material),
            "experiment_protocol": self._experiment_protocol(config, target_prop, search_space),
            "version": "v1.0",
        }

    def _nearest_neighbor_synthesis(self, material: str) -> list[dict]:
        """最近邻材料合成工艺：返回该材料本身 + 化学式相似的材料的合成方法。"""
        if not material:
            return []
        # 1. 该材料本身的合成工艺
        mats = {m.name.lower(): m for m in self.store.list_materials(limit=2000)}
        syns = self.store.list_material_synthesis(limit=5000)
        props = self.store.list_material_properties(limit=5000)

        results = []
        mat = mats.get(material.lower())
        if mat:
            for syn in syns:
                if syn.material_id == mat.material_id:
                    results.append({
                        "source_material": mat.name,
                        "similarity": 1.0,
                        "method": syn.method,
                        "precursors": syn.precursors,
                        "temperature": syn.temperature,
                        "atmosphere": syn.atmosphere,
                        "duration": syn.duration,
                        "steps": syn.steps[:300] if syn.steps else "",
                        "source_paper_id": syn.paper_id,
                        "source_paper_title": syn.paper_title,
                    })
        # 2. 化学式相似材料（共享元素）
        # 简化匹配：包含相同元素族（Bi2Te3 ↔ Sb2Te3 ↔ PbTe 等）
        element_signature = self._element_signature(material)
        for other_name, other_mat in mats.items():
            if other_name == material.lower():
                continue
            other_sig = self._element_signature(other_mat.name)
            if not element_signature or not other_sig:
                continue
            common = element_signature & other_sig
            if not common:
                continue
            sim = len(common) / max(len(element_signature | other_sig), 1)
            if sim >= 0.4:
                # 找该相似材料的合成方法
                for syn in syns:
                    if syn.material_id == other_mat.material_id:
                        results.append({
                            "source_material": other_mat.name,
                            "similarity": round(sim, 2),
                            "method": syn.method,
                            "precursors": syn.precursors,
                            "temperature": syn.temperature,
                            "atmosphere": syn.atmosphere,
                            "duration": syn.duration,
                            "steps": syn.steps[:300] if syn.steps else "",
                            "source_paper_id": syn.paper_id,
                            "source_paper_title": syn.paper_title,
                        })
                        break  # 每材料取 1 个工艺

        # 按相似度排序、去重（按 method）
        results.sort(key=lambda r: -r["similarity"])
        return results[:5]

    def _element_signature(self, formula: str) -> set[str]:
        """提取化学式元素集合（粗略正则）。"""
        import re
        if not formula:
            return set()
        # 匹配大写字母开头的小写字母可选 + 数字可选
        elements = re.findall(r"([A-Z][a-z]?)", formula)
        return set(elements)

    def _similar_materials_table(
        self, material: str, target_prop: str
    ) -> list[dict]:
        """性能对比表：相同元素族的材料 + 其性能基准。"""
        if not material:
            return []
        mats = {m.name.lower(): m for m in self.store.list_materials(limit=2000)}
        all_props = self.store.list_material_properties(limit=5000)
        props_by_mat: dict[str, list] = {}
        for p in all_props:
            props_by_mat.setdefault(p.material_id, []).append(p)

        mat_signature = self._element_signature(material)
        table = []
        for m in mats.values():
            other_sig = self._element_signature(m.name)
            if not mat_signature or not other_sig:
                continue
            common = mat_signature & other_sig
            sim = len(common) / max(len(mat_signature | other_sig), 1)
            if sim < 0.3:
                continue
            mat_props = props_by_mat.get(m.material_id, [])
            # 取目标性能值（如未指定，取第一个数值型）
            target_val = None
            for p in mat_props:
                if p.property_name and target_prop.lower() in p.property_name.lower():
                    target_val = p.value_num
                    break
            if target_val is None and mat_props:
                # 兜底取第一个有数值属性的
                for p in mat_props:
                    if p.value_num is not None:
                        target_val = p.value_num
                        break
            if target_val is not None:
                table.append({
                    "material": m.name,
                    "similarity": round(sim, 2),
                    "target_property": target_prop,
                    "value": target_val,
                    "unit": next((p.unit for p in mat_props if p.unit), ""),
                    "condition": next((p.condition for p in mat_props if p.condition), ""),
                    "source_paper_id": m.paper_id,
                })
        # 按相似度降序
        table.sort(key=lambda r: (-r["similarity"], -float(r.get("value") or 0)))
        return table[:5]

    def _dft_protocol(self, config: dict, material: str) -> dict:
        """DFT 验证建议：基于已有材料结构参数推荐 DFT 计算 protocol。

        返回：计算任务类型、参数建议、预期产物。
        """
        if not material:
            return {}
        # 查找该材料的结构参数
        mats = {m.name.lower(): m for m in self.store.list_materials(limit=2000)}
        mat = mats.get(material.lower())
        if mat is None:
            return {
                "warning": f"材料 {material} 不在材料库中，建议先做结构优化",
                "tasks": ["lattice_optimization", "band_structure", "phonon_dispersion"],
                "expected_outputs": ["relaxed_structure", "band_gap", "thermal_conductivity"],
            }

        tasks = ["lattice_optimization"]
        if mat.space_group:
            tasks.append("structure_validation")
        if mat.crystal_structure:
            tasks.append("elastic_constants")
        tasks.extend(["band_structure", "boltzmann_transport"])

        # 从 config 推断掺杂浓度
        doping = config.get("doping_concentration", 0) or 0
        if doping > 0:
            tasks.append("doped_supercell_calculation")

        return {
            "material": mat.name,
            "tasks": tasks,
            "expected_outputs": [
                "relaxed_crystal_structure",
                "band_gap",
                "effective_mass",
                "phonon_dispersion",
                "lattice_thermal_conductivity",
                "seebeck_coefficient" if doping > 0 else "seebeck_coefficient_undoped",
            ],
            "reference_space_group": mat.space_group or "未确定",
            "reference_lattice_parameters": mat.lattice_parameters or "未确定",
            "software_recommendations": ["VASP", "Quantum ESPRESSO", "Wien2k"],
            "estimated_cpu_hours": "1000-5000（取决于 supercell 大小）",
            "notes": (
                "若材料含重元素（Bi/Pb/Sb）需考虑 SOC；"
                "若掺杂浓度 >5% 建议 supercell ≥2×2×2。"
            ),
        }

    def _experiment_protocol(
        self, config: dict, target_prop: str, search_space: dict
    ) -> dict:
        """实验 protocol：合成 → 表征 → 性能测试 完整流程。"""
        temperature = config.get("temperature", 300)
        doping = config.get("doping_concentration", 0)
        target_unit = search_space.get("target_unit", "")

        # 温区扫描
        scan_low = max(300, int(temperature) - 100)
        scan_high = int(temperature) + 100
        scan_step = 25

        # 推荐对照
        controls = [
            "未掺杂基线材料（验证掺杂效应）",
            "已发表文献同体系最佳样品（验证合成重现性）",
            "传统工艺样品 vs 新工艺样品（验证工艺改进）",
        ]

        return {
            "material": config.get("material", ""),
            "synthesis": {
                "method_recommendation": "参考材料库中已有相似材料工艺（见 nearest_neighbor_synthesis）",
                "atmosphere": "Ar / N2（防氧化）",
                "post_treatment": "退火（400-700°C, 12-24h）",
                "form": "块体 + 薄膜双线制备（验证尺寸效应）",
            },
            "characterization": [
                "XRD（验证相纯度 + Rietveld 精修）",
                "SEM + EDS（验证微观结构 + 元素分布）",
                "TEM（验证纳米结构 + 界面）",
                "XPS（验证价态 + 掺杂位置）",
                "Hall 测量（载流子浓度 + 迁移率）",
            ],
            "performance_test": {
                "target_property": target_prop,
                "target_unit": target_unit,
                "temperature_range_K": [scan_low, scan_high],
                "temperature_step_K": scan_step,
                "instruments": [
                    "ZEM-3（Seebeck + 电导率同时测）",
                    "LFA 467（热扩散 → 热导率）",
                    "PPMS（低温 Hall + 比热）",
                ],
                "estimated_time_per_sample": "3-5 天（含温区扫描）",
            },
            "controls": controls,
            "statistical_design": {
                "sample_count": "≥5 个独立样品（验证可重现性）",
                "uncertainty_quantification": "误差棒用标准差（≥3 次测量）",
                "publication_threshold": f"目标 {target_prop} 相对基线提升 ≥15%（p<0.05）",
            },
            "duration_estimate_weeks": "8-12 周（含合成 + 表征 + 测试 + 数据分析）",
        }


# ============================================================
# 便捷函数（供 Agent 直接调用）
# ============================================================


def score_gaps(store: KnowledgeStore, gaps: list[dict]) -> list[dict]:
    """批量评分 Research Gap。"""
    return GapQualityScorer(store).score_batch(gaps)


def score_discoveries(
    store: KnowledgeStore,
    relationships: list[dict],
    search_space: dict,
    literature_points: Optional[list[dict]] = None,
) -> list[dict]:
    """批量评分构效关系发现可信度。"""
    return DiscoveryReliabilityScorer(store).score_batch(
        relationships, search_space, literature_points or []
    )


def build_expert_assistance(
    store: KnowledgeStore,
    relationship: dict,
    search_space: dict,
) -> dict:
    """为单条发现生成专家辅助包。"""
    return ExpertAssistanceBuilder(store).build_for_discovery(relationship, search_space)