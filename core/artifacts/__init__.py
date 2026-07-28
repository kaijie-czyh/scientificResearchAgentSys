"""产出物版本管理与溯源层。

提供：
- ArtifactManager: 产出物的创建、版本管理、内容存储
- ProvenanceChain: 溯源链构建与校验

设计原则：
- 大型内容（PDF、图、长 LaTeX）存文件，metadata 存数据库
- 每次修订产生新版本，旧版本保留
- 溯源链：Artifact → Claim → Paper/Experiment，必须无断链
"""
from core.artifacts.version import (
    ArtifactManager,
    ArtifactContentStore,
    ArtifactVersionError,
)
from core.artifacts.provenance import (
    ProvenanceChain,
    ProvenanceNode,
    ProvenanceEdge,
    ProvenanceError,
    ProvenanceValidator,
)

__all__ = [
    "ArtifactManager",
    "ArtifactContentStore",
    "ArtifactVersionError",
    "ProvenanceChain",
    "ProvenanceNode",
    "ProvenanceEdge",
    "ProvenanceError",
    "ProvenanceValidator",
]
