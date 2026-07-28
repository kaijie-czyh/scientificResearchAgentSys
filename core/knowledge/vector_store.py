"""向量存储适配层。

起步实现：ChromaDB（本地持久化，零部署）。
接口抽象为 VectorStore，后续可换 FAISS / Pinecone 等。

设计原则：
- 每个 chunk 存：chunk_id, paper_id, embedding, text, metadata
- 按 paper_id 过滤的检索是高频场景
- 嵌入生成由 LLM 适配层提供（embedding 接口），此处只管存储与检索
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol


class EmbeddingFn(Protocol):
    """嵌入函数协议。由 LLM 适配层实现并注入。"""

    def __call__(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class VectorRecord:
    """单条向量记录。"""

    chunk_id: str
    paper_id: str
    text: str
    embedding: list[float]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class VectorQueryResult:
    """向量检索单条结果。"""

    chunk_id: str
    paper_id: str
    text: str
    score: float  # 相似度分数（0~1，越大越相似）
    metadata: dict[str, Any]


class VectorStore(abc.ABC):
    """向量存储抽象基类。"""

    @abc.abstractmethod
    def add(self, records: list[VectorRecord]) -> None:
        """批量写入向量记录。"""

    @abc.abstractmethod
    def query(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        paper_id_filter: Optional[str] = None,
    ) -> list[VectorQueryResult]:
        """检索最相似的 top_k 条记录。可选按 paper_id 过滤。"""

    @abc.abstractmethod
    def delete_by_paper(self, paper_id: str) -> int:
        """删除某论文的所有 chunk 向量。返回删除条数。"""

    @abc.abstractmethod
    def count(self) -> int:
        """当前集合中向量总数。"""


class ChromaVectorStore(VectorStore):
    """ChromaDB 本地持久化实现。

    每个项目独立 collection（以 collection_prefix + project_id 命名）。
    """

    def __init__(
        self,
        persist_dir: Path,
        project_id: str,
        collection_prefix: str = "paper_chunks_",
        distance_metric: str = "cosine",
    ):
        try:
            import chromadb
        except ImportError as e:
            raise ImportError(
                "未安装 chromadb，请运行: pip install chromadb"
            ) from e

        self._project_id = project_id
        self._collection_name = f"{collection_prefix}{project_id}"
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": distance_metric},
        )

    def add(self, records: list[VectorRecord]) -> None:
        if not records:
            return
        self._collection.upsert(
            ids=[r.chunk_id for r in records],
            embeddings=[r.embedding for r in records],
            documents=[r.text for r in records],
            metadatas=[
                {"paper_id": r.paper_id, **r.metadata}
                for r in records
            ],
        )

    def query(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        paper_id_filter: Optional[str] = None,
    ) -> list[VectorQueryResult]:
        where = {"paper_id": paper_id_filter} if paper_id_filter else None
        result = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
        )
        # ChromaDB 返回结构：{ids: [[...]], documents: [[...]], distances: [[...]], metadatas: [[...]]}
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        distances = result.get("distances", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]

        out: list[VectorQueryResult] = []
        for cid, doc, dist, meta in zip(ids, documents, distances, metadatas):
            # cosine 距离转相似度分数
            score = 1.0 - float(dist) if dist is not None else 0.0
            out.append(
                VectorQueryResult(
                    chunk_id=cid,
                    paper_id=meta.get("paper_id", ""),
                    text=doc,
                    score=score,
                    metadata={k: v for k, v in meta.items() if k != "paper_id"},
                )
            )
        return out

    def delete_by_paper(self, paper_id: str) -> int:
        # 先查询该 paper 的所有 chunk id
        result = self._collection.get(where={"paper_id": paper_id})
        ids = result.get("ids", [])
        if ids:
            self._collection.delete(ids=ids)
        return len(ids)

    def count(self) -> int:
        return self._collection.count()
