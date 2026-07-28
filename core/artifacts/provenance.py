"""溯源链构建与校验。

溯源链是"推导可信"的核心保证。每个 Artifact 必须能追溯到：
- 引用的 Claim
- Claim 引证的 Paper / Experiment
- 链路无断点

ProvenanceChain 以 DAG 表示溯源链：
- 节点：Artifact / Claim / Paper / Experiment
- 边：cites / cites_evidence / verifies / derives_from
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.knowledge.schema import (
    Artifact,
    Claim,
    ClaimStatus,
    EntityId,
    EntityType,
    Experiment,
    Paper,
)
from core.knowledge.store import KnowledgeStore


class ProvenanceError(Exception):
    """溯源链错误。"""


@dataclass(frozen=True)
class ProvenanceNode:
    """溯源链节点。"""

    entity_id: EntityId
    entity_type: EntityType
    # 实体摘要（避免大对象）
    summary: str
    # 节点状态（用于校验）
    status: Optional[str] = None  # Claim 状态 / Experiment 状态等


@dataclass(frozen=True)
class ProvenanceEdge:
    """溯源链边。"""

    source_id: EntityId  # 引用方
    target_id: EntityId  # 被引用方
    relation: str  # cites_claim / cites_paper / verified_by_experiment / derives_from_idea


@dataclass
class ProvenanceChain:
    """完整溯源链。"""

    root_artifact_id: EntityId
    nodes: dict[EntityId, ProvenanceNode] = field(default_factory=dict)
    edges: list[ProvenanceEdge] = field(default_factory=list)

    def add_node(self, node: ProvenanceNode) -> None:
        self.nodes[node.entity_id] = node

    def add_edge(self, edge: ProvenanceEdge) -> None:
        self.edges.append(edge)

    def find_broken_links(self) -> list[ProvenanceEdge]:
        """找出断链边：指向不存在节点的边。"""
        broken: list[ProvenanceEdge] = []
        for e in self.edges:
            if e.source_id not in self.nodes or e.target_id not in self.nodes:
                broken.append(e)
        return broken

    def find_unverified_claims(self) -> list[ProvenanceNode]:
        """找出链中未验证的 Claim 节点（状态非 VERIFIED）。

        writing 阶段要求所有 Claim 必须 VERIFIED。
        """
        return [
            n for n in self.nodes.values()
            if n.entity_type == EntityType.CLAIM
            and n.status != ClaimStatus.VERIFIED.value
        ]

    def to_dict(self) -> dict:
        """序列化为可读 dict（用于日志与调试）。"""
        return {
            "root_artifact_id": self.root_artifact_id,
            "nodes": [
                {
                    "entity_id": n.entity_id,
                    "entity_type": n.entity_type.value,
                    "summary": n.summary,
                    "status": n.status,
                }
                for n in self.nodes.values()
            ],
            "edges": [
                {
                    "source": e.source_id,
                    "target": e.target_id,
                    "relation": e.relation,
                }
                for e in self.edges
            ],
            "broken_links": [
                {"source": e.source_id, "target": e.target_id, "relation": e.relation}
                for e in self.find_broken_links()
            ],
            "unverified_claims": [
                n.entity_id for n in self.find_unverified_claims()
            ],
        }


class ProvenanceValidator:
    """溯源链构建器与校验器。"""

    def __init__(self, store: KnowledgeStore):
        self._store = store

    def build_chain(self, artifact_id: EntityId) -> ProvenanceChain:
        """从 Artifact 出发，构建完整溯源链。

        遍历：
        Artifact → cites Claim → cites Paper/Experiment
                 → cites Experiment
        Claim → derives from Idea（可选）
        """
        chain = ProvenanceChain(root_artifact_id=artifact_id)
        visited: set[EntityId] = set()

        try:
            artifact = self._store.get_artifact(artifact_id)
        except Exception as e:
            raise ProvenanceError(f"Artifact 不存在: {artifact_id}") from e

        # 根节点
        chain.add_node(
            ProvenanceNode(
                entity_id=artifact.artifact_id,
                entity_type=EntityType.ARTIFACT,
                summary=f"[{artifact.artifact_type.value}] {artifact.title} (v{artifact.version})",
                status=None,
            )
        )
        visited.add(artifact.artifact_id)

        # 遍历引用的 Claim
        for claim_id in artifact.cites_claim_ids:
            self._walk_claim(chain, claim_id, artifact.artifact_id, visited)

        # 遍历直接引用的 Experiment
        for exp_id in artifact.cites_experiment_ids:
            self._walk_experiment(
                chain, exp_id, artifact.artifact_id, "cites_experiment", visited
            )

        return chain

    def _walk_claim(
        self,
        chain: ProvenanceChain,
        claim_id: EntityId,
        source_id: EntityId,
        visited: set[EntityId],
    ) -> None:
        if claim_id in visited:
            # 仍记录边（即使节点已存在）
            chain.add_edge(
                ProvenanceEdge(
                    source_id=source_id,
                    target_id=claim_id,
                    relation="cites_claim",
                )
            )
            return

        try:
            claim = self._store.get_claim(claim_id)
        except Exception:
            # Claim 不存在：记录断链边
            chain.add_edge(
                ProvenanceEdge(
                    source_id=source_id,
                    target_id=claim_id,
                    relation="cites_claim",
                )
            )
            return

        chain.add_node(
            ProvenanceNode(
                entity_id=claim.claim_id,
                entity_type=EntityType.CLAIM,
                summary=claim.statement[:120],
                status=claim.status.value,
            )
        )
        chain.add_edge(
            ProvenanceEdge(
                source_id=source_id,
                target_id=claim.claim_id,
                relation="cites_claim",
            )
        )
        visited.add(claim.claim_id)

        # Claim 的证据引用
        for ref in claim.evidence_refs:
            ref_type = ref.get("type")
            ref_id = ref.get("id")
            if not ref_id:
                continue
            if ref_type == "paper":
                self._walk_paper(chain, ref_id, claim.claim_id, "cites_paper", visited)
            elif ref_type == "experiment":
                self._walk_experiment(
                    chain, ref_id, claim.claim_id, "verified_by_experiment", visited
                )

        # 派生自 Idea
        if claim.source_idea_id:
            self._walk_idea(chain, claim.source_idea_id, claim.claim_id, visited)

    def _walk_paper(
        self,
        chain: ProvenanceChain,
        paper_id: EntityId,
        source_id: EntityId,
        relation: str,
        visited: set[EntityId],
    ) -> None:
        chain.add_edge(
            ProvenanceEdge(
                source_id=source_id, target_id=paper_id, relation=relation
            )
        )
        if paper_id in visited:
            return
        try:
            paper = self._store.get_paper(paper_id)
        except Exception:
            return  # 断链已通过边记录

        chain.add_node(
            ProvenanceNode(
                entity_id=paper.paper_id,
                entity_type=EntityType.PAPER,
                summary=f"{paper.title} ({paper.year or 'n.d.'})",
                status=None,
            )
        )
        visited.add(paper.paper_id)

    def _walk_experiment(
        self,
        chain: ProvenanceChain,
        exp_id: EntityId,
        source_id: EntityId,
        relation: str,
        visited: set[EntityId],
    ) -> None:
        chain.add_edge(
            ProvenanceEdge(
                source_id=source_id, target_id=exp_id, relation=relation
            )
        )
        if exp_id in visited:
            return
        try:
            exp = self._store.get_experiment(exp_id)
        except Exception:
            return

        chain.add_node(
            ProvenanceNode(
                entity_id=exp.experiment_id,
                entity_type=EntityType.EXPERIMENT,
                summary=f"[{exp.status.value}] {exp.name}",
                status=exp.status.value,
            )
        )
        visited.add(exp.experiment_id)

    def _walk_idea(
        self,
        chain: ProvenanceChain,
        idea_id: EntityId,
        source_id: EntityId,
        visited: set[EntityId],
    ) -> None:
        chain.add_edge(
            ProvenanceEdge(
                source_id=source_id,
                target_id=idea_id,
                relation="derives_from_idea",
            )
        )
        if idea_id in visited:
            return
        try:
            idea = self._store.get_idea(idea_id)
        except Exception:
            return

        chain.add_node(
            ProvenanceNode(
                entity_id=idea.idea_id,
                entity_type=EntityType.IDEA,
                summary=idea.text[:120],
                status=idea.status,
            )
        )
        visited.add(idea.idea_id)

    # ===== 校验 =====

    def validate_for_writing(self, artifact_id: EntityId) -> None:
        """校验 Artifact 是否可进入论文写作阶段。

        要求：
        1. 溯源链无断点
        2. 所有 Claim 已 VERIFIED
        3. 所有 Experiment 已 COMPLETED
        """
        chain = self.build_chain(artifact_id)

        broken = chain.find_broken_links()
        if broken:
            raise ProvenanceError(
                f"溯源链存在 {len(broken)} 个断点："
                f"{[{'source': e.source_id, 'target': e.target_id} for e in broken]}"
            )

        unverified = chain.find_unverified_claims()
        if unverified:
            raise ProvenanceError(
                f"存在 {len(unverified)} 个未验证的 Claim，无法进入写作阶段："
                f"{[n.entity_id for n in unverified]}"
            )

        # 校验 Experiment 状态
        uncompleted_exp = [
            n for n in chain.nodes.values()
            if n.entity_type == EntityType.EXPERIMENT
            and n.status != "completed"
        ]
        if uncompleted_exp:
            raise ProvenanceError(
                f"存在 {len(uncompleted_exp)} 个未完成的 Experiment："
                f"{[n.entity_id for n in uncompleted_exp]}"
            )
